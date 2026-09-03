"""Repo-wide style rules for embeds and member-facing replies.

``docs/embed_style_guide.md`` is the standard. Two of its rules already have
teeth — accent colour (``test_embed_accent_contract.py``) and member names
(the per-feature ``name_fn`` sweeps) — and everything else drifted quietly for
want of a gate. A 2026-09 review found 77 denial replies missing the ``❌``
the 2026-07-21 ruling requires, nine footers separating with ``·`` instead of
``•``, and 40 multi-section builders never calling ``apply_section_spacing``.
None of that was catchable by review alone at 364 call sites.

Each sweep below owns one rule, names the guide section it comes from, and
carries a "guards the guard" meta-test — the check is only worth having if a
reintroduced violation is actually seen.

Adding a builder? None of this asks anything of you that the guide doesn't
already: prefix a denial with ``❌``, separate footer clauses with ``•``, say
"Pick…" on a select, and call ``apply_section_spacing(embed)`` once after the
fields on a card whose sections stack.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

_SEND_METHODS = {"send_message", "send", "edit_message", "respond"}

#: Replies that open by refusing. The guide's own worked example is
#: "❌ Only the host or a mod can start."
_REFUSAL = re.compile(
    r"^(only\b|you can'?t\b|you cannot\b|i can'?t\b|i cannot\b"
    r"|i don'?t have permission\b|you don'?t have permission\b"
    r"|you need\b|i need\b)",
    re.I,
)

#: Openers that read as a refusal but are really an *empty state* or narration
#: ("No one is in the hot seat.", "Nobody submitted a price this round."). The
#: guide wants those as a plain sentence with no emoji, so they must NOT be
#: swept into the ❌ rule. → guide § Empty states & pagination
_NOT_A_REFUSAL = re.compile(
    r"^(no one\b|nobody\b|there'?s no\b|there is no\b|you are not watching\b)",
    re.I,
)

#: Prefixes that already mark a reply's register.
_MARKERS = ("❌", "✅", "⚠️", "🔒", "⏳", "🎉", "💡", "🚫")


def _py_files():
    for path in sorted(SRC.rglob("*.py")):
        yield path, path.relative_to(SRC).as_posix()


def _leading_text(node: ast.expr) -> str | None:
    """The literal text a string or f-string starts with, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            return ""
    return None


def _content_arg(call: ast.Call) -> ast.expr | None:
    """The message body of a send-shaped call: first positional or content=."""
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == "content":
            return keyword.value
    return None


def _is_interaction_reply(call: ast.Call) -> bool:
    """A reply to an interaction, rather than a broadcast into a channel."""
    receiver = ast.unparse(call.func)
    return any(
        marker in receiver
        for marker in ("interaction.", ".response", ".followup", "itx.")
    )


# ── ❌ on denials (ruling 2026-07-21) ─────────────────────────────────


def unprefixed_denials(tree: ast.AST) -> list[int]:
    """Lines where an interaction reply refuses without the ``❌`` prefix."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _SEND_METHODS:
            continue
        arg = _content_arg(node)
        if arg is None:
            continue
        text = _leading_text(arg)
        if not text:
            continue
        stripped = text.strip()
        if stripped.startswith(_MARKERS) or _NOT_A_REFUSAL.match(stripped):
            continue
        if _REFUSAL.match(stripped) and _is_interaction_reply(node):
            hits.append(arg.lineno)
    return hits


def test_every_denial_reply_opens_with_the_cross():
    """A denial that doesn't look like one reads as an ordinary reply.

    → ``embed_style_guide.md`` § Errors, denials & confirmations
    """
    offenders: list[str] = []
    for path, rel in _py_files():
        source = path.read_text(encoding="utf-8")
        if "send" not in source:  # cheap skip
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        offenders += [f"{rel}:{line}" for line in unprefixed_denials(tree)]

    assert not offenders, (
        "These refuse a member without the ❌ prefix the 2026-07-21 ruling "
        "requires. Say how to fix it too, and name the role where there is "
        "one ('❌ Only the host or a mod can start.'):\n  "
        + "\n  ".join(offenders)
    )


def test_the_denial_sweep_can_actually_see_a_violation():
    """Guards the guard, in both directions."""
    bare = ast.parse(
        'await interaction.response.send_message("Only the host can start.")'
    )
    prefixed = ast.parse(
        'await interaction.response.send_message("❌ Only the host can start.")'
    )
    empty_state = ast.parse(
        'await interaction.response.send_message("No one is in the hot seat.")'
    )
    broadcast = ast.parse('await channel.send("Only one price submitted.")')

    assert unprefixed_denials(bare) == [1]
    assert unprefixed_denials(prefixed) == []
    # An empty state is a plain sentence by design, not a denial.
    assert unprefixed_denials(empty_state) == []
    # A narration into the channel is not refusing anybody.
    assert unprefixed_denials(broadcast) == []


# ── footers separate with • ───────────────────────────────────────────


def middot_footers(tree: ast.AST) -> list[int]:
    """Lines where a footer literal separates clauses with ``·``."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "set_footer":
            continue
        for candidate in list(node.args) + [k.value for k in node.keywords]:
            for piece in ast.walk(candidate):
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    if "·" in piece.value:
                        hits.append(node.lineno)
    return sorted(set(hits))


def test_footers_separate_with_a_bullet():
    """→ ``embed_style_guide.md`` § Card anatomy (Separators)."""
    offenders: list[str] = []
    for path, rel in _py_files():
        source = path.read_text(encoding="utf-8")
        if "set_footer" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        offenders += [f"{rel}:{line}" for line in middot_footers(tree)]

    assert not offenders, (
        "Footers separate clauses with a single-spaced ' • ', not '·':\n  "
        + "\n  ".join(offenders)
    )


def test_the_footer_sweep_can_actually_see_a_violation():
    assert middot_footers(ast.parse('e.set_footer(text="Pin of the Day · 24h")')) == [1]
    assert middot_footers(ast.parse('e.set_footer(text="Pin of the Day • 24h")')) == []
    # An f-string carries its literal pieces in the same place.
    assert middot_footers(ast.parse('e.set_footer(text=f"Page {n} · {ctx}")')) == [1]


# ── selects say "Pick", not "Select" ──────────────────────────────────


def _is_select_call(call: ast.Call) -> bool:
    receiver = ast.unparse(call.func)
    return any(
        marker in receiver
        for marker in (
            "ui.select",
            "ui.Select",
            "UserSelect",
            "RoleSelect",
            "ChannelSelect",
            "MentionableSelect",
        )
    )


def select_says_select(tree: ast.AST) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_select_call(node):
            continue
        for keyword in node.keywords:
            if keyword.arg != "placeholder":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if "select" in value.value.lower():
                    hits.append(value.lineno)
    return hits


def test_select_placeholders_say_pick():
    """→ ``embed_style_guide.md`` § Buttons, modals & selects."""
    offenders: list[str] = []
    for path, rel in _py_files():
        source = path.read_text(encoding="utf-8")
        if "placeholder" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        offenders += [f"{rel}:{line}" for line in select_says_select(tree)]

    assert not offenders, (
        "Select placeholders are imperative and say 'Pick…', not 'Select…':\n  "
        + "\n  ".join(offenders)
    )


def test_the_select_sweep_can_actually_see_a_violation():
    bad = ast.parse('discord.ui.Select(placeholder="Select a user")')
    good = ast.parse('discord.ui.Select(placeholder="Pick a user…")')
    assert select_says_select(bad) == [1]
    assert select_says_select(good) == []


# ── the colour= kwarg ─────────────────────────────────────────────────


def test_embeds_spell_the_colour_kwarg_color():
    """551 ``color=`` against one ``colour=`` when the guide was written; that
    one has since gone. Keep it that way — a split kwarg makes every
    colour-related sweep read both spellings forever."""
    offenders = [
        f"{rel}:{i}"
        for path, rel in _py_files()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "colour=" in line
    ]
    assert not offenders, "Use color=, not colour=:\n  " + "\n  ".join(offenders)


# ── section spacing on stacked cards ──────────────────────────────────


def _add_field_inline_flags(fn: ast.AST, var: str) -> list[object]:
    flags: list[object] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_field":
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != var:
            continue
        inline: object = None
        for keyword in node.keywords:
            if keyword.arg == "inline":
                inline = getattr(keyword.value, "value", "expr")
        flags.append(inline)
    return flags


def unspaced_stacked_builders(tree: ast.AST) -> list[int]:
    """Builders that stack 2+ ``inline=False`` sections without the spacer.

    Scoped deliberately to *pure stacked* cards — the shape
    ``apply_section_spacing`` documents itself for. A card mixing ``inline=True``
    triples is a layout judgement call, not a mechanical one, so it is left to
    a human.
    """
    hits: list[int] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "apply_section_spacing"
            for n in ast.walk(fn)
        ):
            continue
        built = {
            target.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "Embed"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        returned = {
            node.value.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        }
        for var in built & returned:
            flags = _add_field_inline_flags(fn, var)
            if len(flags) < 2:
                continue
            if any(flag is True or flag == "expr" for flag in flags):
                continue  # has inline triples — a human decides
            hits.append(fn.lineno)
    return sorted(set(hits))


def test_stacked_cards_get_their_section_spacing():
    """→ ``embed_style_guide.md`` § Section spacing (breathing room)."""
    offenders: list[str] = []
    for path, rel in _py_files():
        source = path.read_text(encoding="utf-8")
        if "add_field" not in source or "Embed(" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        offenders += [f"{rel}:{line}" for line in unspaced_stacked_builders(tree)]

    assert not offenders, (
        "These stack sections without breathing room. Call "
        "apply_section_spacing(embed) once after the fields "
        "(bot_modules.core.branding — it is idempotent and a no-op under two "
        "fields):\n  " + "\n  ".join(offenders)
    )


def test_the_spacing_sweep_can_actually_see_a_violation():
    stacked = ast.parse(
        "def build():\n"
        "    embed = discord.Embed(title='x')\n"
        "    embed.add_field(name='a', value='1', inline=False)\n"
        "    embed.add_field(name='b', value='2', inline=False)\n"
        "    return embed\n"
    )
    spaced = ast.parse(
        "def build():\n"
        "    embed = discord.Embed(title='x')\n"
        "    embed.add_field(name='a', value='1', inline=False)\n"
        "    embed.add_field(name='b', value='2', inline=False)\n"
        "    apply_section_spacing(embed)\n"
        "    return embed\n"
    )
    triples = ast.parse(
        "def build():\n"
        "    embed = discord.Embed(title='x')\n"
        "    embed.add_field(name='a', value='1', inline=True)\n"
        "    embed.add_field(name='b', value='2', inline=False)\n"
        "    return embed\n"
    )
    single = ast.parse(
        "def build():\n"
        "    embed = discord.Embed(title='x')\n"
        "    embed.add_field(name='a', value='1', inline=False)\n"
        "    return embed\n"
    )

    assert unspaced_stacked_builders(stacked) == [1]
    assert unspaced_stacked_builders(spaced) == []
    # Inline triples are a layout judgement, deliberately out of the sweep.
    assert unspaced_stacked_builders(triples) == []
    # One field has nothing to space against.
    assert unspaced_stacked_builders(single) == []


@pytest.mark.parametrize(
    "name",
    ["apply_section_spacing", "SECTION_SPACER"],
)
def test_the_spacing_helper_still_lives_where_the_sweep_expects(name):
    """Guards the sweep's advice: if the helper moved, every failure message
    above would send the next person to a file that no longer has it."""
    from bot_modules.core import branding

    assert hasattr(branding, name)
