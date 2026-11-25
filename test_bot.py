"""Offline tests for the Twitch quiz bot — no Twitch, no Ollama required.

Run:  python test_bot.py
Covers: question bank loading + difficulty tiers, answer matching, scoring
and XP/level persistence, round state, difficulty-filtered rounds, the live
overlay page + state endpoint, Athena mention/rate-limit/trim logic, Athena
offline behaviour (unreachable Ollama -> None), Athena happy path (local
fake Ollama server), proactive-round decision logic, and config parsing.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from athena import AthenaBrain, RateLimiter, mentions_athena, trim_reply  # noqa: E402
from config import Config  # noqa: E402
from quiz import (  # noqa: E402
    CATEGORIES,
    DIFFICULTIES,
    XP_EASY,
    XP_FOR_DIFFICULTY,
    XP_HARD,
    XP_MEDIUM,
    XP_PER_LEVEL,
    Question,
    QuizManager,
    ScoreStore,
    is_correct,
    level_for_xp,
    load_questions,
    normalize,
    proactive_due,
    resolve_category,
    resolve_difficulty,
    should_post_proactive,
    xp_into_level,
    xp_to_next_level,
)
from powers import MAX_BANKED, POWERS, power_for_question  # noqa: E402
from rpg import (  # noqa: E402
    BATTLE_COOLDOWN_SECONDS,
    BATTLE_MAX_ROUNDS,
    BattleManager,
    damage_for,
    max_hp,
    self_damage_for,
)

HERE = Path(__file__).resolve().parent


class TestQuestions(unittest.TestCase):
    def test_bank_loads_157_with_known_counts(self):
        questions = load_questions(HERE / "questions.json")
        self.assertEqual(len(questions), 157)
        counts: dict[str, int] = {}
        tier_counts: dict[tuple[str, str], int] = {}
        for q in questions:
            counts[q.category] = counts.get(q.category, 0) + 1
            tier_counts[(q.category, q.difficulty)] = tier_counts.get((q.category, q.difficulty), 0) + 1
            self.assertTrue(q.question.strip())
            self.assertTrue(q.answer.strip())
            self.assertIsInstance(q.alternates, list)
            self.assertIn(q.difficulty, DIFFICULTIES)
            self.assertIn(q.power, list(POWERS) + [""])
        self.assertEqual(counts, {"gta": 25, "anime": 28, "gaming": 25,
                                  "general": 25, "movies": 29, "music": 25})
        for cat in CATEGORIES:
            # Every category has a real spread across all three tiers.
            for diff in DIFFICULTIES:
                self.assertGreaterEqual(
                    tier_counts.get((cat, diff), 0), 3,
                    f"category {cat} should have >=3 {diff} questions",
                )
        # Difficulty totals after the themed-power expansion (150 -> 157).
        diff_totals = {d: sum(1 for q in questions if q.difficulty == d) for d in DIFFICULTIES}
        self.assertEqual(diff_totals, {"easy": 57, "medium": 56, "hard": 44})
        # At least 60 questions carry a real character/subject power tag;
        # the rest fall back to their category's mascot power.
        tagged = sum(1 for q in questions if q.power)
        self.assertGreaterEqual(tagged, 60)
        for q in questions:
            self.assertIn(power_for_question(q), POWERS)

    def test_no_duplicate_questions(self):
        questions = load_questions(HERE / "questions.json")
        seen = {normalize(q.question) for q in questions}
        self.assertEqual(len(seen), len(questions))

    def test_bad_difficulty_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            bad = HERE / "questions.json"
            data = json.loads(bad.read_text(encoding="utf-8"))
            data["questions"] = [dict(data["questions"][0], difficulty="impossible")]
            path = Path(d) / "questions.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_questions(path)

    def test_bad_power_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            bad = HERE / "questions.json"
            data = json.loads(bad.read_text(encoding="utf-8"))
            data["questions"] = [dict(data["questions"][0], power="not-a-power")]
            path = Path(d) / "questions.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_questions(path)

    def test_flash_questions_grant_speed(self):
        # Chris's example: answer about The Flash, get super speed.
        questions = load_questions(HERE / "questions.json")
        flash = [q for q in questions if "Flash" in q.question]
        self.assertGreaterEqual(len(flash), 3)
        for q in flash:
            self.assertEqual(power_for_question(q), "speed")


class TestDifficulty(unittest.TestCase):
    def test_resolve_difficulty(self):
        self.assertEqual(resolve_difficulty("easy"), "easy")
        self.assertEqual(resolve_difficulty("EASY"), "easy")
        self.assertEqual(resolve_difficulty("med"), "medium")
        self.assertEqual(resolve_difficulty("medium"), "medium")
        self.assertEqual(resolve_difficulty("HARD"), "hard")
        self.assertEqual(resolve_difficulty("h"), "hard")
        self.assertIsNone(resolve_difficulty("nope"))
        self.assertIsNone(resolve_difficulty(None))
        self.assertIsNone(resolve_difficulty(""))

    def test_xp_values(self):
        self.assertEqual(XP_EASY, 10)
        self.assertEqual(XP_MEDIUM, 25)
        self.assertEqual(XP_HARD, 50)
        self.assertEqual(XP_FOR_DIFFICULTY, {"easy": 10, "medium": 25, "hard": 50})
        self.assertEqual(Question("q?", "a", difficulty="easy").xp_value, 10)
        self.assertEqual(Question("q?", "a", difficulty="medium").xp_value, 25)
        self.assertEqual(Question("q?", "a", difficulty="hard").xp_value, 50)

    def test_level_curve(self):
        # Level n needs 100 * (n - 1) total XP.
        self.assertEqual(level_for_xp(0), 1)
        self.assertEqual(level_for_xp(99), 1)
        self.assertEqual(level_for_xp(100), 2)
        self.assertEqual(level_for_xp(199), 2)
        self.assertEqual(level_for_xp(200), 3)
        self.assertEqual(level_for_xp(1000), 11)
        self.assertEqual(xp_into_level(250), 50)
        self.assertEqual(xp_to_next_level(250), 50)
        self.assertEqual(xp_to_next_level(200), 100)


class TestMatching(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("  The  Pacific Ocean! "), "pacific ocean")
        self.assertEqual(normalize("A Tale of Two Cities"), "tale of two cities")
        self.assertEqual(normalize("V-Bucks"), "v bucks")

    def test_exact_and_alternates(self):
        questions = load_questions(HERE / "questions.json")
        q = next(x for x in questions if x.answer == "Niko Bellic")
        self.assertTrue(is_correct("Niko Bellic", q))
        self.assertTrue(is_correct("niko bellic", q))
        self.assertTrue(is_correct("NIKO", q))  # alternate
        self.assertTrue(is_correct("  Niko Bellic!! ", q))
        self.assertFalse(is_correct("Tommy Vercetti", q))
        self.assertFalse(is_correct("", q))
        self.assertFalse(is_correct("Niko Bel", q))

    def test_article_and_punctuation_tolerance(self):
        questions = load_questions(HERE / "questions.json")
        q = next(x for x in questions if x.answer == "Pacific Ocean")
        self.assertTrue(is_correct("the pacific ocean", q))
        self.assertTrue(is_correct("Pacific", q))  # alternate


class TestCategories(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(resolve_category("gta"), "gta")
        self.assertEqual(resolve_category("GTA"), "gta")
        self.assertEqual(resolve_category("movies"), "movies")
        self.assertEqual(resolve_category("tv"), "movies")
        self.assertIsNone(resolve_category("nope"))
        self.assertIsNone(resolve_category(None))
        self.assertIsNone(resolve_category(""))


class TestScores(unittest.TestCase):
    def test_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "scores.db")
            s1 = ScoreStore(db)
            self.assertEqual(s1.add_point("Alice"), 1)
            self.assertEqual(s1.add_point("alice"), 2)  # case-insensitive
            self.assertEqual(s1.get_score("ALICE"), 2)
            self.assertEqual(s1.get_score("nobody"), 0)
            s1.close()
            s2 = ScoreStore(db)
            self.assertEqual(s2.get_score("alice"), 2)
            self.assertEqual(s2.leaderboard(5), [("alice", 2)])
            s2.close()

    def test_leaderboard_order(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            s.add_point("bob")
            s.add_point("amy")
            s.add_point("amy")
            self.assertEqual(s.leaderboard(5), [("amy", 2), ("bob", 1)])
            s.close()


class TestXP(unittest.TestCase):
    def test_award_scales_by_xp_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "scores.db")
            s = ScoreStore(db)
            award = s.award("Alice", 10)
            self.assertEqual(award["points"], 1)
            self.assertEqual(award["xp"], 10)
            self.assertEqual(award["level"], 1)
            self.assertFalse(award["leveled_up"])
            award = s.award("alice", 25)
            self.assertEqual(award["points"], 2)
            self.assertEqual(award["xp"], 35)
            self.assertEqual(s.get_stats("ALICE")["xp"], 35)
            s.close()
            s2 = ScoreStore(db)
            self.assertEqual(s2.get_stats("alice")["xp"], 35)
            self.assertEqual(s2.get_stats("alice")["points"], 2)
            s2.close()

    def test_level_up_detected_at_100_xp(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            s.award("bob", 90)
            award = s.award("bob", 25)  # 115 XP total -> level 2
            self.assertTrue(award["leveled_up"])
            self.assertEqual(award["prev_level"], 1)
            self.assertEqual(award["level"], 2)
            self.assertEqual(award["xp_into_level"], 15)
            self.assertEqual(award["xp_to_next"], 85)
            s.close()

    def test_get_stats_unknown_user(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            stats = s.get_stats("nobody")
            self.assertEqual(stats, {
                "username": "nobody", "points": 0, "xp": 0, "level": 1,
                "xp_into_level": 0, "xp_to_next": 100,
                "streak": 0, "best_streak": 0, "battle_wins": 0,
                "bank": [],
            })
            s.close()

    def test_leaderboard_with_xp_orders_by_xp(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            s.award("amy", 10)    # 1 pt, 10 XP
            s.award("bob", 50)    # 1 pt, 50 XP
            s.award("bob", 50)    # 2 pts, 100 XP
            top = s.leaderboard_with_xp(5)
            self.assertEqual([r["username"] for r in top], ["bob", "amy"])
            self.assertEqual(top[0]["level"], 2)
            self.assertEqual(top[0]["points"], 2)
            self.assertEqual(top[1]["level"], 1)
            # Old points-only leaderboard still returns (name, points).
            self.assertEqual(s.leaderboard(5), [("bob", 2), ("amy", 1)])
            s.close()

    def test_migrates_pre_xp_database(self):
        # A scores.db written before XP existed has no xp column.
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "scores.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE scores (username TEXT PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("INSERT INTO scores (username, points) VALUES ('old', 3)")
            conn.commit()
            conn.close()
            s = ScoreStore(db)
            self.assertEqual(s.get_score("old"), 3)
            self.assertEqual(s.get_stats("old")["xp"], 0)
            award = s.award("old", 50)
            self.assertEqual(award["points"], 4)
            self.assertEqual(award["xp"], 50)
            s.close()


class TestRounds(unittest.TestCase):
    def _manager(self):
        questions = load_questions(HERE / "questions.json")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scores = ScoreStore(str(Path(tmp.name) / "scores.db"))
        self.addCleanup(scores.close)
        return QuizManager(questions, scores, timeout_seconds=30)

    def test_start_and_correct_answer(self):
        m = self._manager()
        rnd = m.start_round("tester", "gta")
        self.assertIsNotNone(rnd)
        self.assertEqual(rnd.question.category, "gta")
        status, detail = m.check_answer("winner", rnd.question.answer)
        self.assertEqual(status, "correct")
        self.assertEqual(detail, "1")
        self.assertEqual(m.scores.get_score("winner"), 1)
        self.assertIsNone(m.active)

    def test_wrong_answer_keeps_round(self):
        m = self._manager()
        rnd = m.start_round("tester")
        status, _ = m.check_answer("someone", "definitely not the answer")
        self.assertEqual(status, "wrong")
        self.assertIsNotNone(m.active)

    def test_second_round_blocked_while_active(self):
        m = self._manager()
        self.assertIsNotNone(m.start_round("tester"))
        self.assertIsNone(m.start_round("tester"))

    def test_no_repeat_within_15(self):
        m = self._manager()
        seen: set[str] = set()
        for _ in range(15):
            rnd = m.start_round("tester")
            assert rnd is not None
            seen.add(normalize(rnd.question.question))
            m.stop_round()
        self.assertEqual(len(seen), 15)

    def test_timeout_expiry(self):
        m = self._manager()
        rnd = m.start_round("tester")
        assert rnd is not None
        rnd.started_at = time.monotonic() - 31  # force expiry
        status, _ = m.check_answer("late", rnd.question.answer)
        self.assertEqual(status, "no-round")
        self.assertIsNone(m.active)

    def _tiny_manager(self):
        """A manager with exactly one question per difficulty, for XP tests."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scores = ScoreStore(str(Path(tmp.name) / "scores.db"))
        self.addCleanup(scores.close)
        questions = [
            Question("easy q?", "EasyAns", category="general", difficulty="easy"),
            Question("medium q?", "MedAns", category="general", difficulty="medium"),
            Question("hard q?", "HardAns", category="general", difficulty="hard"),
        ]
        return QuizManager(questions, scores, timeout_seconds=30)

    def test_start_round_filters_by_difficulty(self):
        m = self._tiny_manager()
        rnd = m.start_round("tester", difficulty="hard")
        assert rnd is not None
        self.assertEqual(rnd.question.difficulty, "hard")
        self.assertEqual(rnd.question.answer, "HardAns")
        m.stop_round()
        rnd = m.start_round("tester", "general", difficulty="easy")
        assert rnd is not None
        self.assertEqual(rnd.question.difficulty, "easy")

    def test_correct_answer_awards_xp_by_difficulty(self):
        m = self._tiny_manager()
        rnd = m.start_round("tester", difficulty="hard")
        assert rnd is not None
        status, detail = m.check_answer("winner", "HardAns")
        self.assertEqual(status, "correct")
        self.assertEqual(detail, "1")  # detail stays the point total
        award = m.last_award
        assert award is not None
        self.assertEqual(award["xp_gained"], 50)
        self.assertEqual(award["xp"], 50)
        self.assertEqual(award["difficulty"], "hard")
        self.assertFalse(award["leveled_up"])
        # Two more hard wins cross 100 XP -> level 2.
        for _ in range(2):
            m.start_round("tester", difficulty="hard")
            m.check_answer("winner", "HardAns")
        self.assertEqual(m.scores.get_stats("winner")["level"], 2)
        self.assertEqual(len(m.level_up_feed), 1)
        self.assertEqual(m.level_up_feed[0]["username"], "winner")
        self.assertEqual(m.level_up_feed[0]["level"], 2)


class TestOverlay(unittest.TestCase):
    def test_build_state_shape(self):
        from overlay import OVERLAY_HTML, build_state

        questions = load_questions(HERE / "questions.json")
        with tempfile.TemporaryDirectory() as d:
            scores = ScoreStore(str(Path(d) / "scores.db"))
            m = QuizManager(questions, scores, timeout_seconds=30)
            state = build_state(m)
            self.assertIn("now", state)
            self.assertEqual(state["leaderboard"], [])
            self.assertIsNone(state["active_round"])
            self.assertEqual(state["level_ups"], [])
            self.assertEqual(state["power_feed"], [])
            # A round shows up with its difficulty and XP value.
            rnd = m.start_round("tester", "gta", difficulty="easy")
            assert rnd is not None
            state = build_state(m)
            self.assertEqual(state["active_round"]["category"], "gta")
            self.assertEqual(state["active_round"]["difficulty"], "easy")
            self.assertEqual(state["active_round"]["xp_value"], 10)
            self.assertGreater(state["active_round"]["ends_at"], state["now"])
            # A win lands on the leaderboard with XP, level — and the
            # themed power it banked, plus the power-grant feed.
            m.check_answer("winner", rnd.question.answer)
            state = build_state(m)
            self.assertIsNone(state["active_round"])
            self.assertEqual(state["leaderboard"][0]["username"], "winner")
            self.assertEqual(state["leaderboard"][0]["xp"], 10)
            self.assertEqual(state["leaderboard"][0]["level"], 1)
            self.assertEqual(len(state["leaderboard"][0]["bank"]), 1)
            self.assertEqual(len(state["power_feed"]), 1)
            self.assertEqual(state["power_feed"][0]["username"], "winner")
            scores.close()

        # The page is self-contained and polls the state endpoint.
        self.assertIn("state.json", OVERLAY_HTML)
        self.assertIn("LEVEL UP", OVERLAY_HTML)
        self.assertIn("LEADERBOARD", OVERLAY_HTML)
        # No external URLs — nothing for OBS to fetch off the internet.
        self.assertNotIn("http://", OVERLAY_HTML)
        self.assertNotIn("https://", OVERLAY_HTML)


class TestOverlayServer(unittest.IsolatedAsyncioTestCase):
    async def test_serves_page_and_state(self):
        from aiohttp import ClientSession

        from overlay import OverlayServer

        questions = load_questions(HERE / "questions.json")
        with tempfile.TemporaryDirectory() as d:
            scores = ScoreStore(str(Path(d) / "scores.db"))
            m = QuizManager(questions, scores, timeout_seconds=30)
            server = OverlayServer(m, host="127.0.0.1", port=0)
            await server.start()
            try:
                base = f"http://127.0.0.1:{server.port}"
                async with ClientSession() as session:
                    async with session.get(base + "/") as resp:
                        self.assertEqual(resp.status, 200)
                        self.assertIn("text/html", resp.content_type)
                        body = await resp.text()
                        self.assertIn("LEADERBOARD", body)
                    async with session.get(base + "/state.json") as resp:
                        self.assertEqual(resp.status, 200)
                        data = await resp.json()
                        self.assertIn("leaderboard", data)
                        self.assertIn("active_round", data)
                        self.assertIn("level_ups", data)
            finally:
                await server.stop()
                scores.close()


class TestAthenaPure(unittest.TestCase):
    def test_mentions(self):
        self.assertTrue(mentions_athena("hi athena!"))
        self.assertTrue(mentions_athena("ATHENA what do you think"))
        self.assertFalse(mentions_athena("hi athenax"))
        self.assertFalse(mentions_athena("hello there"))
        self.assertFalse(mentions_athena(""))

    def test_trim_reply(self):
        self.assertEqual(trim_reply("Hello there. How are you? Third sentence here."),
                         "Hello there. How are you?")
        self.assertEqual(trim_reply("  spaced   out  "), "spaced out")
        self.assertEqual(trim_reply(""), "")

    def test_rate_limiter(self):
        rl = RateLimiter(min_interval_seconds=20)
        self.assertTrue(rl.allow(now=100.0))
        self.assertFalse(rl.allow(now=105.0))
        self.assertTrue(rl.allow(now=121.0))


class TestAthenaNetwork(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_ollama_stays_quiet(self):
        brain = AthenaBrain("http://127.0.0.1:9", "hermes3:8b")
        self.assertIsNone(await brain.reply("tester", "hey athena, what is up?"))

    async def test_command_and_oversize_ignored_without_network(self):
        brain = AthenaBrain("http://127.0.0.1:9", "hermes3:8b")
        self.assertIsNone(await brain.reply("tester", "!quiz gta"))
        self.assertIsNone(await brain.reply("tester", "athena " + "x" * 500))

    async def test_happy_path_against_fake_ollama(self):
        from aiohttp import web

        async def handler(request):
            body = await request.json()
            self.assertEqual(body["model"], "hermes3:8b")
            return web.json_response({"message": {"content": "Hey! Good to see you in chat. How is the stream going? Extra sentence."}})

        app = web.Application()
        app.router.add_post("/api/chat", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            brain = AthenaBrain(f"http://127.0.0.1:{port}", "hermes3:8b")
            reply = await brain.reply("tester", "hey athena")
            # trim_reply keeps at most two sentences; "Hey!" counts as one.
            self.assertEqual(reply, "Hey! Good to see you in chat.")
            # Second call inside the 20 s window is rate-limited.
            self.assertIsNone(await brain.reply("tester", "hey athena again"))
        finally:
            await runner.cleanup()


class TestProactive(unittest.TestCase):
    def test_decision_matrix(self):
        base = dict(now=1000.0, round_active=False, last_round_ended_at=None,
                    interval_seconds=900.0)
        self.assertTrue(should_post_proactive(is_live=True, **base))
        self.assertFalse(should_post_proactive(is_live=False, **base))
        self.assertFalse(should_post_proactive(is_live=True, round_active=True, **{k: v for k, v in base.items() if k != "round_active"}))
        self.assertFalse(should_post_proactive(is_live=True, **{**base, "last_round_ended_at": 900.0}))
        self.assertTrue(should_post_proactive(is_live=True, **{**base, "last_round_ended_at": 600.0}))

    def test_proactive_due(self):
        self.assertTrue(proactive_due(1000.0, 0.0, 900.0))
        self.assertFalse(proactive_due(500.0, 0.0, 900.0))

    def test_pause_blocks_proactive(self):
        base = dict(now=1000.0, round_active=False, last_round_ended_at=None,
                    interval_seconds=900.0)
        self.assertTrue(should_post_proactive(is_live=True, paused=False, **base))
        self.assertFalse(should_post_proactive(is_live=True, paused=True, **base))

    def test_pause_stops_round_and_resume_clears(self):
        questions = load_questions(HERE / "questions.json")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scores = ScoreStore(str(Path(tmp.name) / "scores.db"))
        self.addCleanup(scores.close)
        m = QuizManager(questions, scores, timeout_seconds=30)
        self.assertFalse(m.paused)
        rnd = m.start_round("tester")
        self.assertIsNotNone(rnd)
        stopped = m.pause()
        self.assertTrue(m.paused)
        self.assertIsNotNone(stopped)
        self.assertEqual(stopped.question, rnd.question.question)
        self.assertIsNone(m.active)
        m.resume()
        self.assertFalse(m.paused)

    def test_pause_without_active_round(self):
        questions = load_questions(HERE / "questions.json")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scores = ScoreStore(str(Path(tmp.name) / "scores.db"))
        self.addCleanup(scores.close)
        m = QuizManager(questions, scores, timeout_seconds=30)
        self.assertIsNone(m.pause())
        self.assertTrue(m.paused)
        m.resume()
        self.assertFalse(m.paused)


class TestConfig(unittest.TestCase):
    def test_defaults_and_env_override(self):
        cfg = Config.from_env({"TWITCH_CLIENT_ID": "abc", "TWITCH_CLIENT_SECRET": "def"})
        self.assertEqual(cfg.channel_login, "kingdamienjl")
        self.assertEqual(cfg.bot_id, "59499420")
        self.assertEqual(cfg.ollama_model, "hermes3:8b")
        self.assertEqual(cfg.proactive_interval_minutes, 15)
        self.assertTrue(cfg.overlay_enabled)
        self.assertEqual(cfg.overlay_port, 8765)
        cfg_off = Config.from_env({"OVERLAY_ENABLED": "false", "OVERLAY_PORT": "9999"})
        self.assertFalse(cfg_off.overlay_enabled)
        self.assertEqual(cfg_off.overlay_port, 9999)
        problems = cfg.validate()
        self.assertTrue(any("TWITCH_ACCESS_TOKEN is missing" in p for p in problems))

    def test_dotenv_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            env_file.write_text('TWITCH_CLIENT_ID="abc123"\n# comment\nTWITCH_CLIENT_SECRET=xyz\nEMPTY=\n', encoding="utf-8")
            from config import _parse_dotenv
            vals = _parse_dotenv(env_file)
            self.assertEqual(vals["TWITCH_CLIENT_ID"], "abc123")
            self.assertEqual(vals["TWITCH_CLIENT_SECRET"], "xyz")


class TestScoresRPG(unittest.TestCase):
    def test_streak_and_best_streak(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            s.award("amy", 10)
            s.award("amy", 10)
            s.award("amy", 10)
            stats = s.get_stats("amy")
            self.assertEqual(stats["streak"], 3)
            self.assertEqual(stats["best_streak"], 3)
            s.reset_streak("amy")
            stats = s.get_stats("amy")
            self.assertEqual(stats["streak"], 0)
            self.assertEqual(stats["best_streak"], 3)  # best survives a reset
            s.award("amy", 10)
            self.assertEqual(s.get_stats("amy")["streak"], 1)
            s.close()

    def test_category_stats_and_mastery(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            for _ in range(9):
                s.award("amy", 10, category="gta")
            self.assertEqual(s.get_category_correct("amy", "gta"), 9)
            self.assertEqual(s.mastered_categories("amy"), [])
            s.award("amy", 10, category="gta")
            self.assertEqual(s.get_category_correct("amy", "gta"), 10)
            self.assertEqual(s.mastered_categories("amy"), ["gta"])
            self.assertEqual(s.get_category_correct("amy", "anime"), 0)
            s.close()

    def test_award_xp_no_point_no_streak(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            s.award("amy", 10)  # 1 pt, streak 1
            result = s.award_xp("amy", 50)  # battle reward
            self.assertEqual(result["points"], 1)
            self.assertEqual(result["xp"], 60)
            self.assertEqual(result["streak"], 1)
            s.close()

    def test_battle_wins(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            self.assertEqual(s.add_battle_win("amy"), 1)
            self.assertEqual(s.add_battle_win("amy"), 2)
            self.assertEqual(s.get_stats("amy")["battle_wins"], 2)
            s.close()

    def test_migrates_pre_rpg_database(self):
        # A scores.db from before streaks/battle wins/category stats.
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "scores.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE scores (username TEXT PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0, "
                "xp INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("INSERT INTO scores (username, points, xp) VALUES ('old', 3, 40)")
            conn.commit()
            conn.close()
            s = ScoreStore(db)
            stats = s.get_stats("old")
            self.assertEqual(stats["points"], 3)
            self.assertEqual(stats["xp"], 40)
            self.assertEqual(stats["streak"], 0)
            self.assertEqual(stats["battle_wins"], 0)
            s.award("old", 10, category="gta")
            self.assertEqual(s.get_stats("old")["streak"], 1)
            self.assertEqual(s.get_category_correct("old", "gta"), 1)
            s.close()


class TestRPGNumbers(unittest.TestCase):
    def test_hp_curve(self):
        self.assertEqual(max_hp(1), 100)
        self.assertEqual(max_hp(2), 110)
        self.assertEqual(max_hp(5), 140)
        self.assertEqual(max_hp(10), 190)

    def test_damage_scales_with_difficulty_and_level(self):
        self.assertEqual(damage_for("easy", 1), 11)
        self.assertEqual(damage_for("medium", 1), 16)
        self.assertEqual(damage_for("hard", 1), 21)
        self.assertEqual(damage_for("hard", 10), 30)
        self.assertEqual(damage_for("easy", 5), 15)

    def test_self_damage_is_half_base(self):
        self.assertEqual(self_damage_for("easy"), 5)
        self.assertEqual(self_damage_for("medium"), 7)
        self.assertEqual(self_damage_for("hard"), 10)

    def test_power_catalog(self):
        # One themed list, 11 powers — the old 5 generic abilities are
        # folded in as themed versions (guard/strength/heal/drain/rewind).
        self.assertEqual(len(POWERS), 11)
        for power_id, info in POWERS.items():
            self.assertTrue(info["name"])
            self.assertTrue(info["character"])
            self.assertTrue(info["desc"])
            self.assertTrue(info["earn"])
            self.assertIn(info["effect"], {
                "speed", "strength", "guard", "heal", "drain", "rewind",
                "dodge", "rage", "bounce", "genjutsu", "revive",
            })
        self.assertEqual(POWERS["speed"]["name"], "Flash Speed")
        self.assertEqual(POWERS["speed"]["character"], "The Flash")
        self.assertEqual(POWERS["strength"]["character"], "Goku")
        self.assertEqual(POWERS["guard"]["character"], "Black Panther")
        self.assertEqual(POWERS["revive"]["character"], "Mario")

    def test_max_banked_is_three(self):
        self.assertEqual(MAX_BANKED, 3)


class TestPowers(unittest.TestCase):
    def test_power_for_question_tagged_vs_fallback(self):
        questions = load_questions(HERE / "questions.json")
        flash = next(q for q in questions if q.answer == "Barry Allen")
        self.assertEqual(flash.power, "speed")
        self.assertEqual(power_for_question(flash), "speed")
        goku = next(q for q in questions if q.answer == "Planet Vegeta")
        self.assertEqual(power_for_question(goku), "strength")
        # Untagged questions fall back to their category's mascot power.
        paris = next(q for q in questions if q.answer == "Paris")
        self.assertEqual(paris.power, "")
        self.assertEqual(power_for_question(paris), "speed")  # general
        sony = next(q for q in questions if q.answer == "Sony")
        self.assertEqual(power_for_question(sony), "revive")  # gaming
        beatles = next(q for q in questions if q.answer == "The Beatles")
        self.assertEqual(power_for_question(beatles), "heal")  # music

    def test_bank_cap_and_consume(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            self.assertTrue(s.bank_power("amy", "speed")["granted"])
            self.assertTrue(s.bank_power("amy", "guard")["granted"])
            self.assertTrue(s.bank_power("amy", "heal")["granted"])
            self.assertEqual(s.bank_total("amy"), 3)
            # Full: the 4th grant is refused, not silently dropped.
            result = s.bank_power("amy", "rage")
            self.assertFalse(result["granted"])
            self.assertEqual(result["reason"], "bank-full")
            self.assertEqual(s.bank_total("amy"), 3)
            # Spending one frees a slot.
            self.assertTrue(s.consume_power("amy", "guard"))
            self.assertEqual(s.bank_total("amy"), 2)
            self.assertTrue(s.bank_power("amy", "rage")["granted"])
            # Consuming what you don't have returns False.
            self.assertFalse(s.consume_power("amy", "bounce"))
            self.assertFalse(s.consume_power("nobody", "speed"))
            self.assertEqual(
                s.get_bank("amy"),
                [{"power": "heal", "name": "Senzu Bean", "count": 1},
                 {"power": "rage", "name": "Titan Rage", "count": 1},
                 {"power": "speed", "name": "Flash Speed", "count": 1}],
            )
            s.close()

    def test_bank_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "scores.db")
            s1 = ScoreStore(db)
            s1.bank_power("amy", "speed")
            s1.bank_power("amy", "speed")
            s1.close()
            s2 = ScoreStore(db)
            self.assertEqual(s2.get_bank("amy"),
                             [{"power": "speed", "name": "Flash Speed", "count": 2}])
            self.assertEqual(s2.get_stats("amy")["bank"],
                             [{"power": "speed", "name": "Flash Speed", "count": 2}])
            s2.close()

    def test_migrates_pre_power_database(self):
        # A scores.db from before powers existed has no power_bank table.
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "scores.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE scores (username TEXT PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0, "
                "xp INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0, "
                "best_streak INTEGER NOT NULL DEFAULT 0, battle_wins INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("INSERT INTO scores (username, points, xp) VALUES ('old', 3, 40)")
            conn.commit()
            conn.close()
            s = ScoreStore(db)
            self.assertEqual(s.get_stats("old")["bank"], [])
            self.assertTrue(s.bank_power("old", "speed")["granted"])
            self.assertEqual(s.get_score("old"), 3)
            s.close()

    def test_correct_answer_banks_question_power(self):
        questions = [
            Question("Who is the Flash?", "Barry Allen", category="movies",
                     difficulty="easy", power="speed"),
            Question("Capital of France?", "Paris", category="general", difficulty="easy"),
        ]
        with tempfile.TemporaryDirectory() as d:
            scores = ScoreStore(str(Path(d) / "scores.db"))
            m = QuizManager(questions, scores, timeout_seconds=30)
            rnd = m.start_round("tester", "movies")
            assert rnd is not None
            status, _ = m.check_answer("winner", "Barry Allen")
            self.assertEqual(status, "correct")
            award = m.last_award
            assert award is not None
            self.assertEqual(award["power_granted"], "speed")
            self.assertEqual(award["power"]["name"], "Flash Speed")
            self.assertEqual(scores.get_bank("winner"),
                             [{"power": "speed", "name": "Flash Speed", "count": 1}])
            # The overlay feed records the grant.
            self.assertEqual(len(m.power_feed), 1)
            self.assertEqual(m.power_feed[0]["username"], "winner")
            self.assertEqual(m.power_feed[0]["character"], "The Flash")
            scores.close()

    def test_correct_answer_banks_fallback_when_untagged(self):
        questions = [
            Question("Capital of France?", "Paris", category="general", difficulty="easy"),
        ]
        with tempfile.TemporaryDirectory() as d:
            scores = ScoreStore(str(Path(d) / "scores.db"))
            m = QuizManager(questions, scores, timeout_seconds=30)
            m.start_round("tester")
            m.check_answer("winner", "Paris")
            award = m.last_award
            assert award is not None
            self.assertEqual(award["power_granted"], "speed")  # general fallback
            scores.close()

    def test_bank_full_blocks_grant_but_keeps_xp(self):
        questions = [
            Question("Who is the Flash?", "Barry Allen", category="movies",
                     difficulty="easy", power="speed"),
        ]
        with tempfile.TemporaryDirectory() as d:
            scores = ScoreStore(str(Path(d) / "scores.db"))
            scores.bank_power("winner", "guard")
            scores.bank_power("winner", "heal")
            scores.bank_power("winner", "rage")
            m = QuizManager(questions, scores, timeout_seconds=30)
            m.start_round("tester")
            status, _ = m.check_answer("winner", "Barry Allen")
            self.assertEqual(status, "correct")
            award = m.last_award
            assert award is not None
            self.assertIsNone(award["power_granted"])
            self.assertTrue(award["power_bank_full"])
            self.assertEqual(award["xp_gained"], 10)  # XP still lands
            self.assertEqual(scores.bank_total("winner"), 3)
            scores.close()

    def test_milestone_level_banks_guard_once(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            s.award("amy", 199)  # level 2, no milestone yet
            self.assertEqual(s.get_bank("amy"), [])
            s.award("amy", 1)  # crosses 200 XP -> level 3
            self.assertEqual(s.get_bank("amy"),
                             [{"power": "guard", "name": "Vibranium Guard", "count": 1}])
            s.award("amy", 500)  # level 8 — no second grant
            self.assertEqual(s.get_bank("amy"),
                             [{"power": "guard", "name": "Vibranium Guard", "count": 1}])
            s.close()

    def test_milestone_streak_banks_heal(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            for _ in range(4):
                s.award("amy", 10)
            self.assertEqual(s.get_bank("amy"), [])
            s.award("amy", 10)  # 5th in a row
            self.assertIn({"power": "heal", "name": "Senzu Bean", "count": 1},
                          s.get_bank("amy"))
            s.close()

    def test_milestone_mastery_banks_drain(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            for _ in range(9):
                s.award("amy", 10, category="gta")
            # The 5-answer streak already banked a Senzu Bean — but no drain yet.
            self.assertNotIn("drain", [r["power"] for r in s.get_bank("amy")])
            s.award("amy", 10, category="gta")  # 10th in GTA
            self.assertIn({"power": "drain", "name": "Shinigami Drain", "count": 1},
                          s.get_bank("amy"))
            s.close()

    def test_milestone_points_banks_rewind(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            for _ in range(24):
                s.award("amy", 10)
            self.assertNotIn("rewind", [r["power"] for r in s.get_bank("amy")])
            s.award("amy", 10)  # 25th point
            self.assertIn({"power": "rewind", "name": "DeLorean Rewind", "count": 1},
                          s.get_bank("amy"))
            s.close()

    def test_milestone_wins_banks_strength(self):
        with tempfile.TemporaryDirectory() as d:
            s = ScoreStore(str(Path(d) / "scores.db"))
            for _ in range(4):
                s.add_battle_win("amy")
            self.assertEqual(s.get_bank("amy"), [])
            s.add_battle_win("amy")  # 5th win
            self.assertEqual(s.get_bank("amy"),
                             [{"power": "strength", "name": "Super Saiyan Strength", "count": 1}])
            s.add_battle_win("amy")  # 6th win — no second grant
            self.assertEqual(s.get_bank("amy"),
                             [{"power": "strength", "name": "Super Saiyan Strength", "count": 1}])
            s.close()


class TestBattles(unittest.TestCase):
    def _manager(self, clock=None):
        questions = [
            Question("easy q?", "EasyAns", category="general", difficulty="easy"),
            Question("medium q?", "MedAns", category="general", difficulty="medium"),
            Question("hard q?", "HardAns", category="general", difficulty="hard"),
        ]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scores = ScoreStore(str(Path(tmp.name) / "scores.db"))
        self.addCleanup(scores.close)
        manager = BattleManager(questions, scores, now=(lambda: clock[0]) if clock is not None else None)
        return manager, scores

    def _started_battle(self, manager, challenger="alice", opponent="bob"):
        result = manager.challenge(challenger, opponent)
        self.assertTrue(result["ok"], result)
        result = manager.accept(opponent)
        self.assertTrue(result["ok"], result)
        battle = result["battle"]
        manager.next_question(battle)
        return battle

    def test_challenge_accept_decline(self):
        manager, _ = self._manager()
        self.assertFalse(manager.challenge("alice", "alice")["ok"])  # can't fight yourself
        result = manager.challenge("alice", "bob")
        self.assertTrue(result["ok"])
        self.assertIsNotNone(manager.pending)
        # Only the challenged player can accept/decline.
        self.assertFalse(manager.accept("alice")["ok"])
        self.assertFalse(manager.decline("alice")["ok"])
        self.assertTrue(manager.decline("bob")["ok"])
        self.assertIsNone(manager.pending)
        self.assertFalse(manager.accept("bob")["ok"])  # nothing left to accept

    def test_challenge_expiry(self):
        clock = [1000.0]
        manager, _ = self._manager(clock=clock)
        manager.challenge("alice", "bob")
        self.assertIsNone(manager.expire_pending())
        clock[0] = 1000.0 + 61
        expired = manager.expire_pending()
        self.assertIsNotNone(expired)
        self.assertEqual(expired.challenger, "alice")
        self.assertIsNone(manager.pending)

    def test_correct_answer_deals_damage(self):
        manager, _ = self._manager()
        battle = self._started_battle(manager)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        result = manager.answer("alice", "EasyAns")
        self.assertEqual(result["status"], "hit")
        self.assertFalse(result["battle_over"])
        # Easy base 10 + attacker level 1 = 11 damage.
        self.assertEqual(battle.hp["bob"], 100 - 11)
        self.assertEqual(battle.hp["alice"], 100)
        self.assertEqual(battle.rounds_played, 1)

    def test_wrong_answer_self_damage_and_lockout(self):
        manager, _ = self._manager()
        battle = self._started_battle(manager)
        battle.question = next(q for q in manager.questions if q.difficulty == "medium")
        result = manager.answer("alice", "nope")
        self.assertEqual(result["status"], "self")
        # Medium self-damage is 7.
        self.assertEqual(battle.hp["alice"], 100 - 7)
        # Locked out for the round: a second guess is ignored.
        result = manager.answer("alice", "MedAns")
        self.assertEqual(result["status"], "spent")
        self.assertEqual(battle.hp["alice"], 100 - 7)
        # The other fighter can still answer correctly.
        result = manager.answer("bob", "MedAns")
        self.assertEqual(result["status"], "hit")
        self.assertEqual(battle.hp["alice"], 100 - 7 - 16)  # medium 15 + bob level 1

    def test_both_wrong_fizzles_round(self):
        manager, _ = self._manager()
        battle = self._started_battle(manager)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("alice", "nope")
        result = manager.answer("bob", "also nope")
        self.assertEqual(result["status"], "fizzle")
        self.assertEqual(battle.rounds_played, 1)
        self.assertFalse(result["battle_over"])

    def test_timeout_advances_round(self):
        manager, _ = self._manager()
        battle = self._started_battle(manager)
        result = manager.timeout_round()
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(battle.rounds_played, 1)
        self.assertEqual(battle.hp["alice"], 100)
        self.assertFalse(result["battle_over"])

    def test_ko_ends_battle_and_awards_xp(self):
        manager, scores = self._manager()
        battle = self._started_battle(manager)
        battle.hp["bob"] = 5
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        result = manager.answer("alice", "EasyAns")
        self.assertTrue(result["battle_over"])
        self.assertEqual(result["winner"], "alice")
        self.assertIsNone(manager.active)
        self.assertEqual(scores.get_stats("alice")["xp"], 50)
        self.assertEqual(scores.get_stats("bob")["xp"], 10)
        self.assertEqual(scores.get_stats("alice")["battle_wins"], 1)
        # Both fighters are on cooldown now.
        self.assertTrue(manager.on_cooldown("alice"))
        self.assertTrue(manager.on_cooldown("bob"))
        self.assertFalse(manager.challenge("alice", "bob")["ok"])

    def test_self_ko_loses(self):
        manager, _ = self._manager()
        battle = self._started_battle(manager)
        battle.hp["alice"] = 5
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        result = manager.answer("alice", "nope")
        self.assertTrue(result["battle_over"])
        self.assertEqual(result["winner"], "bob")

    def test_max_rounds_higher_hp_wins(self):
        manager, scores = self._manager()
        battle = self._started_battle(manager)
        battle.hp["alice"] = 50
        battle.hp["bob"] = 80
        battle.rounds_played = BATTLE_MAX_ROUNDS - 1
        result = manager.timeout_round()
        self.assertTrue(result["battle_over"])
        self.assertEqual(result["winner"], "bob")
        self.assertEqual(scores.get_stats("bob")["xp"], 50)

    def test_max_rounds_equal_hp_draws(self):
        manager, scores = self._manager()
        battle = self._started_battle(manager)
        battle.rounds_played = BATTLE_MAX_ROUNDS - 1
        result = manager.timeout_round()
        self.assertTrue(result["battle_over"])
        self.assertTrue(result["draw"])
        self.assertIsNone(result["winner"])
        self.assertEqual(scores.get_stats("alice")["xp"], 25)
        self.assertEqual(scores.get_stats("bob")["xp"], 25)

    def test_cooldown_expires(self):
        clock = [1000.0]
        manager, _ = self._manager(clock=clock)
        battle = self._started_battle(manager)
        battle.hp["bob"] = 1
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("alice", "EasyAns")
        self.assertTrue(manager.on_cooldown("alice"))
        clock[0] += BATTLE_COOLDOWN_SECONDS + 1
        self.assertFalse(manager.on_cooldown("alice"))
        self.assertTrue(manager.challenge("alice", "bob")["ok"])

    def test_non_fighter_answers_ignored(self):
        manager, _ = self._manager()
        battle = self._started_battle(manager)
        result = manager.answer("carol", battle.question.answer)
        self.assertEqual(result["status"], "not-fighter")
        self.assertEqual(battle.hp["bob"], 100)


class TestBattleAbilities(unittest.TestCase):
    def _manager_with_powers(self, *power_ids, opponent_powers=()):
        """BattleManager where alice has the given powers banked."""
        questions = [
            Question("easy q?", "EasyAns", category="general", difficulty="easy"),
            Question("medium q?", "MedAns", category="general", difficulty="medium"),
            Question("hard q?", "HardAns", category="general", difficulty="hard"),
        ]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scores = ScoreStore(str(Path(tmp.name) / "scores.db"))
        self.addCleanup(scores.close)
        for power_id in power_ids:
            self.assertTrue(scores.bank_power("alice", power_id)["granted"])
        for power_id in opponent_powers:
            self.assertTrue(scores.bank_power("bob", power_id)["granted"])
        manager = BattleManager(questions, scores)
        manager.challenge("alice", "bob")
        manager.accept("bob")
        battle = manager.active
        manager.next_question(battle)
        return manager, scores, battle

    def test_guard_blocks_one_hit(self):
        manager, _, battle = self._manager_with_powers("guard")
        result = manager.use_ability("alice", "guard")
        self.assertTrue(result["ok"], result)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("bob", "EasyAns")
        self.assertEqual(battle.hp["alice"], 100)  # blocked
        # Guard is spent: the next hit lands.
        manager.next_question(battle)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("bob", "EasyAns")
        self.assertEqual(battle.hp["alice"], 100 - 11)

    def test_guard_not_banked(self):
        manager, _, _ = self._manager_with_powers()
        result = manager.use_ability("alice", "guard")
        self.assertFalse(result["ok"])
        self.assertIn("banked", result["reason"].lower())
        self.assertIn("Black Panther", result["reason"])

    def test_old_alias_shield_maps_to_guard(self):
        manager, _, battle = self._manager_with_powers("guard")
        result = manager.use_ability("alice", "shield")
        self.assertTrue(result["ok"], result)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("bob", "EasyAns")
        self.assertEqual(battle.hp["alice"], 100)

    def test_strength_doubles_damage(self):
        manager, _, battle = self._manager_with_powers("strength")
        result = manager.use_ability("alice", "strength")
        self.assertTrue(result["ok"], result)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("alice", "EasyAns")
        self.assertEqual(battle.hp["bob"], 100 - 22)  # (10 + 1) * 2

    def test_heal_restores_hp_once(self):
        manager, _, battle = self._manager_with_powers("heal")
        battle.hp["alice"] = 50
        result = manager.use_ability("alice", "heal")
        self.assertTrue(result["ok"], result)
        self.assertEqual(battle.hp["alice"], 80)
        # Once per battle.
        result = manager.use_ability("alice", "heal")
        self.assertFalse(result["ok"])
        self.assertEqual(battle.hp["alice"], 80)

    def test_drain_bonus_damage_and_heal(self):
        manager, _, battle = self._manager_with_powers("drain")
        battle.hp["alice"] = 90
        result = manager.use_ability("alice", "drain")
        self.assertTrue(result["ok"], result)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("alice", "EasyAns")
        self.assertEqual(battle.hp["bob"], 100 - 21)  # (10 + 1) + 10 drain bonus
        self.assertEqual(battle.hp["alice"], 100)  # healed 10, capped at max 100

    def test_rewind_swaps_question(self):
        manager, _, battle = self._manager_with_powers("rewind")
        old = battle.question
        old_token = battle.round_token
        result = manager.use_ability("alice", "rewind")
        self.assertTrue(result["ok"], result)
        self.assertIsNotNone(result["new_question"])
        self.assertNotEqual(battle.question, old)
        self.assertGreater(battle.round_token, old_token)
        # Once per battle.
        self.assertFalse(manager.use_ability("alice", "rewind")["ok"])

    def test_speed_grants_second_try(self):
        manager, _, battle = self._manager_with_powers("speed")
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        result = manager.use_ability("alice", "speed")
        self.assertTrue(result["ok"], result)
        # First guess wrong: self-damage, locked out...
        result = manager.answer("alice", "nope")
        self.assertEqual(result["status"], "self")
        self.assertEqual(battle.hp["alice"], 100 - 5)
        # ...but Flash Speed lets her try again in the same round.
        result = manager.answer("alice", "EasyAns")
        self.assertEqual(result["status"], "hit")
        self.assertEqual(battle.hp["bob"], 100 - 11)

    def test_speed_not_armed_means_lockout(self):
        manager, _, battle = self._manager_with_powers()
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("alice", "nope")
        result = manager.answer("alice", "EasyAns")
        self.assertEqual(result["status"], "spent")

    def test_dodge_negates_self_damage(self):
        manager, _, battle = self._manager_with_powers("dodge")
        battle.question = next(q for q in manager.questions if q.difficulty == "medium")
        result = manager.use_ability("alice", "dodge")
        self.assertTrue(result["ok"], result)
        result = manager.answer("alice", "nope")
        self.assertEqual(result["status"], "self")
        self.assertEqual(battle.hp["alice"], 100)  # no self-damage
        # Still locked out for the round — dodge only removes the damage.
        result = manager.answer("alice", "MedAns")
        self.assertEqual(result["status"], "spent")

    def test_rage_bonus_damage(self):
        manager, _, battle = self._manager_with_powers("rage")
        result = manager.use_ability("alice", "rage")
        self.assertTrue(result["ok"], result)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("alice", "EasyAns")
        self.assertEqual(battle.hp["bob"], 100 - 26)  # (10 + 1) + 15

    def test_bounce_halves_damage(self):
        manager, _, battle = self._manager_with_powers(opponent_powers=("bounce",))
        result = manager.use_ability("bob", "bounce")
        self.assertTrue(result["ok"], result)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("alice", "EasyAns")
        self.assertEqual(battle.hp["bob"], 100 - 5)  # 11 halved, floor 1
        # Bounce is spent: the next hit lands full.
        manager.next_question(battle)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        manager.answer("alice", "EasyAns")
        self.assertEqual(battle.hp["bob"], 100 - 5 - 11)

    def test_genjutsu_turns_correct_answer_wrong(self):
        manager, _, battle = self._manager_with_powers("genjutsu")
        result = manager.use_ability("alice", "genjutsu")
        self.assertTrue(result["ok"], result)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        # Bob answers RIGHT — but the illusion makes it count as wrong.
        result = manager.answer("bob", "EasyAns")
        self.assertEqual(result["status"], "self")
        self.assertEqual(battle.hp["bob"], 100 - 5)
        self.assertEqual(battle.hp["alice"], 100)
        # Hex is spent: next round Bob's correct answer lands normally.
        manager.next_question(battle)
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        result = manager.answer("bob", "EasyAns")
        self.assertEqual(result["status"], "hit")
        self.assertEqual(battle.hp["alice"], 100 - 11)

    def test_revive_saves_from_ko(self):
        manager, _, battle = self._manager_with_powers("revive")
        result = manager.use_ability("alice", "revive")
        self.assertTrue(result["ok"], result)
        battle.hp["alice"] = 5
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        result = manager.answer("bob", "EasyAns")
        self.assertFalse(result["battle_over"])  # 1-Up saves her
        self.assertEqual(battle.hp["alice"], 25)
        self.assertIsNotNone(manager.active)
        # Revive is spent: the next KO is real.
        manager.next_question(battle)
        battle.hp["alice"] = 5
        battle.question = next(q for q in manager.questions if q.difficulty == "easy")
        result = manager.answer("bob", "EasyAns")
        self.assertTrue(result["battle_over"])
        self.assertEqual(result["winner"], "bob")

    def test_power_used_once_per_battle_even_when_banked_twice(self):
        manager, scores, battle = self._manager_with_powers("guard", "guard")
        self.assertEqual(scores.bank_total("alice"), 2)
        self.assertTrue(manager.use_ability("alice", "guard")["ok"])
        self.assertEqual(scores.bank_total("alice"), 1)  # one spent
        result = manager.use_ability("alice", "guard")
        self.assertFalse(result["ok"])
        self.assertIn("already used", result["reason"].lower())
        self.assertEqual(scores.bank_total("alice"), 1)  # the spare stays banked

    def test_unknown_power_rejected(self):
        manager, _, _ = self._manager_with_powers()
        result = manager.use_ability("alice", "kryptonite")
        self.assertFalse(result["ok"])
        self.assertIn("unknown", result["reason"].lower())

    def test_ability_outside_battle_rejected(self):
        questions = [Question("q?", "a", category="general", difficulty="easy")]
        with tempfile.TemporaryDirectory() as d:
            scores = ScoreStore(str(Path(d) / "scores.db"))
            manager = BattleManager(questions, scores)
            result = manager.use_ability("alice", "guard")
            self.assertFalse(result["ok"])
            scores.close()


class TestBattleOverlay(unittest.TestCase):
    def test_build_state_includes_battle(self):
        from overlay import OVERLAY_HTML, build_state

        questions = load_questions(HERE / "questions.json")
        with tempfile.TemporaryDirectory() as d:
            scores = ScoreStore(str(Path(d) / "scores.db"))
            m = QuizManager(questions, scores, timeout_seconds=30)
            state = build_state(m)
            self.assertIn("battle", state)
            self.assertIsNone(state["battle"])
            # Challenge -> pending battle shows up.
            m.battle.challenge("alice", "bob")
            state = build_state(m)
            self.assertEqual(state["battle"]["status"], "pending")
            self.assertEqual(state["battle"]["challenger"], "alice")
            # Accept + ask -> active battle with HP bars and a question.
            m.battle.accept("bob")
            m.battle.next_question(m.battle.active)
            state = build_state(m)
            b = state["battle"]
            self.assertEqual(b["status"], "active")
            self.assertEqual(b["hp"], {"alice": 100, "bob": 100})
            self.assertEqual(b["max_hp"], {"alice": 100, "bob": 100})
            self.assertEqual(b["bank"], {"alice": [], "bob": []})
            self.assertIsInstance(b["question"], str)
            self.assertTrue(b["question"])
            self.assertEqual(b["max_rounds"], BATTLE_MAX_ROUNDS)
            scores.close()

        self.assertIn("BATTLE", OVERLAY_HTML)
        self.assertIn("hpbar", OVERLAY_HTML)
        self.assertIn("battle-panel", OVERLAY_HTML)
        self.assertIn("state.battle", OVERLAY_HTML)
        self.assertIn("powerfeed", OVERLAY_HTML)
        self.assertIn("powerchip", OVERLAY_HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
