"""Pure logic for channel word clouds: windows, tokenising, counting.

Nothing here touches Discord, sqlite or Pillow — the whole surface is
``str``/``int``/dataclasses in, dataclasses out, which is what makes the
tokenising rules (the part that actually decides whether a cloud is readable)
testable without a fixture guild.

The rules exist because of what the archive really holds. Measured over seven
days of the home guild's traffic, the top raw tokens were ``white`` (17,357)
and ``square`` (11,861) — a bot's own board art, not anything a member said —
and once bot authors were excluded ``https``/``com``/``gifs``/``klipy`` rose to
take their place. So URL and markup stripping is not tidying, it is the
difference between a portrait of the room and a portrait of the bot.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

#: A live Discord fetch is capped at ten minutes of history, by product
#: decision. Guilds that don't archive message content can only ever be clouded
#: over this window, so a request for "7d" there is clamped and *said* to be —
#: silently returning ten minutes of a week-long ask would be a lie.
LIVE_FETCH_MAX = timedelta(minutes=10)

#: Words shorter than this are dropped before stopwording. Three keeps "lol"
#: and "cat" while losing the two-letter connective tissue.
MIN_WORD_LEN = 3

#: How many words a cloud renders at most. Beyond this the tail is unreadable
#: at any sane image size.
DEFAULT_MAX_WORDS = 150

_UNITS: dict[str, timedelta] = {
    "m": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "mins": timedelta(minutes=1),
    "minute": timedelta(minutes=1),
    "minutes": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "hrs": timedelta(hours=1),
    "hour": timedelta(hours=1),
    "hours": timedelta(hours=1),
    "d": timedelta(days=1),
    "day": timedelta(days=1),
    "days": timedelta(days=1),
}

_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([a-z]+)\s*$", re.IGNORECASE)

#: The archive starts 2023-12-16; anything past a couple of years is a typo
#: rather than a request, and a runaway window is the one way this command can
#: cost real time even with a message cap.
MAX_WINDOW = timedelta(days=730)


class WindowError(ValueError):
    """A window string the member typed that we can't act on."""


def parse_window(text: str) -> timedelta:
    """Parse ``"30m"`` / ``"6 hours"`` / ``"7d"`` into a timedelta.

    Raises :class:`WindowError` with a message written for the member, since
    it goes straight back to them in an ephemeral reply.
    """
    match = _WINDOW_RE.match(text or "")
    if not match:
        raise WindowError(
            "Give a window like `30m`, `6h` or `7d` — a number and a unit."
        )
    amount = int(match.group(1))
    unit = _UNITS.get(match.group(2).lower())
    if unit is None:
        raise WindowError(
            f"`{match.group(2)}` isn't a unit I know. Use minutes, hours or days."
        )
    if amount <= 0:
        raise WindowError("The window has to be more than zero.")
    too_far = "That's further back than I keep — two years is the most."
    try:
        window = unit * amount
    except OverflowError as exc:
        # ``timedelta`` refuses magnitudes past ~999,999,999 days, so a fat
        # finger on the number ("9999999999999d") blows up *before* the
        # MAX_WINDOW check can turn it into a message the member can read.
        raise WindowError(too_far) from exc
    if window > MAX_WINDOW:
        raise WindowError(too_far)
    return window


def clamp_live_window(window: timedelta) -> tuple[timedelta, bool]:
    """Clamp a window to the live-fetch ceiling.

    Returns ``(window, was_clamped)`` so the caller can tell the member their
    ask was shortened rather than quietly serving them less than they asked
    for.
    """
    if window > LIVE_FETCH_MAX:
        return LIVE_FETCH_MAX, True
    return window, False


# --------------------------------------------------------------------------
# Tokenising
# --------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
# <:name:id> and <a:name:id> — custom emoji render as their raw form in stored
# content, so without this every popular emoji becomes a "word".
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
# <@id>, <@!id>, <@&id>, <#id> — ids would tokenise into nothing useful, but
# the surrounding punctuation can glue words together if left in.
_MENTION_RE = re.compile(r"<[@#][!&]?\d+>")
_TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")
# Discord clients substitute a typographic apostrophe, and in the archive
# U+2019 outnumbers the ASCII one 185 to 73 in "don't" alone. Left alone it
# splits every contraction, and the orphaned stem ("don", "that", "it") walks
# straight past the stopword list into the top ten. Fold them all to ASCII
# before tokenising.
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u201b": "'"})

STOPWORDS: frozenset[str] = frozenset(
    """
    the and for are but not you your yours with have has had this that these those
    they them their there then than was were will would could should can cant
    about after all also am an any as at back be because been before being
    below between both by did didnt do does doing dont down during each few
    from further get got had hadnt hasnt havent having here hers herself him
    himself his how into isnt its itself just me more most must my myself no
    nor now of off on once only or other ought our ours ourselves out over own
    same shant she shouldnt so some such thatll thats theirs themselves theres
    theyd theyll theyre theyve through to too under until up very we wed well
    weve what whats when wheres which while who whos whom why whys wont
    wouldnt yeah yep yup ok okay oh ah eh hmm hm um uh like really actually
    thing things stuff even still much many lot lots make makes made going go
    goes gonna wanna kinda sorta let lets see seen say says said know knows
    think thinks want wants need needs one two three sure right left new old
    yes nope nah way ways she her mine ive youre theyre
    """.split()
)

# Contractions survive tokenising as a single token, so they need listing in
# their apostrophe form too — "don't" never becomes "dont" on this path.
STOPWORDS = STOPWORDS | frozenset(
    """
    don't doesn't didn't can't won't wouldn't shouldn't couldn't isn't aren't
    wasn't weren't hasn't haven't hadn't it's that's there's here's what's
    who's let's i'm i've i'll i'd you're you've you'll you'd we're we've we'll
    they're they've they'll he's she's how's where's when's
    """.split()
)


def clean_text(text: str) -> str:
    """Strip everything that isn't a member's own words.

    Order matters: fences before inline code (a fence contains backticks),
    URLs before mentions (a URL can contain ``#``), and both before tokenising
    so their fragments never reach the counter.
    """
    if not text:
        return ""
    text = text.translate(_APOSTROPHES)
    text = _CODE_FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _CUSTOM_EMOJI_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    return text


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with stopwords and short words removed."""
    words = _TOKEN_RE.findall(clean_text(text).lower())
    return [
        w for w in words if len(w) >= MIN_WORD_LEN and w not in STOPWORDS
    ]


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Doc:
    """One message's contribution to a cloud.

    ``sentiment`` is the archive's per-message score in [-1, 1], or None on the
    live-fetch path, which has no scores to read.
    """

    text: str
    sentiment: float | None = None


@dataclass(frozen=True)
class WordStat:
    """A counted word and the mood of the messages it appeared in."""

    word: str
    count: int
    sentiment: float | None


def apply_cap(docs: list[Doc], cap: int) -> tuple[list[Doc], bool]:
    """Keep at most ``cap`` docs, newest first.

    ``docs`` must already be ordered newest-first. Returns
    ``(docs, was_capped)`` — the caller says so on the card, because a capped
    cloud describes a shorter period than the window it is labelled with.
    """
    if cap <= 0 or len(docs) <= cap:
        return docs, False
    return docs[:cap], True


def build_stats(
    docs: list[Doc],
    *,
    min_count: int = 1,
    max_words: int = DEFAULT_MAX_WORDS,
) -> list[WordStat]:
    """Count words across ``docs`` and average each one's message sentiment.

    A word's sentiment is the mean over *occurrences* that carried a score, so
    a word used ten times in one cheerful message weighs as ten — the same way
    it weighs in the count that sizes it. Words appearing only in scoreless
    messages get ``None`` and fall back to the preset palette at render time.
    """
    counts: Counter[str] = Counter()
    sentiment_totals: defaultdict[str, float] = defaultdict(float)
    sentiment_counts: Counter[str] = Counter()

    for doc in docs:
        tokens = tokenize(doc.text)
        counts.update(tokens)
        if doc.sentiment is None:
            continue
        for token in tokens:
            sentiment_totals[token] += doc.sentiment
            sentiment_counts[token] += 1

    stats: list[WordStat] = []
    for word, count in counts.most_common():
        if count < min_count:
            continue
        scored = sentiment_counts.get(word, 0)
        stats.append(
            WordStat(
                word=word,
                count=count,
                sentiment=(sentiment_totals[word] / scored) if scored else None,
            )
        )
        if len(stats) >= max_words:
            break
    return stats
