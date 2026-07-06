"""Tests for the docs system: pure markdown rendering + db round-trips."""

from __future__ import annotations

import sqlite3

from bot_modules.docs import db as docs_db
from bot_modules.docs.render import (
    EMBED_DESC_LIMIT,
    EMBED_TITLE_LIMIT,
    render_doc,
)
from migrations import apply_migrations_sync


# ── render ──────────────────────────────────────────────────────────

def test_doc_title_leads_as_big_header():
    # No heading in the source → doc title leads as a ``#`` header (renders big).
    specs = render_doc("Server Rules", "Be kind to each other.")
    assert len(specs) == 1
    assert specs[0].description == "# Server Rules\n\nBe kind to each other."


def test_leading_heading_stays_inline_and_wins_over_doc_title():
    specs = render_doc("Ignored Title", "# Real Heading\n\nBody text here.")
    assert len(specs) == 1
    # Author's own heading is kept as-is; doc title is NOT injected.
    assert specs[0].description == "# Real Heading\n\nBody text here."
    assert "Ignored Title" not in specs[0].description


def test_hr_splits_into_separate_messages():
    body = "# Rules\n\nRule one.\n\n---\n\n## FAQ\n\nAn answer."
    specs = render_doc("", body)
    assert len(specs) == 2
    assert specs[0].description.startswith("# Rules")
    assert "Rule one." in specs[0].description
    assert specs[1].description.startswith("## FAQ")
    assert "An answer." in specs[1].description


def test_doc_title_only_leads_first_embed():
    # Title header attaches to the first section only, not later ones.
    specs = render_doc("Doc", "First part.\n\n---\n\nSecond part.")
    assert specs[0].description == "# Doc\n\nFirst part."
    assert specs[1].description == "Second part."


def test_star_and_underscore_rules_also_split():
    assert len(render_doc("", "a\n\n***\n\nb")) == 2
    assert len(render_doc("", "a\n\n___\n\nb")) == 2


def test_masked_links_pass_through_unchanged():
    specs = render_doc("", "See [the guide](https://example.com/guide) please.")
    assert "[the guide](https://example.com/guide)" in specs[0].description


def test_overflow_splits_on_paragraph_boundaries():
    para = "x" * 2000
    body = "\n\n".join([para, para, para])  # ~6000 chars, 3 paragraphs
    specs = render_doc("Doc", body)
    assert len(specs) >= 2
    for s in specs:
        assert len(s.description) <= EMBED_DESC_LIMIT
    # The doc title leads the first embed as a big header.
    assert specs[0].description.startswith("# Doc")


def test_overflow_never_breaks_a_code_fence():
    filler = "\n\n".join("line " + str(i) for i in range(200))
    code = "```python\n" + "\n".join(f"a = {i}" for i in range(60)) + "\n```"
    specs = render_doc("", f"{filler}\n\n{code}")
    joined = [s for s in specs if "```python" in s.description]
    assert len(joined) == 1
    # The fence opens and closes within the same embed.
    frag = joined[0].description
    assert frag.count("```") == 2


def test_empty_doc_yields_placeholder_embed():
    specs = render_doc("", "")
    assert len(specs) == 1
    assert specs[0].description  # non-empty so the embed is valid


def test_long_title_is_truncated():
    specs = render_doc("T" * 500, "body")
    header_line = specs[0].description.split("\n")[0]
    # "# " + up to EMBED_TITLE_LIMIT chars of title.
    assert header_line.startswith("# ")
    assert len(header_line) <= EMBED_TITLE_LIMIT + 2


def test_heading_only_section_stays_inline():
    specs = render_doc("", "## Just A Heading")
    assert specs[0].description.strip() == "## Just A Heading"


# ── db round-trips ──────────────────────────────────────────────────

def _conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "docs.db"
    apply_migrations_sync(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_create_get_list_doc(tmp_path):
    conn = _conn(tmp_path)
    docs_db.create_doc(conn, 1, "rules", "Rules", "# Rules", "", 42, 100.0)
    doc = docs_db.get_doc(conn, 1, "rules")
    assert doc is not None
    assert doc["title"] == "Rules"
    assert doc["updated_by"] == 42
    assert [d["doc_key"] for d in docs_db.list_docs(conn, 1)] == ["rules"]
    # Other guilds don't see it.
    assert docs_db.get_doc(conn, 2, "rules") is None


def test_placement_message_id_reconcile(tmp_path):
    conn = _conn(tmp_path)
    doc_id = docs_db.create_doc(conn, 1, "faq", "FAQ", "body", "", 0, 1.0)
    pid = docs_db.upsert_placement(conn, doc_id, 555, 1.0)
    # upsert is idempotent on (doc, channel).
    assert docs_db.upsert_placement(conn, doc_id, 555, 2.0) == pid

    docs_db.set_placement_message_ids(conn, pid, [10, 11, 12], 2.0)
    assert docs_db.get_placement_message_ids(conn, pid) == [10, 11, 12]
    # Shrinking replaces the whole ordered list.
    docs_db.set_placement_message_ids(conn, pid, [10], 3.0)
    assert docs_db.get_placement_message_ids(conn, pid) == [10]

    assert docs_db.list_placements(conn, doc_id)[0]["message_count"] == 1


def test_delete_doc_cascades(tmp_path):
    conn = _conn(tmp_path)
    doc_id = docs_db.create_doc(conn, 1, "x", "X", "b", "", 0, 1.0)
    pid = docs_db.upsert_placement(conn, doc_id, 9, 1.0)
    docs_db.set_placement_message_ids(conn, pid, [1, 2], 1.0)
    docs_db.delete_doc(conn, doc_id)
    assert docs_db.get_doc(conn, 1, "x") is None
    assert docs_db.list_placements(conn, doc_id) == []
    assert (
        conn.execute("SELECT COUNT(*) FROM doc_placement_messages").fetchone()[0] == 0
    )


def test_slugify_key():
    assert docs_db.slugify_key("Mod FAQ!") == "mod-faq"
    assert docs_db.slugify_key("  Rules  ") == "rules"
    assert docs_db.slugify_key("!!!") == ""


# ── sync reconcile (_sync_channel) ──────────────────────────────────
#
# The heart of "stays in sync everywhere". The invariant every case checks:
# the channel's visible top→bottom order must equal the stored message_ids,
# because future syncs edit by stored index.

import discord  # noqa: E402

from bot_modules.docs import sync as docs_sync  # noqa: E402


def _not_found() -> discord.NotFound:
    class _R:
        status = 404
        reason = "Not Found"

    return discord.NotFound(_R(), "missing")  # type: ignore[arg-type]


class _FakeMessage:
    def __init__(self, channel: "_FakeChannel", mid: int) -> None:
        self._channel = channel
        self.id = mid

    async def edit(self, *, embed=None) -> None:
        self._channel.log.append(("edit", self.id))

    async def delete(self) -> None:
        self._channel.order.remove(self.id)
        self._channel.present.discard(self.id)
        self._channel.log.append(("delete", self.id))


class _FakeChannel:
    """Records send/edit/delete and tracks visible top→bottom order."""

    def __init__(self, present_ids: list[int]) -> None:
        self.present = set(present_ids)
        self.order = list(present_ids)
        self._next = 1000
        self.log: list[tuple[str, int]] = []

    async def fetch_message(self, mid: int) -> _FakeMessage:
        if mid not in self.present:
            raise _not_found()
        return _FakeMessage(self, mid)

    async def send(self, *, embed=None) -> _FakeMessage:
        self._next += 1
        mid = self._next
        self.present.add(mid)
        self.order.append(mid)  # send always appends at the bottom
        self.log.append(("send", mid))
        return _FakeMessage(self, mid)


async def _run_sync(monkeypatch, present_ids, n_embeds, existing_ids):
    channel = _FakeChannel(present_ids)

    async def _fake_resolve(bot, channel_id):
        return channel

    monkeypatch.setattr(docs_sync, "_resolve_channel", _fake_resolve)
    embeds = [discord.Embed(description=str(i)) for i in range(n_embeds)]
    result = await docs_sync._sync_channel(None, 42, embeds, existing_ids)
    return channel, result


async def test_sync_no_change_edits_in_place(monkeypatch):
    channel, result = await _run_sync(monkeypatch, [10, 11], 2, [10, 11])
    assert result.message_ids == [10, 11] == channel.order
    assert result.edited == 2 and result.created == 0 and result.deleted == 0


async def test_sync_grow_appends(monkeypatch):
    channel, result = await _run_sync(monkeypatch, [10], 3, [10])
    assert result.message_ids == channel.order
    assert result.message_ids[0] == 10
    assert result.edited == 1 and result.created == 2 and result.deleted == 0


async def test_sync_shrink_deletes_tail(monkeypatch):
    channel, result = await _run_sync(monkeypatch, [10, 11, 12], 1, [10, 11, 12])
    assert result.message_ids == [10] == channel.order
    assert result.edited == 1 and result.deleted == 2


async def test_sync_middle_deletion_preserves_order(monkeypatch):
    # 11 was manually deleted in Discord; channel holds [10, 12].
    channel, result = await _run_sync(monkeypatch, [10, 12], 3, [10, 11, 12])
    # THE regression guard: stored order must match what's visible in-channel.
    assert result.message_ids == channel.order
    assert result.message_ids[0] == 10  # unbroken prefix edited in place
    assert result.edited == 1 and result.created == 2 and result.deleted == 1


async def test_sync_all_deleted_reposts_in_order(monkeypatch):
    channel, result = await _run_sync(monkeypatch, [], 3, [10, 11, 12])
    assert result.message_ids == channel.order
    assert result.created == 3 and result.edited == 0
    assert 10 not in channel.present  # stale ids gone


# ── image extraction ────────────────────────────────────────────────

def test_image_extracted_and_stripped_from_text():
    specs = render_doc("Doc", "Intro line.\n\n![banner](https://cdn/x.png)\n\nMore text.")
    assert len(specs) == 1
    assert specs[0].image_url == "https://cdn/x.png"
    # The ![]() markdown must not survive as literal text.
    assert "![" not in specs[0].description
    assert "Intro line." in specs[0].description
    assert "More text." in specs[0].description


def test_image_only_section_survives():
    specs = render_doc("", "![pic](https://cdn/only.png)")
    assert len(specs) == 1
    assert specs[0].image_url == "https://cdn/only.png"


def test_image_only_doc_is_not_treated_as_empty():
    specs = render_doc("Banner", "![b](https://cdn/b.png)")
    assert len(specs) == 1
    assert specs[0].image_url == "https://cdn/b.png"
    assert "empty" not in specs[0].description.lower()


def test_masked_link_survives_image_extraction():
    body = "See [the guide](https://example.com) ![pic](https://cdn/p.png)"
    specs = render_doc("", body)
    assert specs[0].image_url == "https://cdn/p.png"
    assert "[the guide](https://example.com)" in specs[0].description
    assert "![pic]" not in specs[0].description


def test_image_as_own_section_is_a_separate_embed():
    specs = render_doc("", "![banner](https://cdn/top.png)\n\n---\n\n# Rules\n\nBe nice.")
    assert len(specs) == 2
    assert specs[0].image_url == "https://cdn/top.png"
    assert specs[1].description.startswith("# Rules")
    assert specs[1].image_url is None


def test_two_images_in_section_first_wins():
    specs = render_doc("", "![a](https://cdn/a.png) ![b](https://cdn/b.png)")
    assert specs[0].image_url == "https://cdn/a.png"


def test_image_only_on_first_embed_of_overflow_section():
    big = "y" * 5000  # forces a 2nd continuation embed
    specs = render_doc("", f"![pic](https://cdn/p.png)\n\n{big}")
    assert len(specs) >= 2
    assert specs[0].image_url == "https://cdn/p.png"
    assert all(s.image_url is None for s in specs[1:])


def test_specs_to_embeds_sets_image():
    specs = render_doc("Doc", "![p](https://cdn/p.png)\n\nhi")
    embeds = docs_sync.specs_to_embeds(specs, discord.Colour(0x123456))
    assert embeds[0].image.url == "https://cdn/p.png"
