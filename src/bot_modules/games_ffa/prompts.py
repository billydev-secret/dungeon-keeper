"""Curated Truth-or-Dare prompt bank for the FFA card game.

Pure data + a single picker. No Discord imports so it stays unit-testable.

Four banks split on two axes — TRUTH vs DARE and SFW vs NSFW. The NSFW
banks are adult/flirty (the bot already ships nsfw toggles elsewhere) but
kept to consenting adults; nothing involving family/step-family framing.

:func:`pick_prompt` returns ``(label, text)`` where ``label`` is the
uppercase header drawn on the card ("TRUTH" / "DARE").
"""
from __future__ import annotations

import random

TRUTH = "TRUTH"
DARE = "DARE"


TRUTH_SFW: list[str] = [
    "What's the most embarrassing thing you've ever done in front of a crowd?",
    "What's a small lie you tell people all the time?",
    "Who in this server would you trust with your deepest secret?",
    "What's the pettiest reason you've ever stopped talking to someone?",
    "What's the most childish thing you still do as an adult?",
    "What's a compliment you secretly give yourself?",
    "What's the worst gift you've ever received and pretended to like?",
    "What's something everyone seems to love that you just don't get?",
    "What's the most trouble you ever got into as a kid?",
    "What's a habit of yours that would annoy a roommate?",
    "What's the last thing you searched on your phone?",
    "What's the cringiest phase you ever went through?",
    "If you could read one person's mind in this server, whose would it be?",
    "What's a talent you have that almost no one knows about?",
    "What's the longest you've gone without showering?",
]

TRUTH_NSFW: list[str] = [
    "Who was the last person who made you blush, and why?",
    "What's your biggest turn-on that you don't usually admit?",
    "What's the boldest thing you've ever done to get someone's attention?",
    "Describe your ideal first kiss in one sentence.",
    "What's a fantasy you've never told anyone about?",
    "What's the most spontaneous hookup story you're willing to share?",
    "What's something you find unexpectedly attractive in a person?",
    "Who in your DMs right now would you actually go on a date with?",
    "What's the cheesiest pickup line that's actually worked on you?",
    "What's the riskiest place you've ever made out?",
    "What's a secret you'd only tell someone you trust completely?",
    "What's the last thing that gave you butterflies?",
    "What's a relationship 'ick' that instantly ruins it for you?",
    "What's the most flirtatious text you've ever sent?",
]

DARE_SFW: list[str] = [
    "Post the most recent photo in your camera roll (keep it clean!).",
    "Send a voice note singing the chorus of the last song you listened to.",
    "Type your next 3 messages in all caps.",
    "Change your nickname to whatever the person above you suggests for 10 minutes.",
    "Do your best impression of someone in this server and post it as a voice note.",
    "Send the 4th emoji in your recently-used list and explain why it's there.",
    "Write a haiku about the last thing you ate.",
    "Talk in rhymes for your next 2 replies.",
    "Post a screenshot of your home screen.",
    "Compliment three different people in the thread right now.",
    "Send a voice note reading your last text in your most dramatic voice.",
    "Set your status to something the thread picks for the next 10 minutes.",
]

DARE_NSFW: list[str] = [
    "Send a voice note moaning the name of your latest crush.",
    "Describe your flirting style in one spicy sentence.",
    "Send the last flirty text you sent (you can censor the name).",
    "Rate the thread on a scale of 1-10 and say who'd you'd shoot your shot with.",
    "Send a voice note saying something you'd whisper to someone you like.",
    "Confess the most scandalous thought you've had today.",
    "Describe your 'type' in explicit-but-tasteful detail.",
    "Send your boldest pickup line as a voice note.",
    "Tell the thread your green flag that makes you irresistible.",
    "Describe the last dream you had that you'd be embarrassed to share.",
]


def _bank(label: str, nsfw: bool) -> list[str]:
    if label == TRUTH:
        return TRUTH_NSFW if nsfw else TRUTH_SFW
    return DARE_NSFW if nsfw else DARE_SFW


def pick_prompt(kind: str = "random", nsfw: bool = False) -> tuple[str, str]:
    """Return ``(label, text)`` for the requested *kind*.

    *kind* is ``"truth"``, ``"dare"``, or ``"random"`` (50/50). *nsfw*
    selects the spicier bank. The label is the uppercase header drawn on
    the card.
    """
    if kind == "truth":
        label = TRUTH
    elif kind == "dare":
        label = DARE
    else:
        label = random.choice((TRUTH, DARE))
    return label, random.choice(_bank(label, nsfw))


def label_for_kind(kind: str) -> str:
    """Header label for a custom (host-typed) prompt; defaults to TRUTH."""
    return DARE if kind == "dare" else TRUTH
