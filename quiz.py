"""Pure quiz logic for the kingdamienjl Twitch quiz bot.

No TwitchIO / network imports here on purpose: this module is unit-testable
offline. The bot (bot.py) wires this logic into chat.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from powers import (
    MAX_BANKED,
    MILESTONE_LEVEL,
    MILESTONE_MASTERY,
    MILESTONE_POINTS,
    MILESTONE_STREAK,
    MILESTONE_WINS,
    POWERS,
    power_for_question,
)

# ---------------------------------------------------------------------------
# Question bank
# ---------------------------------------------------------------------------

CATEGORIES: tuple[str, ...] = ("gta", "anime", "gaming", "general", "movies", "music")

# ---------------------------------------------------------------------------
# Difficulty + XP / levels — tunable here. A correct answer earns XP by
# difficulty; points still count correct answers (1 per win).
# Level curve: level n needs 100 * (n - 1) total XP (level 1 at 0 XP,
# level 2 at 100, level 3 at 200, ...). Level is derived from XP, never
# stored separately, so the curve can change without a migration.
# ---------------------------------------------------------------------------

XP_EASY = 10
XP_MEDIUM = 25
XP_HARD = 50
XP_PER_LEVEL = 100

DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")
XP_FOR_DIFFICULTY: dict[str, int] = {"easy": XP_EASY, "medium": XP_MEDIUM, "hard": XP_HARD}

DIFFICULTY_ALIASES: dict[str, str] = {
    "easy": "easy",
    "e": "easy",
    "medium": "medium",
    "med": "medium",
    "m": "medium",
    "hard": "hard",
    "h": "hard",
}


def level_for_xp(xp: int) -> int:
    """Level from total XP. Level n needs XP_PER_LEVEL * (n - 1) total XP."""
    return max(1, xp // XP_PER_LEVEL + 1)


def xp_into_level(xp: int) -> int:
    """XP earned toward the current level (0 .. XP_PER_LEVEL - 1)."""
    return xp % XP_PER_LEVEL


def xp_to_next_level(xp: int) -> int:
    """XP still needed to reach the next level."""
    return XP_PER_LEVEL - (xp % XP_PER_LEVEL)

# Short aliases viewers may type after !quiz, e.g. "!quiz gta".
CATEGORY_ALIASES: dict[str, str] = {
    "gta": "gta",
    "grand theft auto": "gta",
    "anime": "anime",
    "gaming": "gaming",
    "games": "gaming",
    "general": "general",
    "general knowledge": "general",
    "trivia": "general",
    "movies": "movies",
    "movie": "movies",
    "tv": "movies",
    "movies/tv": "movies",
    "music": "music",
}

_ARTICLES = {"a", "an", "the"}
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, drop leading articles, collapse spaces."""
    text = text.lower().strip()
    text = _PUNCT.sub(" ", text)
    words = [w for w in _SPACES.split(text) if w]
    while words and words[0] in _ARTICLES:
        words.pop(0)
    return " ".join(words)


@dataclass
class Question:
    question: str
    answer: str
    alternates: list[str] = field(default_factory=list)
    category: str = "general"
    difficulty: str = "easy"
    # Themed power this question grants when answered right ("" = the
    # category's fallback power — see powers.power_for_question).
    power: str = ""

    def accepted_answers(self) -> list[str]:
        return [self.answer, *self.alternates]

    @property
    def xp_value(self) -> int:
        """XP a correct answer to this question is worth."""
        return XP_FOR_DIFFICULTY.get(self.difficulty, XP_EASY)


def load_questions(path: str | Path) -> list[Question]:
    """Load and validate questions.json. Raises ValueError on bad data."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    categories = set(data.get("categories", []))
    if not categories:
        raise ValueError("questions file has no categories list")
    unknown = categories - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unknown categories in file: {sorted(unknown)}")

    questions: list[Question] = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("questions", [])):
        q = raw.get("question", "").strip()
        a = raw.get("answer", "").strip()
        cat = raw.get("category", "").strip().lower()
        diff = str(raw.get("difficulty", "easy")).strip().lower()
        alts = raw.get("alternates", []) or []
        power = str(raw.get("power", "") or "").strip().lower()
        if not q or not a or not cat:
            raise ValueError(f"question #{i} is missing question/answer/category")
        if cat not in CATEGORIES:
            raise ValueError(f"question #{i} has unknown category {cat!r}")
        if diff not in DIFFICULTIES:
            raise ValueError(f"question #{i} has unknown difficulty {diff!r}")
        if power and power not in POWERS:
            raise ValueError(f"question #{i} has unknown power {power!r}")
        if not isinstance(alts, list) or any(not isinstance(x, str) for x in alts):
            raise ValueError(f"question #{i} has bad alternates")
        key = normalize(q)
        if key in seen:
            raise ValueError(f"duplicate question: {q!r}")
        seen.add(key)
        questions.append(Question(question=q, answer=a, alternates=alts, category=cat, difficulty=diff, power=power))
    if not questions:
        raise ValueError("questions file has no questions")
    return questions


def is_correct(user_text: str, question: Question) -> bool:
    """True when the viewer's text matches the answer or any alternate."""
    given = normalize(user_text)
    if not given:
        return False
    return any(given == normalize(ok) for ok in question.accepted_answers())


def resolve_category(text: str | None) -> str | None:
    """Map a viewer's category word to a canonical category, or None."""
    if not text:
        return None
    return CATEGORY_ALIASES.get(normalize(text))


def resolve_difficulty(text: str | None) -> str | None:
    """Map a viewer's difficulty word to easy/medium/hard, or None."""
    if not text:
        return None
    return DIFFICULTY_ALIASES.get(normalize(text))


# ---------------------------------------------------------------------------
# Scores (SQLite, survives restarts)
# ---------------------------------------------------------------------------


class ScoreStore:
    """Persistent leaderboard. One row per username (lowercased).

    points = correct answers (1 per win). xp = XP earned, scaled by the
    difficulty of each question answered. Level is derived from xp, so it
    is never stored. Older databases are migrated in place (missing
    columns are added; existing rows keep their points, start at 0 XP).

    RPG tracking lives here too, so abilities can be derived from it:
    streak / best_streak (consecutive correct answers), battle_wins,
    and per-category correct counts in the category_stats table.

    Powers live here too: the power_bank table holds banked powers
    (username, power id, count). A correct answer banks its question's
    power (see powers.power_for_question), capped at MAX_BANKED total;
    battle use consumes one. Old databases get the table on open.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS scores (username TEXT PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0, xp INTEGER NOT NULL DEFAULT 0)"
        )
        # Migrate older databases: add any missing column.
        for column in (
            "xp INTEGER NOT NULL DEFAULT 0",
            "streak INTEGER NOT NULL DEFAULT 0",
            "best_streak INTEGER NOT NULL DEFAULT 0",
            "battle_wins INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                self._conn.execute(f"ALTER TABLE scores ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS category_stats "
            "(username TEXT NOT NULL, category TEXT NOT NULL, correct INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (username, category))"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS power_bank "
            "(username TEXT NOT NULL, power TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (username, power))"
        )
        self._conn.commit()

    # -- power bank ------------------------------------------------------

    def bank_total(self, username: str) -> int:
        """How many powers this player has banked in total."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM power_bank WHERE username = ?",
            (username.lower(),),
        ).fetchone()
        return row[0] if row else 0

    def get_bank(self, username: str) -> list[dict]:
        """Banked powers: [{power, name, count}] sorted by power id."""
        rows = self._conn.execute(
            "SELECT power, count FROM power_bank WHERE username = ? AND count > 0 ORDER BY power",
            (username.lower(),),
        ).fetchall()
        return [
            {"power": p, "name": POWERS[p]["name"], "count": c}
            for p, c in rows if p in POWERS
        ]

    def bank_power(self, username: str, power_id: str) -> dict:
        """Bank one power for a player. Returns {granted, reason, bank}.

        The bank caps at MAX_BANKED total — when full, the grant is lost
        (reason "bank-full") rather than silently overflowing.
        """
        user = username.lower()
        if power_id not in POWERS:
            return {"granted": False, "reason": "unknown-power", "bank": self.get_bank(user)}
        if self.bank_total(user) >= MAX_BANKED:
            return {"granted": False, "reason": "bank-full", "bank": self.get_bank(user)}
        self._conn.execute(
            "INSERT INTO power_bank (username, power, count) VALUES (?, ?, 1) "
            "ON CONFLICT(username, power) DO UPDATE SET count = count + 1",
            (user, power_id),
        )
        self._conn.commit()
        return {"granted": True, "reason": None, "bank": self.get_bank(user)}

    def consume_power(self, username: str, power_id: str) -> bool:
        """Spend one banked power. True when one was there to spend."""
        user = username.lower()
        row = self._conn.execute(
            "SELECT count FROM power_bank WHERE username = ? AND power = ?",
            (user, power_id),
        ).fetchone()
        if not row or row[0] <= 0:
            return False
        if row[0] == 1:
            self._conn.execute(
                "DELETE FROM power_bank WHERE username = ? AND power = ?", (user, power_id)
            )
        else:
            self._conn.execute(
                "UPDATE power_bank SET count = count - 1 WHERE username = ? AND power = ?",
                (user, power_id),
            )
        self._conn.commit()
        return True

    def _bank_milestone(self, username: str, before: dict, after: dict,
                        category: str | None, cat_before: int | None) -> None:
        """Bank a themed power when a milestone is crossed for the first time.

        The old unlock-based abilities, folded into the themed catalog:
        level 3 -> Vibranium Guard, 5 battle wins -> Super Saiyan Strength,
        best streak 5 -> Senzu Bean, category mastery -> Shinigami Drain,
        25 points -> DeLorean Rewind. Only fires on the crossing (before
        below the threshold, after at or above it), so each fires once.
        ``cat_before`` is the pre-award correct count for ``category``
        (read by the caller before it bumps the count)."""
        user = username.lower()
        if before["level"] < MILESTONE_LEVEL <= after["level"]:
            self.bank_power(user, "guard")
        if before["points"] < MILESTONE_POINTS <= after["points"]:
            self.bank_power(user, "rewind")
        if before["best_streak"] < MILESTONE_STREAK <= after["best_streak"]:
            self.bank_power(user, "heal")
        if category is not None and cat_before is not None:
            if cat_before < MILESTONE_MASTERY <= cat_before + 1:
                self.bank_power(user, "drain")

    def add_point(self, username: str) -> int:
        user = username.lower()
        self._conn.execute(
            "INSERT INTO scores (username, points) VALUES (?, 1) "
            "ON CONFLICT(username) DO UPDATE SET points = points + 1",
            (user,),
        )
        self._conn.commit()
        return self.get_score(user)

    def award(self, username: str, xp_gain: int, category: str | None = None) -> dict:
        """Award a correct answer: +1 point and xp_gain XP, streak +1.

        ``category`` (when given) also bumps the per-category correct
        count used for category mastery. Returns a stats dict: username,
        points, xp, level, xp_gained, xp_into_level, xp_to_next,
        leveled_up, prev_level, streak, best_streak, battle_wins.
        """
        user = username.lower()
        before = self.get_stats(user)
        cat_before = self.get_category_correct(user, category) if category else None
        self._conn.execute(
            "INSERT INTO scores (username, points, xp, streak, best_streak) VALUES (?, 1, ?, 1, 1) "
            "ON CONFLICT(username) DO UPDATE SET points = points + 1, xp = xp + excluded.xp, "
            "streak = streak + 1, best_streak = MAX(best_streak, streak + 1)",
            (user, xp_gain),
        )
        if category:
            self._conn.execute(
                "INSERT INTO category_stats (username, category, correct) VALUES (?, ?, 1) "
                "ON CONFLICT(username, category) DO UPDATE SET correct = correct + 1",
                (user, category),
            )
        self._conn.commit()
        after = self.get_stats(user)
        after["xp_gained"] = xp_gain
        after["prev_level"] = before["level"]
        after["leveled_up"] = after["level"] > before["level"]
        self._bank_milestone(user, before, after, category, cat_before)
        after["bank"] = self.get_bank(user)
        return after

    def award_xp(self, username: str, xp_gain: int) -> dict:
        """Award XP only (battle rewards): no point, no streak change."""
        user = username.lower()
        before = self.get_stats(user)
        self._conn.execute(
            "INSERT INTO scores (username, xp) VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET xp = xp + excluded.xp",
            (user, xp_gain),
        )
        self._conn.commit()
        after = self.get_stats(user)
        after["xp_gained"] = xp_gain
        after["prev_level"] = before["level"]
        after["leveled_up"] = after["level"] > before["level"]
        # Battle XP can push a player over level 3 — same milestone grant.
        self._bank_milestone(user, before, after, None, None)
        after["bank"] = self.get_bank(user)
        return after

    def reset_streak(self, username: str) -> None:
        """A wrong answer breaks the consecutive-correct streak."""
        self._conn.execute(
            "UPDATE scores SET streak = 0 WHERE username = ?", (username.lower(),)
        )
        self._conn.commit()

    def add_battle_win(self, username: str) -> int:
        """Record a battle win. Returns the new total.

        A 5th battle win banks Super Saiyan Strength (milestone, once).
        """
        user = username.lower()
        before = self.get_stats(user)["battle_wins"]
        self._conn.execute(
            "INSERT INTO scores (username, battle_wins) VALUES (?, 1) "
            "ON CONFLICT(username) DO UPDATE SET battle_wins = battle_wins + 1",
            (user,),
        )
        self._conn.commit()
        wins = self.get_stats(user)["battle_wins"]
        if before < MILESTONE_WINS <= wins:
            self.bank_power(user, "strength")
        return wins

    def get_category_correct(self, username: str, category: str) -> int:
        row = self._conn.execute(
            "SELECT correct FROM category_stats WHERE username = ? AND category = ?",
            (username.lower(), category),
        ).fetchone()
        return row[0] if row else 0

    def mastered_categories(self, username: str, threshold: int = 10) -> list[str]:
        """Categories with at least ``threshold`` correct answers."""
        rows = self._conn.execute(
            "SELECT category FROM category_stats WHERE username = ? AND correct >= ? ORDER BY category",
            (username.lower(), threshold),
        ).fetchall()
        return [r[0] for r in rows]

    def get_score(self, username: str) -> int:
        row = self._conn.execute(
            "SELECT points FROM scores WHERE username = ?", (username.lower(),)
        ).fetchone()
        return row[0] if row else 0

    def get_stats(self, username: str) -> dict:
        """Points, XP, level, streak and battle wins (zeros when unknown)."""
        row = self._conn.execute(
            "SELECT points, xp, streak, best_streak, battle_wins FROM scores WHERE username = ?",
            (username.lower(),),
        ).fetchone()
        points, xp, streak, best_streak, battle_wins = row if row else (0, 0, 0, 0, 0)
        return {
            "username": username.lower(),
            "points": points,
            "xp": xp,
            "level": level_for_xp(xp),
            "xp_into_level": xp_into_level(xp),
            "xp_to_next": xp_to_next_level(xp),
            "streak": streak,
            "best_streak": best_streak,
            "battle_wins": battle_wins,
            "bank": self.get_bank(username.lower()),
        }

    def leaderboard(self, limit: int = 5) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT username, points FROM scores ORDER BY points DESC, username ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(u, p) for u, p in rows]

    def leaderboard_with_xp(self, limit: int = 5) -> list[dict]:
        """Top players by XP, each with points, XP and level."""
        rows = self._conn.execute(
            "SELECT username, points, xp FROM scores ORDER BY xp DESC, points DESC, username ASC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict] = []
        for username, points, xp in rows:
            out.append({
                "username": username,
                "points": points,
                "xp": xp,
                "level": level_for_xp(xp),
                "xp_into_level": xp_into_level(xp),
                "xp_to_next": xp_to_next_level(xp),
                "bank": self.get_bank(username),
            })
        return out

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Active round state
# ---------------------------------------------------------------------------


@dataclass
class Round:
    question: Question
    started_at: float
    timeout_seconds: int
    asked_by: str
    proactive: bool = False

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.started_at) > self.timeout_seconds


class QuizManager:
    """Holds the question bank, the active round, and the scores.

    last_award holds the stats dict from the most recent correct answer
    (see ScoreStore.award), or None before any win. level_up_feed is a
    short feed of recent level-ups for the live overlay, newest last;
    power_feed is the same for themed power grants. ``battle`` is the
    RPG battle layer (rpg.BattleManager), sharing the same question bank
    and scores.
    """

    def __init__(self, questions: list[Question], scores: ScoreStore, timeout_seconds: int = 30):
        if not questions:
            raise ValueError("QuizManager needs at least one question")
        self.questions = questions
        self.scores = scores
        self.timeout_seconds = timeout_seconds
        self.active: Round | None = None
        # True after !quizstop until !quizresume: no auto-rounds. Manual
        # !quiz rounds still work while paused (an explicit ask).
        self.paused: bool = False
        self._recent: list[str] = []  # normalized questions, avoid quick repeats
        self.last_award: dict | None = None
        self.level_up_feed: list[dict] = []
        self.power_feed: list[dict] = []  # recent power grants, for the overlay
        # Imported here (not at module top) so quiz.py stays importable
        # without rpg.py pulling in anything extra — and so rpg.py can
        # import quiz.py without a cycle.
        from rpg import BattleManager

        self.battle = BattleManager(questions, scores)

    def by_category(self, category: str | None) -> list[Question]:
        if category is None:
            return self.questions
        return [q for q in self.questions if q.category == category]

    def by_category_and_difficulty(
        self, category: str | None, difficulty: str | None
    ) -> list[Question]:
        pool = self.by_category(category)
        if difficulty is None:
            return pool
        return [q for q in pool if q.difficulty == difficulty]

    def start_round(
        self,
        asked_by: str,
        category: str | None = None,
        difficulty: str | None = None,
        proactive: bool = False,
    ) -> Round | None:
        """Start a round. Returns None if one is already active."""
        if self.active is not None and not self.active.expired:
            return None
        pool = [q for q in self.by_category_and_difficulty(category, difficulty)
                if normalize(q.question) not in self._recent]
        if not pool:
            self._recent.clear()
            pool = self.by_category_and_difficulty(category, difficulty)
        question = random.choice(pool)
        self._recent.append(normalize(question.question))
        self._recent = self._recent[-15:]
        self.active = Round(
            question=question,
            started_at=time.monotonic(),
            timeout_seconds=self.timeout_seconds,
            asked_by=asked_by,
            proactive=proactive,
        )
        return self.active

    def check_answer(self, username: str, text: str) -> tuple[str, str | None]:
        """Check a chat message against the active round.

        Returns ("no-round" | "wrong" | "correct", detail). On "correct"
        XP is awarded (scaled by the question's difficulty), the point is
        counted, and the round ends. Detail is the new point total, as
        before; the full award (XP, level, level-up) is in last_award.
        """
        if self.active is None or self.active.expired:
            self.active = None
            return "no-round", None
        if is_correct(text, self.active.question):
            question = self.active.question
            award = self.scores.award(
                username, question.xp_value, category=question.category
            )
            award["difficulty"] = question.difficulty
            # A correct answer banks the question's themed power
            # (specific tag, or the category fallback) for the next battle.
            power_id = power_for_question(question)
            granted = self.scores.bank_power(username, power_id)
            award["power_granted"] = power_id if granted["granted"] else None
            award["power_bank_full"] = not granted["granted"] and granted["reason"] == "bank-full"
            award["power"] = POWERS[power_id]
            if granted["granted"]:
                self.power_feed.append({
                    "username": award["username"],
                    "power": power_id,
                    "name": POWERS[power_id]["name"],
                    "character": POWERS[power_id]["character"],
                    "at": time.time(),
                })
                self.power_feed = self.power_feed[-10:]
            self.last_award = award
            if award["leveled_up"]:
                self.level_up_feed.append({
                    "username": award["username"],
                    "level": award["level"],
                    "at": time.time(),
                })
                self.level_up_feed = self.level_up_feed[-10:]
            self.active = None
            return "correct", str(award["points"])
        return "wrong", None

    def expire_round(self) -> Question | None:
        """End the active round if it timed out; return its question or None."""
        if self.active is not None and self.active.expired:
            q = self.active.question
            self.active = None
            return q
        return None

    def stop_round(self) -> Question | None:
        """End the active round immediately; return its question or None."""
        if self.active is None:
            return None
        q = self.active.question
        self.active = None
        return q

    def pause(self) -> Question | None:
        """Pause auto-rounds and end the active round now.

        Returns the stopped question, or None when no round was active.
        Manual !quiz rounds still work while paused.
        """
        self.paused = True
        return self.stop_round()

    def resume(self) -> None:
        """Re-enable auto-rounds after a pause."""
        self.paused = False


# ---------------------------------------------------------------------------
# Proactive-round decision (pure, so it can be tested)
# ---------------------------------------------------------------------------


def should_post_proactive(
    *,
    now: float,
    is_live: bool,
    round_active: bool,
    last_round_ended_at: float | None,
    interval_seconds: float,
    cooldown_after_round_seconds: float = 300.0,
    paused: bool = False,
) -> bool:
    """True when the bot may ask a question on its own right now."""
    if not is_live:
        return False
    if round_active:
        return False
    if paused:
        return False
    if last_round_ended_at is not None and (now - last_round_ended_at) < cooldown_after_round_seconds:
        return False
    return True


def proactive_due(now: float, last_check_at: float, interval_seconds: float) -> bool:
    return (now - last_check_at) >= interval_seconds
