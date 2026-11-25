"""Live overlay for the kingdamienjl Twitch quiz bot.

A small aiohttp server (no new dependencies) that serves one page for OBS
browser sources plus a JSON state endpoint the page polls:

  GET /           the overlay page (transparent background, OBS-friendly)
  GET /state.json live state: leaderboard with XP bars, the active round,
                  recent level-ups for the level-up animation, and the
                  active battle (HP bars, battle question, event feed)

The page polls /state.json every 2 seconds. When a level-up appears in the
feed, the page plays a big animated LEVEL UP banner. Nothing here needs
Twitch credentials; it only reads the QuizManager's in-memory state and
the persistent ScoreStore.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from quiz import QuizManager

POLL_MS = 2000
LEVEL_UP_SHOW_SECONDS = 8  # a level-up counts as "fresh" for this long
LEVEL_UP_BANNER_MS = 6000  # how long the banner stays on screen

OVERLAY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Quiz Overlay — kingdamienjl</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { background: transparent; }
  body {
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    color: #fff;
    text-shadow: 0 2px 6px rgba(0,0,0,.9);
    padding: 16px;
  }
  .panel {
    background: rgba(10, 10, 18, 0.62);
    border: 2px solid rgba(255, 215, 0, 0.55);
    border-radius: 14px;
    padding: 14px 18px;
    margin-bottom: 14px;
    backdrop-filter: blur(2px);
  }
  h1 { font-size: 30px; letter-spacing: 1px; color: #ffd700; margin-bottom: 10px; }
  h1 .sub { font-size: 16px; color: #ccc; letter-spacing: 0; }
  .question { font-size: 26px; line-height: 1.3; }
  .meta { font-size: 17px; color: #ffd700; margin-bottom: 6px; }
  .meta .diff-easy { color: #4ade80; }
  .meta .diff-medium { color: #fbbf24; }
  .meta .diff-hard { color: #f87171; }
  .timerbar { height: 10px; background: rgba(255,255,255,.18); border-radius: 5px; overflow: hidden; margin-top: 10px; }
  .timerbar > div { height: 100%; background: #ffd700; transition: width 1s linear; }
  .idle { font-size: 20px; color: #bbb; }
  table { width: 100%; border-collapse: collapse; font-size: 20px; }
  td, th { padding: 6px 8px; text-align: left; vertical-align: middle; }
  th { color: #ffd700; font-size: 15px; text-transform: uppercase; letter-spacing: 1px; }
  tr + tr td { border-top: 1px solid rgba(255,255,255,.12); }
  .lvl {
    display: inline-block; min-width: 44px; text-align: center;
    background: #ffd700; color: #111; font-weight: 800;
    border-radius: 20px; padding: 2px 8px; text-shadow: none;
  }
  .xpbar { width: 150px; height: 12px; background: rgba(255,255,255,.18); border-radius: 6px; overflow: hidden; }
  .xpbar > div { height: 100%; background: linear-gradient(90deg, #22c55e, #a3e635); }
  .xpnum { font-size: 15px; color: #ddd; white-space: nowrap; }
  .pts { color: #ffd700; font-weight: 700; white-space: nowrap; }
  #levelup {
    display: none; position: fixed; left: 50%; top: 34%; transform: translate(-50%, -50%);
    background: rgba(20, 12, 0, 0.88); border: 4px solid #ffd700; border-radius: 22px;
    padding: 26px 44px; text-align: center; z-index: 10;
    box-shadow: 0 0 60px rgba(255, 215, 0, 0.75);
  }
  #levelup.show { display: block; animation: pop 0.5s ease-out, glow 1.2s ease-in-out infinite alternate; }
  #levelup .big { font-size: 52px; font-weight: 900; color: #ffd700; }
  #levelup .who { font-size: 30px; margin-top: 6px; }
  @keyframes pop {
    0% { transform: translate(-50%, -50%) scale(0.4); opacity: 0; }
    70% { transform: translate(-50%, -50%) scale(1.12); opacity: 1; }
    100% { transform: translate(-50%, -50%) scale(1); opacity: 1; }
  }
  @keyframes glow {
    from { box-shadow: 0 0 30px rgba(255, 215, 0, 0.45); }
    to { box-shadow: 0 0 70px rgba(255, 215, 0, 0.95); }
  }
  /* --- battle panel --- */
  #battle-panel { display: none; border-color: rgba(248, 113, 113, 0.65); }
  #battle-panel.show { display: block; }
  .fighter { margin-bottom: 10px; }
  .fighter .fname { font-size: 22px; font-weight: 800; }
  .fighter .fname .lvl { margin-left: 6px; }
  .hpbar { height: 18px; background: rgba(255,255,255,.15); border-radius: 9px; overflow: hidden; margin-top: 4px; }
  .hpbar > div { height: 100%; background: linear-gradient(90deg, #ef4444, #f97316); transition: width 0.6s ease; }
  .hpbar.low > div { background: linear-gradient(90deg, #dc2626, #991b1b); }
  .hpnum { font-size: 15px; color: #ddd; }
  .vs { text-align: center; font-size: 24px; font-weight: 900; color: #f87171; margin: 2px 0 8px; }
  .bround { font-size: 16px; color: #fbbf24; margin-bottom: 6px; }
  .bquestion { font-size: 22px; line-height: 1.3; margin-top: 8px; }
  .bfeed { margin-top: 10px; font-size: 17px; line-height: 1.45; }
  .bfeed div { padding: 2px 0; }
  .bfeed .kind-hit { color: #fca5a5; }
  .bfeed .kind-self { color: #fbbf24; }
  .bfeed .kind-guard { color: #93c5fd; }
  .bfeed .kind-heal { color: #4ade80; }
  .bfeed .kind-strength { color: #f472b6; }
  .bfeed .kind-drain { color: #c084fc; }
  .bfeed .kind-rewind { color: #67e8f9; }
  .bfeed .kind-speed { color: #fde047; }
  .bfeed .kind-dodge { color: #fdba74; }
  .bfeed .kind-rage { color: #f87171; }
  .bfeed .kind-bounce { color: #f0abfc; }
  .bfeed .kind-genjutsu { color: #a78bfa; }
  .bfeed .kind-revive { color: #86efac; }
  .bfeed .kind-win { color: #ffd700; font-weight: 800; }
  .bfeed .kind-info { color: #ddd; }
  .powerchip {
    display: inline-block; background: rgba(255, 215, 0, 0.18);
    border: 1px solid rgba(255, 215, 0, 0.55); color: #ffd700;
    border-radius: 10px; padding: 1px 8px; margin: 2px 3px 0 0;
    font-size: 14px; font-weight: 700; text-shadow: none; white-space: nowrap;
  }
  .powerfeed { margin-top: 10px; font-size: 17px; line-height: 1.45; }
  .powerfeed div { padding: 2px 0; color: #fde047; }
</style>
</head>
<body>
  <div id="levelup"><div class="big">&#127881; LEVEL UP! &#127881;</div><div class="who" id="levelup-who"></div></div>

  <div class="panel" id="battle-panel">
    <h1>&#9876;&#65039; BATTLE <span class="sub" id="battle-sub"></span></h1>
    <div id="battle-body"></div>
  </div>

  <div class="panel" id="round-panel">
    <h1>&#129504; QUIZ <span class="sub">— kingdamienjl</span></h1>
    <div id="round-body"><div class="idle">No round running — type !quiz to start one!</div></div>
  </div>

  <div class="panel">
    <h1>&#127942; LEADERBOARD</h1>
    <table>
      <thead><tr><th>#</th><th>Player</th><th>Level</th><th>XP</th><th>Points</th></tr></thead>
      <tbody id="board"><tr><td colspan="5" class="idle">No scores yet — be the first!</td></tr></tbody>
    </table>
    <div class="powerfeed" id="powerfeed"></div>
  </div>

<script>
  const shownLevelUps = new Set();
  let bannerTimer = null;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }

  function render(state) {
    const now = state.now;
    // --- active round ---
    const body = document.getElementById("round-body");
    const r = state.active_round;
    if (r) {
      const left = Math.max(0, r.ends_at - now);
      const pct = Math.max(0, Math.min(100, 100 * left / r.timeout_seconds));
      body.innerHTML =
        '<div class="meta">[' + esc(r.category) + ' &bull; <span class="diff-' + esc(r.difficulty) + '">' +
        esc(r.difficulty.toUpperCase()) + ' &bull; ' + r.xp_value + ' XP</span>] &nbsp;' +
        Math.ceil(left) + 's left</div>' +
        '<div class="question">' + esc(r.question) + '</div>' +
        '<div class="timerbar"><div style="width:' + pct + '%"></div></div>';
    } else {
      body.innerHTML = '<div class="idle">No round running &mdash; type !quiz to start one!</div>';
    }

    // --- leaderboard (with banked powers) ---
    const board = document.getElementById("board");
    if (!state.leaderboard.length) {
      board.innerHTML = '<tr><td colspan="5" class="idle">No scores yet &mdash; be the first!</td></tr>';
    } else {
      board.innerHTML = state.leaderboard.map((row, i) => {
        const pct = Math.round(100 * row.xp_into_level / 100);
        const chips = (row.bank || []).map(p =>
          '<span class="powerchip">&#9889; ' + esc(p.name) + (p.count > 1 ? ' x' + p.count : '') + '</span>'
        ).join("");
        return '<tr><td>' + (i + 1) + '</td><td>' + esc(row.username) +
          (chips ? '<br>' + chips : '') + '</td>' +
          '<td><span class="lvl">Lv ' + row.level + '</span></td>' +
          '<td><div class="xpbar"><div style="width:' + pct + '%"></div></div>' +
          '<div class="xpnum">' + row.xp + ' XP (' + row.xp_into_level + '/100 to Lv ' + (row.level + 1) + ')</div></td>' +
          '<td class="pts">' + row.points + ' pts</td></tr>';
      }).join("");
    }

    // --- recent power grants ---
    const pfeed = document.getElementById("powerfeed");
    const freshPowers = (state.power_feed || []).filter(e => (now - e.at) < 30);
    pfeed.innerHTML = freshPowers.map(e =>
      '<div>&#9889; @' + esc(e.username) + ' answered about ' + esc(e.character) +
      ' and gained ' + esc(e.name) + '!</div>'
    ).join("");

    // --- battle ---
    const bpanel = document.getElementById("battle-panel");
    const b = state.battle;
    if (!b) {
      bpanel.classList.remove("show");
    } else {
      bpanel.classList.add("show");
      document.getElementById("battle-sub").textContent =
        b.status === "pending" ? "— challenge waiting" :
        b.status === "done" ? "— " + (b.draw ? "DRAW!" : "@" + b.winner + " wins!") :
        "— Round " + b.round + "/" + b.max_rounds;
      const body = document.getElementById("battle-body");
      if (b.status === "pending") {
        body.innerHTML = '<div class="bquestion">@' + esc(b.challenger) + ' challenged @' + esc(b.opponent) +
          ' — type !accept in chat!</div>';
      } else {
        const fighterHtml = (name) => {
          const hp = b.hp[name] || 0, max = b.max_hp[name] || 1;
          const pct = Math.max(0, Math.min(100, 100 * hp / max));
          const low = pct <= 25 ? " low" : "";
          const chips = ((b.bank && b.bank[name]) || []).map(p =>
            '<span class="powerchip">&#9889; ' + esc(p.name) + (p.count > 1 ? ' x' + p.count : '') + '</span>'
          ).join("");
          return '<div class="fighter"><div class="fname">@' + esc(name) +
            '<span class="lvl">Lv ' + (b.level[name] || 1) + '</span></div>' +
            '<div class="hpbar' + low + '"><div style="width:' + pct + '%"></div></div>' +
            '<div class="hpnum">' + hp + ' / ' + max + ' HP</div>' +
            (chips ? '<div>' + chips + '</div>' : '') + '</div>';
        };
        let html = fighterHtml(b.challenger) + '<div class="vs">VS</div>' + fighterHtml(b.opponent);
        if (b.status === "active" && b.question) {
          html += '<div class="bround">Round ' + b.round + ' of ' + b.max_rounds + ' — fighters only!</div>' +
            '<div class="bquestion">' + esc(b.question) + '</div>';
        }
        if (b.feed && b.feed.length) {
          html += '<div class="bfeed">' + b.feed.map(e =>
            '<div class="kind-' + esc(e.kind) + '">' + esc(e.text) + '</div>').join("") + '</div>';
        }
        body.innerHTML = html;
      }
    }

    // --- level-up animation ---
    const fresh = state.level_ups.filter(e => (now - e.at) < 8);
    for (const e of fresh) {
      const key = e.username + "@" + e.at;
      if (shownLevelUps.has(key)) continue;
      shownLevelUps.add(key);
      const banner = document.getElementById("levelup");
      document.getElementById("levelup-who").textContent = "@" + e.username + " reached Level " + e.level + "!";
      banner.classList.remove("show");
      void banner.offsetWidth; // restart the pop animation
      banner.classList.add("show");
      clearTimeout(bannerTimer);
      bannerTimer = setTimeout(() => banner.classList.remove("show"), 6000);
    }
  }

  async function tick() {
    try {
      const res = await fetch("state.json", {cache: "no-store"});
      if (res.ok) render(await res.json());
    } catch (err) { /* overlay keeps last state; retry next tick */ }
  }
  tick();
  setInterval(tick, 2000);
</script>
</body>
</html>
"""


def build_state(quiz: QuizManager, *, limit: int = 5) -> dict:
    """JSON-able snapshot for the overlay page. Pure read of quiz state."""
    now = time.time()
    active = quiz.active
    round_state = None
    if active is not None and not active.expired:
        remaining = max(0.0, active.timeout_seconds - (time.monotonic() - active.started_at))
        round_state = {
            "question": active.question.question,
            "category": active.question.category,
            "difficulty": active.question.difficulty,
            "xp_value": active.question.xp_value,
            "timeout_seconds": active.timeout_seconds,
            "ends_at": now + remaining,
        }
    return {
        "now": now,
        "leaderboard": quiz.scores.leaderboard_with_xp(limit),
        "active_round": round_state,
        "battle": quiz.battle.state_for_overlay(),
        "level_ups": [
            {"username": e["username"], "level": e["level"], "at": e["at"]}
            for e in quiz.level_up_feed
            if (now - e["at"]) < LEVEL_UP_SHOW_SECONDS * 4
        ],
        "power_feed": [
            {"username": e["username"], "power": e["power"], "name": e["name"],
             "character": e["character"], "at": e["at"]}
            for e in quiz.power_feed
            if (now - e["at"]) < 30
        ],
    }


class OverlayServer:
    """Serves the overlay page + state endpoint on its own aiohttp app."""

    def __init__(self, quiz: QuizManager, *, host: str = "0.0.0.0", port: int = 8765):
        self.quiz = quiz
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def _make_app(self) -> web.Application:
        app = web.Application()

        async def index(_request: web.Request) -> web.Response:
            return web.Response(text=OVERLAY_HTML, content_type="text/html")

        async def state(_request: web.Request) -> web.Response:
            return web.json_response(build_state(self.quiz))

        app.router.add_get("/", index)
        app.router.add_get("/state.json", state)
        return app

    async def start(self) -> None:
        """Bind and start listening. Raises OSError if the port is taken."""
        self._runner = web.AppRunner(self._make_app())
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        if self._runner.addresses:
            self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"
