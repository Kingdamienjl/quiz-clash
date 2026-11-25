"""RPG battle layer for the kingdamienjl Twitch quiz bot.

Pure logic, no TwitchIO / network imports — unit-testable offline, same
pattern as quiz.py. The bot (bot.py) wires this into chat; the overlay
(overlay.py) reads BattleManager.state_for_overlay().

The idea: XP and levels already exist, so battles give them teeth.

HP
--
A fighter's max HP scales with level: 100 HP at level 1, +10 per level
(``max_hp = 100 + 10 * (level - 1)``). HP is derived from level, never
stored — it is set when a battle starts and only lives for that battle.

Battles
-------
``!fight @user`` challenges someone. The challenged player has 60 seconds
to ``!accept`` (or ``!decline``). One battle at a time, for the whole
channel — a quiz round and a battle can never run at once.

A battle is up to 10 rounds. Each round the bot asks a question from the
normal bank (any category, any difficulty). Both fighters answer in chat:

* First correct answer deals damage to the other fighter. Damage scales
  with the question's difficulty (easy 10 / medium 15 / hard 20) plus the
  attacker's level (+1 per level). A level 1 fighter answering an easy
  question hits for 11; a level 10 fighter answering a hard one hits for 30.
* A wrong answer costs the answerer: half the question's base damage to
  themselves (easy 5 / medium 7 / hard 10). Each fighter gets ONE guess per
  round — after a wrong guess they are locked out for that round. This is
  the anti-spam rule: you cannot machine-gun guesses.
* If nobody answers in 30 seconds, the round fizzles (no damage).
* If both fighters guess wrong, the round ends early (no waiting out the
  timer).

The battle ends when a fighter hits 0 HP (the other wins), or after 10
rounds (higher HP wins; equal HP is a draw). Winner takes 50 XP, loser
takes 10 XP, a draw is 25 XP each, and the winner's battle-win count goes
up by 1. After a battle both fighters are on a 5-minute cooldown before
they can fight again.

Themed powers
-------------
Powers are the characters themselves (see powers.py — the single catalog).
Answer a question about The Flash, bank Flash Speed; answer about Goku,
bank Super Saiyan Strength. Every correct quiz answer banks its
question's power (a specific character tag, or the category's fallback
power), up to 3 banked at a time. Milestones bank powers too: level 3 ->
Vibranium Guard, 5 battle wins -> Super Saiyan Strength, best streak 5 ->
Senzu Bean, category mastery -> Shinigami Drain, 25 points -> DeLorean
Rewind.

In battle, ``!ability <power>`` spends one banked power, once per battle
each. Old generic names still work as aliases (shield -> guard,
power -> strength, reroll -> rewind).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable

from powers import ALIASES, POWERS, RAGE_BONUS, REVIVE_HP
from quiz import Question, ScoreStore, is_correct

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

BASE_HP = 100
HP_PER_LEVEL = 10

BASE_DAMAGE: dict[str, int] = {"easy": 10, "medium": 15, "hard": 20}
WINNER_XP = 50
LOSER_XP = 10
DRAW_XP = 25

BATTLE_MAX_ROUNDS = 10
BATTLE_ROUND_SECONDS = 30
CHALLENGE_EXPIRY_SECONDS = 60
BATTLE_COOLDOWN_SECONDS = 300

HEAL_AMOUNT = 30
DRAIN_DAMAGE_BONUS = 10
DRAIN_HEAL_AMOUNT = 10
POWER_MULTIPLIER = 2


def max_hp(level: int) -> int:
    """Max HP for a level: 100 at level 1, +10 per level after."""
    return BASE_HP + HP_PER_LEVEL * (max(1, level) - 1)


def damage_for(difficulty: str, attacker_level: int) -> int:
    """Damage a correct answer deals: difficulty base + attacker level."""
    return BASE_DAMAGE.get(difficulty, BASE_DAMAGE["easy"]) + max(1, attacker_level)


def self_damage_for(difficulty: str) -> int:
    """Damage a wrong answer deals to the answerer: half the base."""
    return BASE_DAMAGE.get(difficulty, BASE_DAMAGE["easy"]) // 2


# ---------------------------------------------------------------------------
# Battle state
# ---------------------------------------------------------------------------


@dataclass
class Battle:
    challenger: str
    opponent: str
    status: str = "pending"  # pending | active | done
    hp: dict[str, int] = field(default_factory=dict)
    max_hp_map: dict[str, int] = field(default_factory=dict)
    level: dict[str, int] = field(default_factory=dict)
    question: Question | None = None
    round_no: int = 0
    rounds_played: int = 0
    round_token: int = 0  # increments per question; lets the bot spot stale timeouts
    spent: set[str] = field(default_factory=set)  # fighters locked out this round
    # Armed power effects (one flag per fighter, consumed when they fire):
    guard_on: dict[str, bool] = field(default_factory=dict)      # negate next damage taken
    strength_on: dict[str, bool] = field(default_factory=dict)   # next hit deals double
    drain_on: dict[str, bool] = field(default_factory=dict)      # next hit +10 dmg, heal 10
    speed_on: dict[str, bool] = field(default_factory=dict)      # next wrong guess doesn't lock out
    dodge_on: dict[str, bool] = field(default_factory=dict)      # next wrong guess costs 0
    rage_on: dict[str, bool] = field(default_factory=dict)        # next hit +15 damage
    bounce_on: dict[str, bool] = field(default_factory=dict)     # next damage taken is halved
    hexed: dict[str, bool] = field(default_factory=dict)          # next answer counts as wrong (cast on you)
    revive_on: dict[str, bool] = field(default_factory=dict)     # revive at 25 HP if you hit 0
    used_abilities: dict[str, set[str]] = field(default_factory=dict)
    winner: str | None = None
    draw: bool = False
    created_at: float = 0.0

    def fighters(self) -> list[str]:
        return [self.challenger, self.opponent]

    def other(self, username: str) -> str:
        return self.opponent if username == self.challenger else self.challenger


@dataclass
class _Event:
    text: str
    kind: str  # hit | self | win | info | a power id (speed, guard, ...)
    at: float


class BattleManager:
    """One battle at a time for the whole channel.

    ``now`` is injectable so tests can control the clock; it defaults to
    time.monotonic (seconds).
    """

    def __init__(
        self,
        questions: list[Question],
        scores: ScoreStore,
        now: Callable[[], float] | None = None,
    ) -> None:
        if not questions:
            raise ValueError("BattleManager needs at least one question")
        self.questions = questions
        self.scores = scores
        self._now = now or time.monotonic
        self.pending: Battle | None = None
        self.active: Battle | None = None
        self.last_battle: Battle | None = None  # finished battle, shown briefly on the overlay
        self.cooldowns: dict[str, float] = {}  # username -> monotonic expiry
        self.feed: list[_Event] = []  # battle announcements, newest last

    # -- helpers -----------------------------------------------------------

    def now(self) -> float:
        return self._now()

    def _announce(self, text: str, kind: str = "info") -> None:
        self.feed.append(_Event(text=text, kind=kind, at=time.time()))
        self.feed = self.feed[-12:]

    def on_cooldown(self, username: str) -> bool:
        return self.cooldowns.get(username.lower(), 0.0) > self.now()

    def cooldown_left(self, username: str) -> int:
        return max(0, int(self.cooldowns.get(username.lower(), 0.0) - self.now()))

    def _involved(self, username: str) -> bool:
        user = username.lower()
        for battle in (self.pending, self.active):
            if battle is not None and user in battle.fighters():
                return True
        return False

    def _pick_question(self, exclude: Question | None = None) -> Question:
        pool = [q for q in self.questions if q is not exclude]
        return random.choice(pool or self.questions)

    # -- challenge flow ----------------------------------------------------

    def challenge(self, challenger: str, opponent: str) -> dict:
        """``!fight @user``. Returns {ok, reason, battle}."""
        chal, opp = challenger.lower(), opponent.lower()
        if chal == opp:
            return {"ok": False, "reason": "You can't fight yourself.", "battle": None}
        if self._involved(chal):
            return {"ok": False, "reason": "You are already in a battle or challenge.", "battle": None}
        if self._involved(opp):
            return {"ok": False, "reason": f"@{opp} is already in a battle or challenge.", "battle": None}
        if self.on_cooldown(chal):
            return {"ok": False, "reason": f"You must wait {self.cooldown_left(chal)}s before your next battle.", "battle": None}
        if self.on_cooldown(opp):
            return {"ok": False, "reason": f"@{opp} must wait {self.cooldown_left(opp)}s before their next battle.", "battle": None}
        battle = Battle(challenger=chal, opponent=opp, created_at=self.now())
        self.pending = battle
        self._announce(f"⚔️ @{chal} challenged @{opp} to a quiz battle! @{opp}, type !accept or !decline.", "info")
        return {"ok": True, "reason": None, "battle": battle}

    def expire_pending(self) -> Battle | None:
        """Drop a challenge that sat unanswered past the expiry. Returns it or None."""
        if self.pending is not None and (self.now() - self.pending.created_at) > CHALLENGE_EXPIRY_SECONDS:
            expired = self.pending
            self.pending = None
            self._announce(f"⌛ @{expired.challenger}'s challenge to @{expired.opponent} expired.", "info")
            return expired
        return None

    def accept(self, username: str) -> dict:
        """``!accept`` — only the challenged player can accept."""
        user = username.lower()
        if self.pending is None:
            return {"ok": False, "reason": "No challenge is waiting for you.", "battle": None}
        if self.pending.opponent != user:
            return {"ok": False, "reason": "That challenge isn't for you.", "battle": None}
        battle = self.pending
        self.pending = None
        battle.status = "active"
        for fighter in battle.fighters():
            level = self.scores.get_stats(fighter)["level"]
            battle.level[fighter] = level
            battle.max_hp_map[fighter] = max_hp(level)
            battle.hp[fighter] = battle.max_hp_map[fighter]
            for flag in ("guard_on", "strength_on", "drain_on", "speed_on",
                         "dodge_on", "rage_on", "bounce_on", "hexed", "revive_on"):
                getattr(battle, flag)[fighter] = False
            battle.used_abilities[fighter] = set()
        self.active = battle
        self._announce(
            f"⚔️ BATTLE! @{battle.challenger} (Lv{battle.level[battle.challenger]}, "
            f"{battle.hp[battle.challenger]} HP) vs @{battle.opponent} "
            f"(Lv{battle.level[battle.opponent]}, {battle.hp[battle.opponent]} HP) — "
            f"first to 0 HP loses, {BATTLE_MAX_ROUNDS} rounds max!",
            "info",
        )
        return {"ok": True, "reason": None, "battle": battle}

    def decline(self, username: str) -> dict:
        """``!decline`` — only the challenged player can decline."""
        user = username.lower()
        if self.pending is None:
            return {"ok": False, "reason": "No challenge is waiting for you.", "battle": None}
        if self.pending.opponent != user:
            return {"ok": False, "reason": "That challenge isn't for you.", "battle": None}
        battle = self.pending
        self.pending = None
        self._announce(f"🙅 @{user} declined @{battle.challenger}'s challenge.", "info")
        return {"ok": True, "reason": None, "battle": battle}

    # -- rounds ------------------------------------------------------------

    def next_question(self, battle: Battle) -> Question:
        """Post the next battle question. Returns it."""
        battle.question = self._pick_question(exclude=battle.question)
        battle.round_no += 1
        battle.round_token += 1
        battle.spent = set()
        return battle.question

    def _check_end(self, battle: Battle) -> dict | None:
        """End the battle if someone is at 0 HP or rounds ran out.

        A fighter with a 1-Up Mushroom armed revives at REVIVE_HP instead
        of losing, and the battle continues. Returns the end result dict,
        or None if the battle continues. Awards XP, records the win, and
        sets both fighters' cooldowns.
        """
        chal, opp = battle.challenger, battle.opponent
        # Revive first: a 1-Up saves its owner from the KO.
        for fighter in battle.fighters():
            if battle.hp[fighter] <= 0 and battle.revive_on.get(fighter):
                battle.revive_on[fighter] = False
                battle.hp[fighter] = REVIVE_HP
                self._announce(
                    f"🍄 @{fighter}'s 1-UP MUSHROOM kicks in — revived at {REVIVE_HP} HP!",
                    "revive",
                )
        winner: str | None = None
        draw = False
        if battle.hp[chal] <= 0 and battle.hp[opp] <= 0:
            draw = True
        elif battle.hp[chal] <= 0:
            winner = opp
        elif battle.hp[opp] <= 0:
            winner = chal
        elif battle.rounds_played >= BATTLE_MAX_ROUNDS:
            if battle.hp[chal] > battle.hp[opp]:
                winner = chal
            elif battle.hp[opp] > battle.hp[chal]:
                winner = opp
            else:
                draw = True
        if winner is None and not draw:
            return None
        battle.status = "done"
        battle.winner = winner
        battle.draw = draw
        for fighter in battle.fighters():
            self.cooldowns[fighter] = self.now() + BATTLE_COOLDOWN_SECONDS
        if draw:
            self.scores.award_xp(chal, DRAW_XP)
            self.scores.award_xp(opp, DRAW_XP)
            self._announce(
                f"🤝 DRAW! @{chal} and @{opp} both walk away with +{DRAW_XP} XP.",
                "win",
            )
        else:
            loser = battle.other(winner)  # type: ignore[arg-type]
            self.scores.award_xp(winner, WINNER_XP)  # type: ignore[arg-type]
            self.scores.award_xp(loser, LOSER_XP)
            self.scores.add_battle_win(winner)  # type: ignore[arg-type]
            self._announce(
                f"🏆 @{winner} WINS the battle! +{WINNER_XP} XP (and @{loser} takes +{LOSER_XP} XP).",
                "win",
            )
        self.active = None
        self.last_battle = battle  # overlay keeps showing the result briefly
        return {"battle_over": True, "winner": winner, "draw": draw}

    def answer(self, username: str, text: str) -> dict:
        """A fighter's chat message during an active battle round.

        Returns {status, events, battle_over, winner, draw, battle}.
        Statuses: no-battle | not-fighter | no-question | spent | hit |
        self | fizzle.
        """
        battle = self.active
        if battle is None or battle.status != "active":
            return {"status": "no-battle", "events": [], "battle_over": False, "winner": None, "draw": False, "battle": None}
        user = username.lower()
        if user not in battle.fighters():
            return {"status": "not-fighter", "events": [], "battle_over": False, "winner": None, "draw": False, "battle": battle}
        if battle.question is None:
            return {"status": "no-question", "events": [], "battle_over": False, "winner": None, "draw": False, "battle": battle}
        if user in battle.spent:
            # Flash Speed: a second try in the same round instead of a lockout.
            if battle.speed_on.get(user):
                battle.speed_on[user] = False
                battle.spent.discard(user)
            else:
                return {"status": "spent", "events": [], "battle_over": False, "winner": None, "draw": False, "battle": battle}

        question = battle.question
        defender = battle.other(user)
        events: list[dict] = []

        def emit(text: str, kind: str = "info") -> None:
            events.append({"text": text, "kind": kind})
            self._announce(text, kind)

        # Genjutsu: a hexed fighter's answer counts as wrong, right or not.
        hexed = battle.hexed.get(user, False)
        if hexed:
            battle.hexed[user] = False
            emit(f"🌀 @{user} is caught in GENJUTSU — their answer counts as wrong!", "genjutsu")

        if (not hexed) and is_correct(text, question):
            damage = damage_for(question.difficulty, battle.level[user])
            if battle.strength_on[user]:
                damage *= POWER_MULTIPLIER
                battle.strength_on[user] = False
                emit(f"💥 @{user} unleashes SUPER SAIYAN STRENGTH — double damage!", "strength")
            if battle.rage_on[user]:
                damage += RAGE_BONUS
                battle.rage_on[user] = False
                emit(f"😤 @{user} unleashes TITAN RAGE — +{RAGE_BONUS} damage!", "rage")
            if battle.drain_on[user]:
                damage += DRAIN_DAMAGE_BONUS
                healed = min(DRAIN_HEAL_AMOUNT, battle.max_hp_map[user] - battle.hp[user])
                battle.hp[user] += healed
                battle.drain_on[user] = False
                emit(f"🧛 @{user} drains {DRAIN_DAMAGE_BONUS} HP from @{defender} and heals {healed} HP!", "drain")
            if battle.guard_on[defender]:
                battle.guard_on[defender] = False
                emit(f"🛡️ @{defender}'s VIBRANIUM GUARD blocks the hit from @{user}!", "guard")
                damage = 0
            else:
                if battle.bounce_on[defender]:
                    battle.bounce_on[defender] = False
                    damage = max(1, damage // 2)
                    emit(f"🎈 @{defender}'s GUM-GUM BOUNCE halves the hit!", "bounce")
                battle.hp[defender] = max(0, battle.hp[defender] - damage)
            emit(
                f"⚔️ @{user} answered right and hit @{defender} for {damage} damage! "
                f"(@{defender}: {battle.hp[defender]}/{battle.max_hp_map[defender]} HP)",
                "hit",
            )
            battle.rounds_played += 1
            end = self._check_end(battle)
            return {
                "status": "hit",
                "events": events,
                "battle_over": end is not None,
                "winner": (end or {}).get("winner"),
                "draw": (end or {}).get("draw", False),
                "battle": battle,
            }

        # Wrong answer: self-damage, and this fighter is spent for the round.
        self_dmg = self_damage_for(question.difficulty)
        if battle.dodge_on.get(user):
            battle.dodge_on[user] = False
            self_dmg = 0
            emit(f"🏎️ @{user}'s GETAWAY DRIVER dodges the damage!", "dodge")
        battle.hp[user] = max(0, battle.hp[user] - self_dmg)
        battle.spent.add(user)
        emit(
            f"💢 @{user} answered wrong and hurt themselves for {self_dmg} damage! "
            f"(@{user}: {battle.hp[user]}/{battle.max_hp_map[user]} HP)",
            "self",
        )
        end = self._check_end(battle)
        if end is not None:
            return {"status": "self", "events": events, "battle_over": True,
                    "winner": end.get("winner"), "draw": end.get("draw", False), "battle": battle}
        if len(battle.spent) >= 2:
            # Both fighters guessed wrong: the round fizzles, move on.
            battle.rounds_played += 1
            emit("😶 Both fighters missed — next question!", "info")
            end = self._check_end(battle)
            return {"status": "fizzle", "events": events, "battle_over": end is not None,
                    "winner": (end or {}).get("winner"), "draw": (end or {}).get("draw", False), "battle": battle}
        return {"status": "self", "events": events, "battle_over": False,
                "winner": None, "draw": False, "battle": battle}

    def timeout_round(self) -> dict:
        """The active battle question timed out with no correct answer."""
        battle = self.active
        if battle is None or battle.status != "active" or battle.question is None:
            return {"status": "no-battle", "events": [], "battle_over": False, "winner": None, "draw": False, "battle": None}
        events: list[dict] = []
        text = f"⏰ Time! Nobody got it — the answer was: {battle.question.answer}"
        events.append({"text": text, "kind": "info"})
        self._announce(text, "info")
        battle.rounds_played += 1
        end = self._check_end(battle)
        return {"status": "timeout", "events": events, "battle_over": end is not None,
                "winner": (end or {}).get("winner"), "draw": (end or {}).get("draw", False), "battle": battle}

    # -- powers ------------------------------------------------------------

    def use_ability(self, username: str, ability_id: str) -> dict:
        """``!ability <name>`` — spend one banked power in your battle.

        Returns {ok, reason, events, new_question}. The power must be
        banked (earned by answering questions about its character, or a
        milestone) and unused in this battle. Old generic names work as
        aliases (shield -> guard, power -> strength, reroll -> rewind).
        """
        battle = self.active
        if battle is None or battle.status != "active":
            return {"ok": False, "reason": "No battle is running right now.", "events": [], "new_question": None}
        user = username.lower()
        if user not in battle.fighters():
            return {"ok": False, "reason": "You are not in this battle.", "events": [], "new_question": None}
        ability_id = ALIASES.get(ability_id.lower(), ability_id.lower())
        if ability_id not in POWERS:
            return {"ok": False, "reason": f"Unknown power {ability_id!r}. Try !abilities to see yours.", "events": [], "new_question": None}
        info = POWERS[ability_id]
        if ability_id in battle.used_abilities[user]:
            return {"ok": False, "reason": f"You already used {info['name']} in this battle.", "events": [], "new_question": None}
        if not self.scores.consume_power(user, ability_id):
            return {
                "ok": False,
                "reason": f"You don't have {info['name']} banked — {info['earn']}.",
                "events": [],
                "new_question": None,
            }
        battle.used_abilities[user].add(ability_id)
        events: list[dict] = []

        def emit(text: str, kind: str) -> None:
            events.append({"text": text, "kind": kind})
            self._announce(text, kind)

        new_question: Question | None = None
        effect = info["effect"]
        if effect == "guard":
            battle.guard_on[user] = True
            emit(f"🛡️ @{user} raises VIBRANIUM GUARD — the next hit will be blocked!", "guard")
        elif effect == "strength":
            battle.strength_on[user] = True
            emit(f"💥 @{user} charges SUPER SAIYAN STRENGTH — their next correct answer deals double damage!", "strength")
        elif effect == "heal":
            healed = min(HEAL_AMOUNT, battle.max_hp_map[user] - battle.hp[user])
            battle.hp[user] += healed
            emit(f"🫘 @{user} eats a SENZU BEAN and heals {healed} HP! (@{user}: {battle.hp[user]}/{battle.max_hp_map[user]} HP)", "heal")
        elif effect == "drain":
            battle.drain_on[user] = True
            emit(f"🧛 @{user} readies SHINIGAMI DRAIN — their next correct answer steals HP!", "drain")
        elif effect == "rewind":
            new_question = self.next_question(battle)
            emit(f"⏪ @{user} uses DELOREAN REWIND — new question!", "rewind")
        elif effect == "speed":
            battle.speed_on[user] = True
            emit(f"⚡ @{user} uses FLASH SPEED — their next wrong guess won't lock them out!", "speed")
        elif effect == "dodge":
            battle.dodge_on[user] = True
            emit(f"🏎️ @{user} is ready with the GETAWAY DRIVER — their next wrong answer costs nothing!", "dodge")
        elif effect == "rage":
            battle.rage_on[user] = True
            emit(f"😤 @{user} builds TITAN RAGE — their next correct answer deals +{RAGE_BONUS} damage!", "rage")
        elif effect == "bounce":
            battle.bounce_on[user] = True
            emit(f"🎈 @{user} stretches out with GUM-GUM BOUNCE — the next damage they take is halved!", "bounce")
        elif effect == "genjutsu":
            target = battle.other(user)
            battle.hexed[target] = True
            emit(f"🌀 @{user} casts GENJUTSU on @{target} — their next answer will count as wrong!", "genjutsu")
        elif effect == "revive":
            battle.revive_on[user] = True
            emit(f"🍄 @{user} holds a 1-UP MUSHROOM — if they hit 0 HP, they'll revive at {REVIVE_HP} HP!", "revive")
        return {"ok": True, "reason": None, "events": events, "new_question": new_question}

    # -- overlay -----------------------------------------------------------

    def state_for_overlay(self) -> dict | None:
        """JSON-able battle snapshot for /state.json, or None when idle.

        A finished battle keeps showing until its last announcement goes
        stale (30 s), so the winner banner is visible on stream.
        """
        battle = self.active or self.pending
        if battle is None and self.last_battle is not None:
            last_event_at = self.feed[-1].at if self.feed else 0.0
            if (time.time() - last_event_at) < 30:
                battle = self.last_battle
            else:
                self.last_battle = None
        if battle is None:
            return None
        now = time.time()
        return {
            "status": battle.status,
            "challenger": battle.challenger,
            "opponent": battle.opponent,
            "hp": dict(battle.hp),
            "max_hp": dict(battle.max_hp_map),
            "level": dict(battle.level),
            "bank": {
                fighter: self.scores.get_bank(fighter)
                for fighter in battle.fighters()
            },
            "round": battle.round_no,
            "max_rounds": BATTLE_MAX_ROUNDS,
            "rounds_played": battle.rounds_played,
            "question": battle.question.question if battle.question else None,
            "winner": battle.winner,
            "draw": battle.draw,
            "feed": [
                {"text": e.text, "kind": e.kind, "at": e.at}
                for e in self.feed
                if (now - e.at) < 30
            ][-6:],
        }
