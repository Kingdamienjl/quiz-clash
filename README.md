# Quiz Clash — Twitch quiz + battle bot for kingdamienjl

A Twitch chat quiz bot with mixed categories, proactive rounds while the
stream is live, a persistent leaderboard, and Athena herself hanging out
in chat. Built on **TwitchIO 3.3.2** (EventSub chat — no IRC).

## What it does

- `!quiz [category]` — start a round. Categories: `gta`, `anime`, `gaming`,
  `general`, `movies`, `music`. 30 seconds to answer, first correct answer
  in chat wins 1 point.
- `!score` — your points, XP, level, HP, battle wins and streak.
  `!leaderboard` — top 5 players.
- `!quizstop` — mods / broadcaster stop the active round and pause auto-rounds.
  `!quizresume` — mods / broadcaster resume auto-rounds after a pause.
- `!quizhelp` — list the commands.
- **Quiz battles** — `!fight @user` challenges someone to a head-to-head
  battle: HP scales with level, first correct answer in a battle round
  deals damage, wrong answers hurt the answerer, winner takes 50 XP.
  `!accept` / `!decline` answer a challenge, `!battle` shows the fight,
  `!ability <name>` spends a banked power, `!abilities` lists yours.
- **Themed powers** — answer a question about a character and you bank
  their power (The Flash -> Flash Speed, Goku -> Super Saiyan Strength,
  Mario -> 1-Up Mushroom). Bank up to 3, spend them in battle.
- **Proactive rounds** — while the stream is live, the bot asks a question
  on its own about every 15 minutes (Helix live check first; silent offline,
  and never right after a round just ended).
- **Athena in chat** — when someone mentions Athena by name, she replies
  (Fails PC Ollama `hermes3:8b`, local, no paid API), at most once per
  ~20 seconds. If Ollama is down she stays quiet — she never fakes a reply.
- **Scores persist** in SQLite (`data/scores.db`) across restarts.

## Project layout

| File | What it is |
|---|---|
| `bot.py` | The bot: TwitchIO AutoBot, commands, proactive loop, Athena hookup |
| `quiz.py` | Pure quiz logic (bank, matching, rounds, scores) — testable offline |
| `rpg.py` | Pure RPG battle logic (HP, damage, powers, battles) — testable offline |
| `powers.py` | The themed power catalog (single source of truth) + fallbacks |
| `athena.py` | Ollama client, persona, rate limit, reply trimming |
| `config.py` | `.env` / environment loading, cross-platform paths |
| `questions.json` | 157 questions (gta 25, anime 28, gaming 25, general 25, movies 29, music 25), each with accepted alternates |
| `requirements.txt` | `twitchio>=3.3.2`, `aiohttp`, `asqlite` |
| `config.example` | Copy to `.env` and fill in (never commit `.env`) |
| `twitch-quiz-bot.service` | systemd unit for the R810 LXC |
| `test_bot.py` | Offline tests — `python test_bot.py` (90 tests, all passing) |

## Setup (any machine)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip install -r requirements.txt
cp config.example .env                     # then fill in .env (see below)
.venv/bin/python bot.py                    # Windows: .venv\Scripts\python bot.py
```

## One-time Twitch authorization (Chris — this part needs you)

The bot logs in as **kingdamienjl himself** (bot id == owner id == `59499420`,
already filled in). Nobody can do this step for you — Twitch needs your
browser login once.

1. **Create the app** — go to https://dev.twitch.tv/console, sign in as
   kingdamienjl, *Register Your Application*. Name it anything (e.g.
   `kingdamienjl-quiz-bot`), category *Chat Bot*, and add this OAuth
   Redirect URL exactly:
   `http://localhost:4343/oauth/callback`
   Copy the **Client ID** and **Client Secret** into `.env`.
2. **Generate the token** — easiest with the Twitch CLI
   (https://github.com/twitchdev/twitch-cli — install it, then run):
   ```bash
   twitch token -u -s "user:read:chat user:write:chat user:bot"
   ```
   A browser window opens — sign in as kingdamienjl and Authorize. The
   CLI prints an **access token** and a **refresh token**. Paste both into
   `.env` as `TWITCH_ACCESS_TOKEN` / `TWITCH_REFRESH_TOKEN`.
3. **Start the bot.** On first run it saves the tokens to
   `data/tokens.db` and refreshes them automatically from then on.

Scopes, and why each is needed:

| Scope | Why |
|---|---|
| `user:read:chat` | Read chat messages (answers, Athena mentions) |
| `user:write:chat` | Send messages (questions, scores, Athena replies) |
| `user:bot` | Required by Twitch when a bot account sends chat messages |

Alternative to step 2 (web flow): set `TWITCH_WITH_ADAPTER=true` in `.env`,
start the bot, and visit
`http://localhost:4343/oauth?scopes=user:read:chat%20user:write:chat%20user:bot&force_verify=true`
in a browser **on the same machine as the bot**. On the R810 LXC that means
SSH port-forwarding 4343 first — the CLI route above is simpler.

## XP, levels & live overlay

Every question has a difficulty (`easy` / `medium` / `hard`) — 157 questions
(gta 25, anime 28, gaming 25, general 25, movies 29, music 25), spread
across all three tiers (easy 57, medium 56, hard 44). `!quiz` takes an
optional difficulty: `!quiz gta hard`, `!quiz hard`, or just `!quiz`.

A correct answer earns XP by difficulty — **easy 10, medium 25, hard 50** —
and **every 100 XP is a level** (level 2 at 100 XP, level 3 at 200, ...).
Points still count correct answers (1 per win). XP and points persist in
`data/scores.db`, so levels survive restarts. Level-ups are announced in
chat. Tune the numbers at the top of `quiz.py` (`XP_EASY`, `XP_MEDIUM`,
`XP_HARD`, `XP_PER_LEVEL`).

## Quiz battles (RPG layer)

Levels turn into hit points: **max HP = 100 + 10 per level** (level 1 has
100 HP, level 5 has 140, level 10 has 190). HP is derived from level, so
it is never stored — it is set when a battle starts.

**How a battle works** (`!fight @user`, then the challenged player types
`!accept` within 60 seconds, or `!decline`):

- One battle at a time for the whole channel. A quiz round and a battle
  can never run at once, and both fighters are on a 5-minute cooldown
  after a battle ends.
- Up to **10 rounds**. Each round the bot asks a question from the normal
  bank (any category, any difficulty). Only the two fighters' answers
  count, and each fighter gets **one guess per round**.
- **First correct answer deals damage** to the other fighter: difficulty
  base (easy 10 / medium 15 / hard 20) **+ your level**. A level 1
  fighter answering an easy question hits for 11; a level 10 fighter
  answering a hard one hits for 30.
- **A wrong answer hurts you**: half the question's base damage to
  yourself (easy 5 / medium 7 / hard 10), and you are locked out for the
  rest of that round. If both fighters guess wrong, the round ends early.
- **Nobody answers in 30 seconds**: the round fizzles, no damage.
- The battle ends when a fighter hits **0 HP**, or after 10 rounds
  (higher HP wins; equal HP is a draw). **Winner takes 50 XP, loser takes
  10 XP, a draw is 25 XP each**, and the winner's battle-win count goes up.

**Themed powers** — the characters are the abilities. Every correct quiz
answer banks a power for your next battle (max **3 banked** at a time):

- Answer a question **about a character** and you bank *their* power:
  The Flash -> **Flash Speed**, Goku -> **Super Saiyan Strength**,
  Black Panther -> **Vibranium Guard**, Mario -> **1-Up Mushroom**, and
  so on. 63 questions carry a specific character tag.
- Any other question banks its **category's mascot power** instead of
  nothing: GTA -> Getaway Driver, anime -> Super Saiyan Strength,
  gaming -> 1-Up Mushroom, general -> Flash Speed, movies -> Vibranium
  Guard, music -> Senzu Bean.
- **Milestones** bank powers too: level 3 -> Vibranium Guard, 5 battle
  wins -> Super Saiyan Strength, best streak of 5 -> Senzu Bean,
  mastering a category (10 correct in one) -> Shinigami Drain,
  25 points -> DeLorean Rewind. Each fires once, when crossed.
- Powers persist in `data/scores.db` (old databases get the new table
  automatically). `!score` shows your bank; `!abilities` shows your
  bank plus the full list.

In battle, `!ability <power>` spends one banked power, **once per battle
each**. Old generic names still work as aliases (`shield` -> `guard`,
`power` -> `strength`, `reroll` -> `rewind`).

| Power | Character | Effect | How you earn it |
|---|---|---|---|
| `speed` — Flash Speed | The Flash | Your next wrong guess doesn't lock you out — a second try in the same round | Answer a Flash question (or another speedster) |
| `strength` — Super Saiyan Strength | Goku | Your next correct answer deals double damage | Answer a Goku / Dragon Ball Z question |
| `guard` — Vibranium Guard | Black Panther | Negate the next damage you would take | Answer a Black Panther / Wakanda question |
| `heal` — Senzu Bean | Dragon Ball | Restore 30 HP right now | Answer a Dragon Ball question |
| `drain` — Shinigami Drain | Ryuk | Your next correct answer deals +10 damage and heals you 10 HP | Answer a Death Note / Ryuk question |
| `rewind` — DeLorean Rewind | Back to the Future | Swap the current battle question for a new one | Answer a Back to the Future question |
| `dodge` — Getaway Driver | Franklin Clinton | Your next wrong answer costs 0 self-damage | Answer a Franklin / GTA question |
| `rage` — Titan Rage | Eren Yeager | Your next correct answer deals +15 damage | Answer an Attack on Titan question |
| `bounce` — Gum-Gum Bounce | Luffy | The next damage you take is halved | Answer a Luffy / One Piece question |
| `genjutsu` — Genjutsu | Kakashi | Your opponent's next answer counts as wrong | Answer a Kakashi / Naruto question |
| `revive` — 1-Up Mushroom | Mario | If you hit 0 HP, revive at 25 HP once | Answer a Mario question |

A wrong guess during a normal quiz round breaks your consecutive-correct
streak (Athena mentions don't count as guesses). Streaks, best streaks,
battle wins and per-category correct counts all persist in
`data/scores.db` — old databases are migrated in place, scores are never
wiped. Tune the battle numbers at the top of `rpg.py` (`BASE_HP`,
`BASE_DAMAGE`, `WINNER_XP`, `BATTLE_MAX_ROUNDS`, ...) and the power
catalog in `powers.py`.

The bot also serves a live overlay for OBS at
`http://<bot-host>:8765/` — add it as a **Browser Source** in OBS. It shows
the leaderboard with XP bars **and each player's banked powers**, the
current question with its countdown, a big animated LEVEL UP banner
whenever someone levels, and a feed of recent power grants ("@alice
answered about The Flash and gained Flash Speed!"). During a battle it
also shows **both fighters' HP bars and banked powers**, the battle
question, and a live feed of hits, blocks, heals and wins. It polls
`/state.json` every 2 seconds; no extra dependencies. Turn it off with
`OVERLAY_ENABLED=false` in `.env` (or change the port with
`OVERLAY_PORT`).

## Deploy to the R810 LXC

1. Create a small LXC on the R810 (Debian 12 template, 1 vCPU, 512 MB RAM,
   4 GB disk, DHCP on vmbr0). It needs outbound internet (Twitch) and a
   route to Fails PC Ollama — Tailscale on the LXC (uses the default
   `OLLAMA_URL=http://100.95.184.104:11434`), or the LAN fallback
   `OLLAMA_URL=http://192.168.1.221:11434` in `.env`.
2. Copy the project to `/opt/twitch-quiz-bot` on the LXC.
3. Install:
   ```bash
   apt update && apt install -y python3-venv
   cd /opt/twitch-quiz-bot
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp config.example .env   # fill in .env (see authorization above)
   ```
4. Install the service:
   ```bash
   cp twitch-quiz-bot.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now twitch-quiz-bot
   ```
5. Check it: `systemctl status twitch-quiz-bot` and
   `journalctl -u twitch-quiz-bot -f`. You should see "Logged in as bot id
   59499420". Test in chat with `!quizhelp`.

The service auto-restarts on crash. Tokens live in
`/opt/twitch-quiz-bot/data/tokens.db`, scores in `data/scores.db`.

## Adding questions

Edit `questions.json` (one entry per question: `question`, `answer`,
`alternates`, `category`). To tie a question to a themed power, add an
optional `"power"` field with one of the ids in `powers.py` (e.g.
`"power": "speed"` on a Flash question) — untagged questions grant their
category's fallback power. Then restart the bot. Run
`python test_bot.py` after editing — it checks for duplicates, bad
categories, bad powers, and missing fields. Only add facts you are sure
about.

## Troubleshooting

- **Bot starts but never answers** — check the logs first. If you see auth
  errors, the token is stale: re-run the Twitch CLI token step and update
  `.env` (delete `data/tokens.db` too, so the bad token isn't reloaded).
- **Quiz works but Athena never replies** — Ollama on Fails PC is
  unreachable from the bot host. Check `OLLAMA_URL` in `.env` (try the LAN
  address `http://192.168.1.221:11434`). The quiz keeps working either way.
- **Proactive rounds never fire** — the stream has to be live (Helix
  check). Command rounds (`!quiz`) work even when offline.
- **Answers not matching** — matching is case-insensitive, ignores
  punctuation and leading "the/a/an", and accepts the listed alternates.
  Add a missing spelling as an `alternate` in `questions.json`.

## Security notes

- Never commit `.env`, `data/tokens.db`, or `data/scores.db`. They hold
  live Twitch tokens. (`.gitignore` covers them.)
- Never paste tokens in chat. If one leaks, revoke it at
  https://dev.twitch.tv/console and generate a fresh one.
