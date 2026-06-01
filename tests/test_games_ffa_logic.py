"""Tests for the extracted Free For All pure-logic modules.

Covers ``bot_modules/games_ffa/logic.py`` (anonymous reply append) and
``bot_modules/games_ffa/embeds.py`` (FFA status-bar embed). Mirrors
the pressure_cooker / traditional pattern: the cog file stays thin;
this module proves the extracted pieces work without spinning up
Discord.
"""

from __future__ import annotations

from bot_modules.games_ffa.embeds import build_ffa_embed
from bot_modules.games_ffa.logic import add_anon_reply


# ── add_anon_reply ───────────────────────────────────────────────────


def test_add_anon_reply_creates_anon_replies_on_empty_payload():
    payload: dict = {}
    count = add_anon_reply(payload, user_id=42, text="my answer")
    assert count == 1
    assert payload["anon_replies"] == {"1": {"user_id": 42, "text": "my answer"}}


def test_add_anon_reply_returns_running_count():
    """Each call appends and the returned count is the new total —
    the cog uses this to update the embed footer."""
    payload: dict = {}
    assert add_anon_reply(payload, 1, "first") == 1
    assert add_anon_reply(payload, 2, "second") == 2
    assert add_anon_reply(payload, 3, "third") == 3


def test_add_anon_reply_uses_sequential_string_ids():
    """Ids are stringified ints so JSON-serialised payloads round-trip."""
    payload: dict = {}
    add_anon_reply(payload, 1, "a")
    add_anon_reply(payload, 2, "b")
    assert list(payload["anon_replies"].keys()) == ["1", "2"]


def test_add_anon_reply_records_user_id_alongside_text():
    """The submitter's id is stashed for the audit log even though the
    public post is anonymous."""
    payload: dict = {}
    add_anon_reply(payload, user_id=99, text="hello")
    entry = payload["anon_replies"]["1"]
    assert entry["user_id"] == 99
    assert entry["text"] == "hello"


def test_add_anon_reply_preserves_existing_entries():
    payload = {"anon_replies": {"1": {"user_id": 7, "text": "old"}}}
    count = add_anon_reply(payload, 8, "new")
    assert count == 2
    assert payload["anon_replies"]["1"] == {"user_id": 7, "text": "old"}
    assert payload["anon_replies"]["2"] == {"user_id": 8, "text": "new"}


def test_add_anon_reply_allows_same_user_to_post_multiple_times():
    payload: dict = {}
    add_anon_reply(payload, 42, "first thought")
    add_anon_reply(payload, 42, "second thought")
    assert len(payload["anon_replies"]) == 2
    assert payload["anon_replies"]["1"]["text"] == "first thought"
    assert payload["anon_replies"]["2"]["text"] == "second thought"


def test_add_anon_reply_keeps_other_payload_keys_untouched():
    payload = {"question": "What's your favourite colour?", "other": [1, 2]}
    add_anon_reply(payload, 1, "blue")
    assert payload["question"] == "What's your favourite colour?"
    assert payload["other"] == [1, 2]


# ── build_ffa_embed ──────────────────────────────────────────────────


def test_build_ffa_embed_has_title_and_question_field():
    embed = build_ffa_embed("What's your favourite colour?")
    assert embed.title is not None
    assert "FREE FOR ALL" in embed.title

    by_name = {f.name: f.value or "" for f in embed.fields}
    assert "Question" in by_name
    assert "What's your favourite colour?" in by_name["Question"]


def test_build_ffa_embed_renders_question_as_h1():
    embed = build_ffa_embed("hello")
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert by_name["Question"].startswith("# ")


def test_build_ffa_embed_escapes_markdown_in_question():
    """A question containing markdown shouldn't break rendering."""
    embed = build_ffa_embed("**not bold** _nor italic_")
    by_name = {f.name: f.value or "" for f in embed.fields}
    # The asterisks/underscores should be escaped with backslashes
    assert "\\*\\*not bold\\*\\*" in by_name["Question"]
    assert "\\_nor italic\\_" in by_name["Question"]


def test_build_ffa_embed_footer_hides_count_when_zero():
    """Empty game shouldn't say "0 anonymous replies"."""
    embed = build_ffa_embed("Q?", reply_count=0)
    assert embed.footer.text is not None
    assert "Free For All" in embed.footer.text
    assert "anonymous replies" not in embed.footer.text


def test_build_ffa_embed_footer_shows_count_when_positive():
    embed = build_ffa_embed("Q?", reply_count=5)
    assert embed.footer.text is not None
    assert "5 anonymous replies" in embed.footer.text


def test_build_ffa_embed_footer_singular_or_plural_count_is_literal():
    """The current cog uses the same string for 1 reply as for many —
    pin the behaviour so a future change is visible."""
    embed = build_ffa_embed("Q?", reply_count=1)
    assert embed.footer.text is not None
    assert "1 anonymous replies" in embed.footer.text


def test_build_ffa_embed_has_golden_meadow_color():
    from bot_modules.games.constants import BRAND_COLOR

    embed = build_ffa_embed("Q?")
    assert embed.color is not None
    assert embed.color.value == BRAND_COLOR


def test_build_ffa_embed_default_reply_count_is_zero():
    embed = build_ffa_embed("Q?")
    assert embed.footer.text is not None
    assert "anonymous replies" not in embed.footer.text
