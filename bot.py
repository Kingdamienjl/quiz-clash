"""Twitch chat quiz bot for kingdamienjl.

TwitchIO 3.3.2 (EventSub chat, no IRC). The bot logs in as Chris himself
(bot id == owner id == 59499420), so the chat subscription is for his own
channel — this deliberately differs from the TwitchIO quickstart, which
skips subscribing to the bot's own channel.

Commands:
  !quiz [category] [difficulty]  start a round (gta, anime, gaming, general,
      movies, music; easy, medium, hard — args in either order)
  !score            your points, XP, level, HP, battle wins and streak
  !leaderboard      top 5 players with level/XP
  !quizstop         mods/broadcaster only — stop the active round and pause
      auto-rounds (no more quiz questions until !quizresume)
  !quizresume       mods/broadcaster only — resume auto-rounds after !quizstop
  !quizhelp         list commands
  !fight @user      challenge someone to a quiz battle (HP, powers)
  !accept / !decline  answer a pending battle challenge
  !ability <name>   use a banked power in your battle
  !abilities        show your banked powers and the full power list
  !battle           current battle status

Correct answers earn XP by difficulty (easy 10, medium 25, hard 50) and
levels come every 100 XP. Level-ups are announced in chat and shown on
the live overlay. Battles (rpg.py): HP scales with level, first correct
answer in a battle round deals damage, wrong answers hurt the answerer,
winner takes 50 XP. Powers are earned by answering questions about their
character (or a milestone) and banked for the next battle — see
!abilities and README.md.

Live overlay: the bot also serves a small page for OBS browser sources at
http://<bot-host>:8765/ (config OVERLAY_PORT) — leaderboard with XP bars,
the live question with countdown, and a LEVEL UP animation. Battles show
HP bars and banked powers; power grants show up in the overlay feed.

Themed powers: answer a question about a character and you bank their
power (The Flash -> Flash Speed, Goku -> Super Saiyan Strength, ...).
Bank up to 3, spend them in battle with !ability. See powers.py and
README.md for the full list.

Proactive rounds: about every 15 minutes while the stream is live, the bot
asks a question on its own (Helix live check first; silent when offline).
Command rounds work any time, live or not.

Athena: when someone mentions Athena by name (not a command), she replies
via Ollama on Fails PC (hermes3:8b), at most once per ~20 seconds. If
Ollama is down, she stays quiet.

Run:  python bot.py        (needs .env — see README.md / config.example)
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

import asqlite
import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from athena import AthenaBrain, mentions_athena
from config import Config, PROJECT_DIR
from overlay import OverlayServer
from quiz import (
    CATEGORIES,
    DIFFICULTIES,
    QuizManager,
    ScoreStore,
    is_correct,
    load_questions,
    proactive_due,
    resolve_category,
    resolve_difficulty,
    should_post_proactive,
)
from powers import MAX_BANKED, POWERS
from rpg import (
    BATTLE_MAX_ROUNDS,
    BATTLE_ROUND_SECONDS,
    max_hp,
)

if TYPE_CHECKING:
    import sqlite3 as _sqlite3

LOGGER: logging.Logger = logging.getLogger("twitch-quiz-bot")

CATEGORY_LIST = ", ".join(CATEGORIES)
DIFFICULTY_LIST = ", ".join(DIFFICULTIES)
PROACTIVE_COOLDOWN_SECONDS = 300.0  # don't auto-ask right after any round


class QuizBot(commands.AutoBot):
    def __init__(
        self,
        *,
        config: Config,
        quiz: QuizManager,
        brain: AthenaBrain,
        token_database: asqlite.Pool,
        subs: list[eventsub.SubscriptionPayload],
    ) -> None:
        self.config = config
        self.quiz = quiz
        self.brain = brain
        self.token_database = token_database
        self._channel_user: twitchio.User | twitchio.PartialUser | None = None
        self._last_round_ended_at: float | None = None
        self._last_proactive_check_at: float = 0.0

        super().__init__(
            client_id=config.client_id,
            client_secret=config.client_secret,
            bot_id=config.bot_id,
            owner_id=config.owner_id,
            prefix="!",
            subscriptions=subs,
            force_subscribe=True,
        )

    # -- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        await self.add_component(QuizComponent(self))
        try:
            users = await self.fetch_users(logins=[self.config.channel_login])
            if users:
                self._channel_user = users[0]
        except Exception:
            LOGGER.warning("Could not resolve channel user at startup", exc_info=True)
        asyncio.create_task(self._proactive_loop())
        LOGGER.info("Setup done. Proactive rounds every %d min while live.",
                    self.config.proactive_interval_minutes)

    async def event_ready(self) -> None:
        LOGGER.info("Logged in as bot id %s — watching #%s", self.bot_id, self.config.channel_login)

    async def event_token_refreshed(self, payload: twitchio.TokenRefreshedPayload) -> None:
        """Twitch rotated our token: persist the new pair so restarts keep working."""
        LOGGER.info("Token refreshed for user %s", payload.user_id)
        try:
            async with self.token_database.acquire() as conn:
                await conn.execute(
                    "INSERT INTO tokens (user_id, token, refresh) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET token = excluded.token, refresh = excluded.refresh",
                    (payload.user_id, payload.token, payload.refresh_token),
                )
        except Exception:
            LOGGER.warning("Could not persist refreshed token", exc_info=True)

    async def event_oauth_authorized(self, payload: twitchio.authentication.UserTokenPayload) -> None:
        await self.add_token(payload.access_token, payload.refresh_token)
        # NOTE: quickstart skips subscribing when the authorizing user IS the
        # bot. Here the bot IS the broadcaster, so his channel is exactly the
        # one we must subscribe to.
        subs = [eventsub.ChatMessageSubscription(
            broadcaster_user_id=payload.user_id, user_id=self.bot_id
        )]
        resp = await self.multi_subscribe(subs)
        if resp and resp.errors:
            LOGGER.warning("Failed to subscribe to chat: %r", resp.errors)

    async def add_token(self, token: str, refresh: str) -> twitchio.authentication.ValidateTokenPayload:
        resp: twitchio.authentication.ValidateTokenPayload = await super().add_token(token, refresh)
        query = """
            INSERT INTO tokens (user_id, token, refresh)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET token = excluded.token, refresh = excluded.refresh
        """
        async with self.token_database.acquire() as connection:
            await connection.execute(query, (resp.user_id, token, refresh))
        LOGGER.info("Stored token for user %s", resp.user_id)
        return resp

    # -- chat sending ------------------------------------------------------

    async def send_chat(self, text: str) -> None:
        """Send a message to the channel outside a command context."""
        text = text.strip()
        if not text:
            return
        target = self._channel_user
        if target is None:
            LOGGER.warning("No channel user cached; cannot send: %r", text[:80])
            return
        try:
            await target.send_message(text, sender=self.bot_id)
        except Exception:
            LOGGER.warning("Failed to send chat message", exc_info=True)

    # -- quiz rounds -------------------------------------------------------

    def _mark_round_ended(self) -> None:
        self._last_round_ended_at = time.monotonic()

    async def _start_round(self, asked_by: str, category: str | None, proactive: bool) -> bool:
        rnd = self.quiz.start_round(asked_by, category, proactive=proactive)
        if rnd is None:
            return False
        tag = "🧠 QUIZ" if not proactive else "🧠 QUIZ TIME"
        cat = f" [{rnd.question.category}]" if rnd.question.category else ""
        diff = f" [{rnd.question.difficulty} • {rnd.question.xp_value} XP]"
        await self.send_chat(
            f"{tag}{cat}{diff} ({rnd.timeout_seconds}s): {rnd.question.question} "
            "— first correct answer in chat wins!"
        )
        asyncio.create_task(self._round_timeout(rnd))
        return True

    async def _round_timeout(self, rnd) -> None:
        await asyncio.sleep(rnd.timeout_seconds + 0.5)
        question = self.quiz.expire_round()
        if question is not None:
            self._mark_round_ended()
            await self.send_chat(f"⏰ Time! The answer was: {question.answer}")

    # -- battle rounds -----------------------------------------------------

    async def _post_battle_question(self, battle) -> None:
        """Ask the next battle question and arm its timeout."""
        question = self.quiz.battle.next_question(battle)
        token = battle.round_token
        await self.send_chat(
            f"⚔️ BATTLE Round {battle.round_no}/{BATTLE_MAX_ROUNDS} "
            f"[{question.category} • {question.difficulty}] ({BATTLE_ROUND_SECONDS}s): "
            f"{question.question} — fighters only, first correct answer hits!"
        )
        asyncio.create_task(self._battle_round_timeout(battle, token))

    async def _battle_round_timeout(self, battle, token: int) -> None:
        await asyncio.sleep(BATTLE_ROUND_SECONDS + 0.5)
        # Stale guard: the round already ended (answered / rerolled).
        if (
            self.quiz.battle.active is not battle
            or battle.round_token != token
            or battle.question is None
        ):
            return
        result = self.quiz.battle.timeout_round()
        for event in result["events"]:
            await self.send_chat(event["text"])
        await self._after_battle_round(result)

    async def _after_battle_round(self, result: dict) -> None:
        """After a battle round resolves: end the battle or ask next."""
        if result["battle_over"]:
            self._mark_round_ended()
            return
        battle = result["battle"]
        if battle is None or battle.status != "active":
            return
        token = battle.round_token
        await asyncio.sleep(2)  # let the hit land in chat before the next question
        # Reroll (or a timeout) may have moved the battle on meanwhile.
        if (
            self.quiz.battle.active is battle
            and battle.round_token == token
            and battle.status == "active"
        ):
            await self._post_battle_question(battle)

    async def _is_live(self) -> bool:
        try:
            async for stream in self.fetch_streams(user_logins=[self.config.channel_login]):
                if stream.type == "live":
                    return True
            return False
        except Exception:
            LOGGER.warning("Live check failed", exc_info=True)
            return False

    async def _proactive_loop(self) -> None:
        interval = self.config.proactive_interval_minutes * 60.0
        await asyncio.sleep(60)  # let the bot settle in first
        while True:
            try:
                now = time.monotonic()
                # A challenge left hanging expires (announced once, here).
                expired = self.quiz.battle.expire_pending()
                if expired is not None:
                    await self.send_chat(
                        f"⌛ @{expired.challenger}'s challenge to @{expired.opponent} expired."
                    )
                if proactive_due(now, self._last_proactive_check_at, interval):
                    self._last_proactive_check_at = now
                    live = await self._is_live()
                    active = self.quiz.active is not None and not self.quiz.active.expired
                    # Battles (and waiting challenges) block auto-rounds too.
                    battle_busy = (
                        self.quiz.battle.active is not None
                        or self.quiz.battle.pending is not None
                    )
                    if should_post_proactive(
                        now=now,
                        is_live=live,
                        round_active=active or battle_busy,
                        last_round_ended_at=self._last_round_ended_at,
                        interval_seconds=interval,
                        cooldown_after_round_seconds=PROACTIVE_COOLDOWN_SECONDS,
                        paused=self.quiz.paused,
                    ):
                        started = await self._start_round("athena", None, proactive=True)
                        if started:
                            LOGGER.info("Proactive round posted (stream live)")
                    elif not live:
                        LOGGER.debug("Proactive check: stream offline, staying silent")
            except Exception:
                LOGGER.warning("Proactive loop error", exc_info=True)
            await asyncio.sleep(60)

    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        # The bot logs in as Chris himself (bot_id == owner_id), so his own
        # chat messages ARE the owner's commands — but TwitchIO's default
        # event_message drops every message from the bot's own user id,
        # which silently ate every command Chris typed (!quizstop included).
        # Let them through; the bot never sends a "!"-prefixed message
        # itself, so no self-command loop is possible.
        if payload.source_broadcaster is not None:
            return
        await self.process_commands(payload)


class QuizComponent(commands.Component):
    def __init__(self, bot: QuizBot) -> None:
        self.bot = bot
        self.quiz = bot.quiz
        self.brain = bot.brain

    # -- message listener: answers + Athena --------------------------------

    @commands.Component.listener()
    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        text = (payload.text or "").strip()
        if not text:
            return
        # Ignore our own messages — except the owner's own answers: the bot
        # logs in as Chris, so his typed answers arrive as bot_id. An exact
        # answer match is safe (bot announcements never equal an answer),
        # so let those through to the quiz check below; everything else
        # from bot_id (announcements, help text) is still ignored, and
        # Athena never talks to herself.
        is_self = str(payload.chatter.id) == str(self.bot.bot_id)
        if is_self and not (
            self.quiz.active is not None
            and is_correct(text, self.quiz.active.question)
        ):
            return
        # Cache the channel handle for proactive sends.
        if self.bot._channel_user is None:
            self.bot._channel_user = payload.broadcaster

        # 0. Battle answers take priority (fighters only, not commands).
        battle = self.quiz.battle
        if (
            battle.active is not None
            and battle.active.status == "active"
            and not text.startswith("!")
            and payload.chatter.name.lower() in battle.active.fighters()
        ):
            result = battle.answer(payload.chatter.name, text)
            for event in result["events"]:
                await self.bot.send_chat(event["text"])
            if result["battle_over"]:
                self.bot._mark_round_ended()
            elif result["status"] in ("hit", "fizzle"):
                await self.bot._after_battle_round(result)
            # "self" (wrong guess) leaves the round open for the other
            # fighter; "spent" is a locked-out repeat guess — stay quiet.
            return

        # 1. Quiz answers take priority.
        if self.quiz.active is not None and not text.startswith("!"):
            status, detail = self.quiz.check_answer(payload.chatter.name, text)
            if status == "correct":
                self.bot._mark_round_ended()
                award = self.quiz.last_award or {}
                msg = (
                    f"🎉 @{payload.chatter.name} got it! +{award.get('xp_gained', 0)} XP "
                    f"({award.get('difficulty', '')}) — {award.get('points', detail)} pts • "
                    f"{award.get('xp', 0)} XP • Level {award.get('level', 1)}."
                )
                power = award.get("power")
                if power and award.get("power_granted"):
                    msg += f" ⚡ Gained {power['name']} ({power['character']})!"
                elif award.get("power_bank_full"):
                    msg += " ⚡ Power bank is full — spend powers with !ability in battle!"
                await self.bot.send_chat(msg)
                if award.get("leveled_up"):
                    await self.bot.send_chat(
                        f"🆙 LEVEL UP! @{payload.chatter.name} reached Level {award['level']}! 🎉"
                    )
                return
            if status == "wrong":
                # A wrong guess breaks the consecutive-correct streak.
                # (Athena mentions are for Athena, not guesses.)
                if not mentions_athena(text):
                    self.quiz.scores.reset_streak(payload.chatter.name)
            if status == "no-round":
                pass  # round expired between checks; fall through to Athena

        # 2. Athena chats back when mentioned by name (not on commands).
        if mentions_athena(text):
            reply = await self.brain.reply(payload.chatter.display_name or payload.chatter.name, text)
            if reply:
                await self.bot.send_chat(reply)

    # -- commands ----------------------------------------------------------

    @commands.command()
    async def quiz(self, ctx: commands.Context, arg1: str | None = None, arg2: str | None = None) -> None:
        """Start a quiz round. Usage: !quiz [category] [difficulty] (either order)"""
        category: str | None = None
        difficulty: str | None = None
        for arg in (arg1, arg2):
            if arg is None:
                continue
            cat = resolve_category(arg)
            diff = resolve_difficulty(arg)
            if cat is not None and category is None:
                category = cat
            elif diff is not None and difficulty is None:
                difficulty = diff
            else:
                await ctx.reply(
                    f"Unknown option {arg!r}. Categories: {CATEGORY_LIST} • "
                    f"Difficulties: {DIFFICULTY_LIST}"
                )
                return
        if self.quiz.active is not None and not self.quiz.active.expired:
            await ctx.reply("A round is already running — answer in chat!")
            return
        if self.quiz.battle.active is not None:
            await ctx.reply("A battle is running — wait for it to end, then !quiz.")
            return
        rnd = self.quiz.start_round(ctx.chatter.name, category, difficulty=difficulty)
        if rnd is None:
            await ctx.reply("A round is already running — answer in chat!")
            return
        await ctx.send(
            f"🧠 QUIZ [{rnd.question.category} • {rnd.question.difficulty} • {rnd.question.xp_value} XP] "
            f"({rnd.timeout_seconds}s): "
            f"{rnd.question.question} — first correct answer wins!"
        )
        asyncio.create_task(self.bot._round_timeout(rnd))

    @commands.command()
    async def score(self, ctx: commands.Context) -> None:
        """Show your points, XP, level, HP, battle wins, streak and banked powers. Usage: !score"""
        stats = self.quiz.scores.get_stats(ctx.chatter.name)
        bank = stats.get("bank", [])
        bank_text = (
            " • ⚡ banked: " + ", ".join(
                f"{row['name']}" + (f" x{row['count']}" if row["count"] > 1 else "")
                for row in bank
            )
            if bank else " • ⚡ banked: none (answer questions to earn powers!)"
        )
        await ctx.reply(
            f"@{ctx.chatter.name}: {stats['points']} pts • {stats['xp']} XP • "
            f"Level {stats['level']} ({stats['xp_to_next']} XP to Level {stats['level'] + 1}) • "
            f"❤️ {max_hp(stats['level'])} HP • 🏆 {stats['battle_wins']} battle wins • "
            f"🔥 streak {stats['streak']} (best {stats['best_streak']}){bank_text}"
        )

    @commands.command()
    async def leaderboard(self, ctx: commands.Context) -> None:
        """Show the top 5 players with level/XP. Usage: !leaderboard"""
        top = self.quiz.scores.leaderboard_with_xp(5)
        if not top:
            await ctx.reply("No scores yet — be the first! Type !quiz to start a round.")
            return
        line = "  •  ".join(
            f"{i}. {row['username']} — Lv{row['level']} • {row['xp']} XP ({row['points']} pts)"
            for i, row in enumerate(top, 1)
        )
        await ctx.reply(f"🏆 Leaderboard: {line}")

    @commands.command()
    async def quizstop(self, ctx: commands.Context) -> None:
        """Stop the active round and pause auto-rounds (mods/broadcaster only). Usage: !quizstop"""
        is_mod = bool(getattr(ctx.chatter, "moderator", False))
        is_owner = str(ctx.chatter.id) == str(self.bot.config.owner_id)
        if not (is_mod or is_owner):
            await ctx.reply("Only mods can stop the quizzes.")
            return
        question = self.quiz.pause()
        self.bot._mark_round_ended()
        if question is None:
            await ctx.send("🛑 Quizzes paused — no more auto-rounds until !quizresume.")
            return
        await ctx.send(
            f"🛑 Quiz stopped (the answer was: {question.answer}). "
            "Quizzes are paused — !quizresume to start them again."
        )

    @commands.command()
    async def quizresume(self, ctx: commands.Context) -> None:
        """Resume auto-rounds after a !quizstop pause (mods/broadcaster only). Usage: !quizresume"""
        is_mod = bool(getattr(ctx.chatter, "moderator", False))
        is_owner = str(ctx.chatter.id) == str(self.bot.config.owner_id)
        if not (is_mod or is_owner):
            await ctx.reply("Only mods can resume the quizzes.")
            return
        if not self.quiz.paused:
            await ctx.reply("Quizzes aren't paused — auto-rounds are already on.")
            return
        self.quiz.resume()
        await ctx.send("✅ Quizzes resumed — auto-rounds are back on while you're live.")

    @commands.command()
    async def quizhelp(self, ctx: commands.Context) -> None:
        """List quiz commands. Usage: !quizhelp"""
        await ctx.reply(
            "!quiz [category] [difficulty] — start a round (gta, anime, gaming, general, movies, music; "
            "easy, medium, hard) • !score — your points, XP, level, HP & streak • "
            "!leaderboard — top players • !quizstop — mods stop the quizzes & pause auto-rounds • "
            "!quizresume — mods resume auto-rounds • "
            "!fight @user — challenge someone to a quiz battle • !accept / !decline — answer a challenge • "
            "!ability <name> — use a banked power in battle (answer questions to bank powers!) • !abilities — your powers • "
            "!battle — current battle status • Mention Athena to chat with her."
        )

    # -- battles -----------------------------------------------------------

    @commands.command()
    async def fight(self, ctx: commands.Context, target: str | None = None) -> None:
        """Challenge someone to a quiz battle. Usage: !fight @user"""
        if self.quiz.active is not None and not self.quiz.active.expired:
            await ctx.reply("A quiz round is running — wait for it to end, then !fight.")
            return
        if self.quiz.battle.active is not None:
            await ctx.reply("A battle is already running — wait for it to end.")
            return
        if target is None:
            await ctx.reply("Usage: !fight @user — challenge someone to a quiz battle!")
            return
        target = target.lstrip("@").lower()
        if not target:
            await ctx.reply("Usage: !fight @user")
            return
        if target == self.bot.config.channel_login.lower():
            await ctx.reply("You can't fight the boss — that's Chris (and the bot). Pick a chatter!")
            return
        self.quiz.battle.expire_pending()
        result = self.quiz.battle.challenge(ctx.chatter.name, target)
        if not result["ok"]:
            await ctx.reply(result["reason"])
            return
        await ctx.send(
            f"⚔️ @{ctx.chatter.name} challenged @{target} to a quiz battle! "
            f"@{target}, type !accept (60s) or !decline."
        )

    @commands.command()
    async def accept(self, ctx: commands.Context) -> None:
        """Accept a pending battle challenge. Usage: !accept"""
        self.quiz.battle.expire_pending()
        if self.quiz.active is not None and not self.quiz.active.expired:
            await ctx.reply("A quiz round is running — wait for it to end, then !accept.")
            return
        result = self.quiz.battle.accept(ctx.chatter.name)
        if not result["ok"]:
            await ctx.reply(result["reason"])
            return
        battle = result["battle"]
        await ctx.send(
            f"⚔️ BATTLE! @{battle.challenger} (Lv{battle.level[battle.challenger]}, "
            f"{battle.hp[battle.challenger]} HP) vs @{battle.opponent} "
            f"(Lv{battle.level[battle.opponent]}, {battle.hp[battle.opponent]} HP) — "
            f"first correct answer hits, wrong answers hurt you. {BATTLE_MAX_ROUNDS} rounds max!"
        )
        await self.bot._post_battle_question(battle)

    @commands.command()
    async def decline(self, ctx: commands.Context) -> None:
        """Decline a pending battle challenge. Usage: !decline"""
        self.quiz.battle.expire_pending()
        result = self.quiz.battle.decline(ctx.chatter.name)
        if not result["ok"]:
            await ctx.reply(result["reason"])
            return
        await ctx.send(f"🙅 @{ctx.chatter.name} declined the challenge.")

    @commands.command()
    async def ability(self, ctx: commands.Context, name: str | None = None) -> None:
        """Use an unlocked ability in your battle. Usage: !ability shield"""
        if name is None:
            await ctx.reply("Usage: !ability <name> — see !abilities for your unlocked ones.")
            return
        result = self.quiz.battle.use_ability(ctx.chatter.name, name)
        if not result["ok"]:
            await ctx.reply(result["reason"])
            return
        for event in result["events"]:
            await self.bot.send_chat(event["text"])
        if result["new_question"] is not None:
            # Reroll swapped the question: post it and restart the timer.
            # (The old question's timeout task sees the new round token
            # and stands down on its own.)
            battle = self.quiz.battle.active
            question = result["new_question"]
            await self.bot.send_chat(
                f"🎲 FRESH QUESTION [{question.category} • {question.difficulty}] "
                f"({BATTLE_ROUND_SECONDS}s): {question.question} — fighters only!"
            )
            asyncio.create_task(
                self.bot._battle_round_timeout(battle, battle.round_token)
            )

    @commands.command()
    async def abilities(self, ctx: commands.Context) -> None:
        """Show your banked powers and the full power list. Usage: !abilities"""
        stats = self.quiz.scores.get_stats(ctx.chatter.name)
        bank = stats.get("bank", [])
        banked = {row["power"]: row["count"] for row in bank}
        bank_total = sum(banked.values())
        parts = []
        for power_id, info in POWERS.items():
            count = banked.get(power_id, 0)
            mark = f"⚡ x{count}" if count else "—"
            parts.append(
                f"{mark} {power_id} ({info['name']} — {info['character']}): {info['desc']} "
                f"— earn: {info['earn']}"
            )
        await ctx.reply(
            f"@{ctx.chatter.name}'s powers (banked {bank_total}/{MAX_BANKED}): "
            + " | ".join(parts)
        )

    @commands.command()
    async def battle(self, ctx: commands.Context) -> None:
        """Show the current battle status. Usage: !battle"""
        manager = self.quiz.battle
        if manager.active is not None and manager.active.status == "active":
            b = manager.active
            await ctx.reply(
                f"⚔️ BATTLE Round {b.round_no}/{BATTLE_MAX_ROUNDS}: @{b.challenger} "
                f"{b.hp[b.challenger]}/{b.max_hp_map[b.challenger]} HP vs "
                f"@{b.opponent} {b.hp[b.opponent]}/{b.max_hp_map[b.opponent]} HP."
            )
        elif manager.pending is not None:
            await ctx.reply(
                f"⚔️ @{manager.pending.challenger} challenged @{manager.pending.opponent} "
                "— waiting for !accept."
            )
        else:
            await ctx.reply("No battle running. Type !fight @user to start one!")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def setup_database(db: asqlite.Pool, config: Config) -> tuple[list[tuple[str, str]], list[eventsub.SubscriptionPayload]]:
    query = "CREATE TABLE IF NOT EXISTS tokens(user_id TEXT PRIMARY KEY, token TEXT NOT NULL, refresh TEXT NOT NULL)"
    async with db.acquire() as connection:
        await connection.execute(query)
        rows: list[_sqlite3.Row] = await connection.fetchall("SELECT * FROM tokens")
        tokens: list[tuple[str, str]] = [(row["token"], row["refresh"]) for row in rows]

    subs: list[eventsub.SubscriptionPayload] = []
    # First run: tokens.db is empty but .env holds the tokens — the
    # subscription must be built anyway, or the bot starts with 0
    # subscriptions and never sees chat (found live 2026-11-19: the bot
    # logged in fine but !quizhelp did nothing).
    if tokens or config.access_token:
        # Bot == broadcaster here: subscribe to his own channel's chat.
        subs.append(eventsub.ChatMessageSubscription(
            broadcaster_user_id=config.owner_id, user_id=config.bot_id
        ))
    return tokens, subs


def main() -> None:
    twitchio.utils.setup_logging(level=logging.INFO)
    config = Config.from_env()
    problems = config.validate()
    if problems:
        for p in problems:
            LOGGER.error("Config problem: %s", p)
        LOGGER.error("Fill in .env (copy config.example) — see README.md. Exiting.")
        raise SystemExit(1)

    config.data_dir.mkdir(parents=True, exist_ok=True)
    questions = load_questions(config.questions_path)
    scores = ScoreStore(config.data_dir / "scores.db")
    quiz = QuizManager(questions, scores, timeout_seconds=config.question_timeout_seconds)
    brain = AthenaBrain(config.ollama_url, config.ollama_model)
    LOGGER.info("Loaded %d questions across %d categories.", len(questions), len(set(q.category for q in questions)))

    async def runner() -> None:
        overlay: OverlayServer | None = None
        if config.overlay_enabled:
            overlay = OverlayServer(quiz, host="0.0.0.0", port=config.overlay_port)
            try:
                await overlay.start()
                LOGGER.info("Quiz overlay live at %s (OBS browser source)", overlay.url)
            except Exception:
                LOGGER.warning("Overlay failed to start — bot continues without it", exc_info=True)
                overlay = None
        try:
            async with asqlite.create_pool(str(config.data_dir / "tokens.db")) as tdb:
                tokens, subs = await setup_database(tdb, config)
                async with QuizBot(config=config, quiz=quiz, brain=brain, token_database=tdb, subs=subs) as bot:
                    if not tokens and config.access_token and config.refresh_token:
                        # First run: seed from .env; add_token() persists to tokens.db.
                        await bot.add_token(config.access_token, config.refresh_token)
                        tokens = [(config.access_token, config.refresh_token)]
                    for pair in tokens:
                        await bot.add_token(*pair)
                    await bot.start(load_tokens=False, with_adapter=config.with_adapter)
        finally:
            if overlay is not None:
                await overlay.stop()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        LOGGER.warning("Shutting down (Ctrl+C)")
    finally:
        scores.close()


if __name__ == "__main__":
    main()
