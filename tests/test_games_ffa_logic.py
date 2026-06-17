"""Tests for the extracted Truth-or-Dare card (FFA) pure-logic modules.

Covers ``bot_modules/games_ffa/logic.py`` (anonymous reply append),
``bot_modules/games_ffa/embeds.py`` (card status-bar embed), and
``bot_modules/games_ffa/prompts.py`` (prompt bank + picker). Mirrors
the pressure_cooker / traditional pattern: the cog file stays thin;
this module proves the extracted pieces work without spinning up
Discord.
"""

from __future__ import annotations

from bot_modules.games_ffa.embeds import build_ffa_embed
from bot_modules.games_ffa.logic import add_anon_reply
from bot_modules.games_ffa.prompts import (
    DARE,
    TRUTH,
    label_for_kind,
    pick_prompt,
)


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


def test_build_ffa_embed_title_has_label_and_number():
    embed = build_ffa_embed("TRUTH", 5)
    assert embed.title is not None
    assert "TRUTH" in embed.title
    assert "#5" in embed.title


def test_build_ffa_embed_references_card_attachment():
    from bot_modules.games_ffa.embeds import CARD_FILENAME

    embed = build_ffa_embed("DARE", 1)
    assert embed.image.url == f"attachment://{CARD_FILENAME}"


def test_build_ffa_embed_footer_hides_count_when_zero():
    """Empty game shouldn't say "0 anonymous replies"."""
    embed = build_ffa_embed("TRUTH", 1, reply_count=0)
    assert embed.footer.text is not None
    assert "Truth or Dare" in embed.footer.text
    assert "anonymous replies" not in embed.footer.text


def test_build_ffa_embed_footer_shows_count_when_positive():
    embed = build_ffa_embed("TRUTH", 1, reply_count=5)
    assert embed.footer.text is not None
    assert "5 anonymous replies" in embed.footer.text


def test_build_ffa_embed_footer_singular_or_plural_count_is_literal():
    """The cog uses the same string for 1 reply as for many —
    pin the behaviour so a future change is visible."""
    embed = build_ffa_embed("TRUTH", 1, reply_count=1)
    assert embed.footer.text is not None
    assert "1 anonymous replies" in embed.footer.text


def test_build_ffa_embed_has_brand_color():
    from bot_modules.games.constants import BRAND_COLOR

    embed = build_ffa_embed("TRUTH", 1)
    assert embed.color is not None
    assert embed.color.value == BRAND_COLOR


def test_build_ffa_embed_default_reply_count_is_zero():
    embed = build_ffa_embed("DARE", 2)
    assert embed.footer.text is not None
    assert "anonymous replies" not in embed.footer.text


# ── pick_prompt / label_for_kind ─────────────────────────────────────


def test_pick_prompt_truth_returns_truth_label():
    label, text = pick_prompt("truth", nsfw=False)
    assert label == TRUTH
    assert isinstance(text, str) and text


def test_pick_prompt_dare_returns_dare_label():
    label, text = pick_prompt("dare", nsfw=True)
    assert label == DARE
    assert isinstance(text, str) and text


def test_pick_prompt_random_always_returns_a_valid_label():
    for _ in range(50):
        label, text = pick_prompt("random", nsfw=False)
        assert label in (TRUTH, DARE)
        assert text


def test_pick_prompt_nsfw_pulls_from_nsfw_bank():
    from bot_modules.games_ffa.prompts import TRUTH_NSFW, TRUTH_SFW

    sfw_only = set(TRUTH_SFW) - set(TRUTH_NSFW)
    # Every nsfw truth pick should come from the nsfw bank.
    for _ in range(50):
        _, text = pick_prompt("truth", nsfw=True)
        assert text not in sfw_only


def test_label_for_kind_defaults_to_truth():
    assert label_for_kind("dare") == DARE
    assert label_for_kind("truth") == TRUTH
    assert label_for_kind("random") == TRUTH
