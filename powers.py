"""Themed power catalog for the kingdamienjl Twitch quiz bot.

One system, one list: every battle power is a character/subject-flavored
version of a battle effect. You earn powers by KNOWING THINGS — answer a
question about The Flash, bank Flash Speed; answer about Goku, bank Super
Saiyan Strength. Powers are banked (max 3 at a time) and spent in battle
with ``!ability <power>``, once per battle each.

This module is the single source of truth for powers. ``quiz.py`` uses it
to tag questions and bank grants, ``rpg.py`` uses it for battle effects,
``bot.py``/``overlay.py`` use it for display. It imports nothing from the
project, so there are no import cycles.

Effect kinds (what the battle engine does with each power):
  speed    — your next wrong guess doesn't lock you out (one extra try)
  strength — your next correct answer deals double damage
  guard    — negate the next damage you would take
  heal     — restore 30 HP right now
  drain    — your next correct answer deals +10 damage and heals you 10 HP
  rewind   — swap the current battle question for a new one
  dodge    — your next wrong answer costs 0 self-damage (still locks you out)
  rage     — your next correct answer deals +15 damage
  bounce   — the next damage you take is halved
  genjutsu — your opponent's next answer counts as wrong
  revive   — if you hit 0 HP, revive at 25 HP once
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quiz import Question

# How many powers a player can hold banked at once.
MAX_BANKED = 3

# HP restored by the 1-Up Mushroom revive.
REVIVE_HP = 25
# Flat bonus damage from Titan Rage.
RAGE_BONUS = 15

POWERS: dict[str, dict[str, str]] = {
    "speed": {
        "name": "Flash Speed",
        "character": "The Flash",
        "flavor": "You answered about the fastest man alive — now you're fast too.",
        "desc": "Your next wrong guess doesn't lock you out — you get a second try in the same round.",
        "effect": "speed",
        "earn": "Answer a question about The Flash (or another speedster)",
    },
    "strength": {
        "name": "Super Saiyan Strength",
        "character": "Goku",
        "flavor": "Goku's power flows through you — your next hit lands twice as hard.",
        "desc": "Your next correct answer deals double damage.",
        "effect": "strength",
        "earn": "Answer a question about Goku / Dragon Ball Z",
    },
    "guard": {
        "name": "Vibranium Guard",
        "character": "Black Panther",
        "flavor": "Wakanda's metal absorbs the blow — the next hit bounces off you.",
        "desc": "Negate the next damage you would take.",
        "effect": "guard",
        "earn": "Answer a question about Black Panther / Wakanda",
    },
    "heal": {
        "name": "Senzu Bean",
        "character": "Dragon Ball",
        "flavor": "A magic bean that puts a fighter back on their feet in seconds.",
        "desc": "Restore 30 HP right now.",
        "effect": "heal",
        "earn": "Answer a question about Dragon Ball (or a healing subject)",
    },
    "drain": {
        "name": "Shinigami Drain",
        "character": "Ryuk",
        "flavor": "The Shinigami's hunger — your next hit steals life from your opponent.",
        "desc": "Your next correct answer deals +10 damage and heals you 10 HP.",
        "effect": "drain",
        "earn": "Answer a question about Death Note / Ryuk",
    },
    "rewind": {
        "name": "DeLorean Rewind",
        "character": "Back to the Future",
        "flavor": "Great Scott! The DeLorean takes you back — new question, fresh start.",
        "desc": "Swap the current battle question for a new one.",
        "effect": "rewind",
        "earn": "Answer a question about Back to the Future",
    },
    "dodge": {
        "name": "Getaway Driver",
        "character": "Franklin Clinton",
        "flavor": "Franklin at the wheel — your next mistake costs you nothing.",
        "desc": "Your next wrong answer costs 0 self-damage.",
        "effect": "dodge",
        "earn": "Answer a question about Franklin / GTA getaways",
    },
    "rage": {
        "name": "Titan Rage",
        "character": "Eren Yeager",
        "flavor": "The Titans' fury — your next hit comes down like a house.",
        "desc": "Your next correct answer deals +15 damage.",
        "effect": "rage",
        "earn": "Answer a question about Attack on Titan",
    },
    "bounce": {
        "name": "Gum-Gum Bounce",
        "character": "Luffy",
        "flavor": "Rubber body, rubber rules — the next hit just bounces off.",
        "desc": "The next damage you take is halved.",
        "effect": "bounce",
        "earn": "Answer a question about Luffy / One Piece",
    },
    "genjutsu": {
        "name": "Genjutsu",
        "character": "Kakashi",
        "flavor": "The Copy Ninja's illusion — your opponent sees a wrong answer.",
        "desc": "Your opponent's next answer counts as wrong.",
        "effect": "genjutsu",
        "earn": "Answer a question about Kakashi / Naruto ninjas",
    },
    "revive": {
        "name": "1-Up Mushroom",
        "character": "Mario",
        "flavor": "An extra life, straight out of the Mushroom Kingdom.",
        "desc": "If you hit 0 HP, revive at 25 HP once.",
        "effect": "revive",
        "earn": "Answer a question about Mario",
    },
}

# Questions without a specific character tag fall back to their category's
# mascot power, so every correct answer banks something.
CATEGORY_FALLBACK_POWER: dict[str, str] = {
    "gta": "dodge",      # every GTA answer is a getaway
    "anime": "strength",  # anime runs on power-ups
    "gaming": "revive",  # games hand out extra lives
    "general": "speed",  # trivia is a race — quick wits
    "movies": "guard",   # blockbusters are built on heroes with shields
    "music": "heal",     # music soothes and restores
}

# Milestone grants — the old unlock-based abilities, folded in: crossing one
# of these milestones banks the matching themed power once. (power_id, stat
# checked, threshold, and which category the mastery check uses.)
#   level 3 -> guard, 5 battle wins -> strength, best streak 5 -> heal,
#   category mastery (10 in one category) -> drain, 25 points -> rewind.
MILESTONE_LEVEL = 3
MILESTONE_WINS = 5
MILESTONE_STREAK = 5
MILESTONE_MASTERY = 10
MILESTONE_POINTS = 25


# Old generic ability names, kept as chat aliases so chatters who learned
# them still land on the right power (shield -> guard, power -> strength,
# reroll -> rewind).
ALIASES: dict[str, str] = {
    "shield": "guard",
    "power": "strength",
    "reroll": "rewind",
}


def power_for_question(question: Question) -> str:
    """The power a correct answer to this question grants.

    A question tagged with a specific ``power`` grants that; anything else
    grants its category's fallback power.
    """
    if question.power and question.power in POWERS:
        return question.power
    return CATEGORY_FALLBACK_POWER.get(question.category, "speed")
