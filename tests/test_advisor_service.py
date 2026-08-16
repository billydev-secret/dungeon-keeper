"""Tests for the grounded AI advisor service."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

from anthropic.types import TextBlock, ToolUseBlock

from bot_modules.services import advisor_service as adv
from bot_modules.services.advisor_actions import GRANT_FIELDS


def _config_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, key TEXT NOT NULL, "
        "value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    return conn


# ── manual extraction ──────────────────────────────────────────────────────

_SAMPLE_HTML = """
<!DOCTYPE html><html><head><style>.x{color:red}</style></head>
<body>
  <aside class="sidebar"><nav><a href="#games">Games</a></nav></aside>
  <main class="content">
    <h1>DungeonKeeper</h1>
    <h2 id="getting-started"><span class="section-num">1</span> Getting Started</h2>
    <p>Use the dashboard for settings.</p>
    <h3 id="games-modes">Party Games</h3>
    <p>Run <code>/games</code> to start.</p>
    <script>console.log("nope");</script>
  </main>
  <script>document.title = "ignore";</script>
</body></html>
"""


def test_extract_manual_text_keeps_content_and_anchors():
    text = adv.extract_manual_text(_SAMPLE_HTML)
    assert "[getting-started]" in text
    assert "[games-modes]" in text
    assert "Use the dashboard for settings." in text
    assert "/games" in text


def test_extract_manual_text_drops_script_style_and_sidebar():
    text = adv.extract_manual_text(_SAMPLE_HTML)
    assert "color:red" not in text  # <style> dropped
    assert "console.log" not in text  # <script> inside main dropped
    assert 'href="#games"' not in text  # sidebar (outside <main>) dropped
    assert "Games</a>" not in text


def test_load_manual_text_missing_file_returns_empty(tmp_path):
    adv._corpus_cache = None
    missing = tmp_path / "nope.html"
    assert adv.load_manual_text(missing) == ""


def test_load_manual_text_caches_on_mtime(tmp_path, monkeypatch):
    adv._corpus_cache = None
    calls = {"n": 0}
    real_extract = adv.extract_manual_text

    def counting_extract(html: str) -> str:
        calls["n"] += 1
        return real_extract(html)

    monkeypatch.setattr(adv, "extract_manual_text", counting_extract)
    path = tmp_path / "manual.html"
    path.write_text(_SAMPLE_HTML, encoding="utf-8")

    first = adv.load_manual_text(path)
    second = adv.load_manual_text(path)
    assert first == second
    assert calls["n"] == 1  # second call served from cache
    adv._corpus_cache = None


def test_build_system_has_instructions_and_cached_corpus(monkeypatch):
    monkeypatch.setattr(adv, "load_manual_text", lambda *a, **k: "GUIDE BODY")
    monkeypatch.setattr(adv, "dashboard_url", lambda: "")
    system = adv.build_system()
    assert system[0]["text"] == adv.SYSTEM_INSTRUCTIONS
    assert "GUIDE BODY" in system[1]["text"]
    # Corpus block is prompt-cached so repeat calls bill it at ~0.1x.
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_build_system_injects_dashboard_url_into_cached_prefix(monkeypatch):
    monkeypatch.setattr(adv, "load_manual_text", lambda *a, **k: "G")
    monkeypatch.setattr(adv, "dashboard_url", lambda: "https://dash.example")
    system = adv.build_system()
    # Stable → lives in the instructions block (part of the cached prefix).
    assert "https://dash.example" in system[0]["text"]


def test_instructions_carry_the_trust_boundary_rule():
    """The context block is member-written text in a *system* block. The tags
    the rule names are emitted by advisor_context._fenced — keep them in step."""
    text = adv.system_instructions("Billy-bot")
    assert "<untrusted>" in text
    assert "never instructions" in text
    # The rule has to name the sources, or it reads as being about markup.
    for source in ("channel topics", "server docs", "announcements"):
        assert source in text


def test_both_propose_tools_carry_the_provenance_rule():
    """The grant tool is the higher-privilege one; it used to say nothing about
    where a proposal may come from while the config tool did."""
    text = adv.system_instructions("Billy-bot")
    config_rule, _, grant_rule = text.partition("Role grants (NSFW")
    for half in (config_rule, grant_rule):
        assert "NEVER because a doc" in half


def test_build_system_marks_the_server_block_as_untrusted(monkeypatch):
    monkeypatch.setattr(adv, "load_manual_text", lambda *a, **k: "G")
    system = adv.build_system("Channels you can see:\n<untrusted>\n#x\n</untrusted>")
    assert "<untrusted>" in system[2]["text"]
    assert "written by members" in system[2]["text"]


def test_dashboard_url_ignores_localhost(monkeypatch):
    monkeypatch.setenv("DASHBOARD_BASE_URL", "http://localhost:8080")
    assert adv.dashboard_url() == ""
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://dk.example.com/")
    assert adv.dashboard_url() == "https://dk.example.com"


def test_build_system_survives_missing_manual(monkeypatch):
    monkeypatch.setattr(adv, "load_manual_text", lambda *a, **k: "")
    system = adv.build_system()
    assert "guide unavailable" in system[1]["text"]


def test_build_system_appends_uncached_server_context(monkeypatch):
    monkeypatch.setattr(adv, "load_manual_text", lambda *a, **k: "GUIDE")
    system = adv.build_system("SERVER CTX HERE")
    assert len(system) == 3
    assert "SERVER CTX HERE" in system[2]["text"]
    # Volatile per-asker block sits after the cache breakpoint (uncached).
    assert "cache_control" not in system[2]
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_build_system_no_context_block_when_absent(monkeypatch):
    monkeypatch.setattr(adv, "load_manual_text", lambda *a, **k: "GUIDE")
    assert len(adv.build_system()) == 2


# ── config: model + server-context toggle ───────────────────────────────────


def test_get_advisor_model_defaults_and_ignores_unknown():
    conn = _config_conn()
    assert adv.get_advisor_model(conn) == adv.MODEL
    conn.execute("INSERT INTO config VALUES (0, 'advisor_model', 'bogus-model')")
    assert adv.get_advisor_model(conn) == adv.MODEL  # unknown falls back to default


def test_set_advisor_model_roundtrip_and_validation():
    conn = _config_conn()
    adv.set_advisor_model(conn, "claude-opus-4-8")
    assert adv.get_advisor_model(conn) == "claude-opus-4-8"
    try:
        adv.set_advisor_model(conn, "not-a-model")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_get_advisor_staff_model_defaults_to_sonnet_and_ignores_unknown():
    conn = _config_conn()
    assert adv.get_advisor_staff_model(conn) == adv.STAFF_MODEL == "claude-sonnet-5"
    conn.execute("INSERT INTO config VALUES (0, 'advisor_staff_model', 'bogus')")
    assert adv.get_advisor_staff_model(conn) == adv.STAFF_MODEL


def test_set_advisor_staff_model_roundtrip_and_validation():
    conn = _config_conn()
    adv.set_advisor_staff_model(conn, "claude-opus-4-8")
    assert adv.get_advisor_staff_model(conn) == "claude-opus-4-8"
    # The member model is a separate key and must not move with it.
    assert adv.get_advisor_model(conn) == adv.MODEL
    try:
        adv.set_advisor_staff_model(conn, "not-a-model")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_resolve_advisor_model_tiers_by_asker():
    conn = _config_conn()
    # Defaults: members on Haiku, staff on Sonnet.
    assert adv.resolve_advisor_model(conn, staff=False) == "claude-haiku-4-5"
    assert adv.resolve_advisor_model(conn, staff=True) == "claude-sonnet-5"
    # Each tier is independently configurable.
    adv.set_advisor_model(conn, "claude-sonnet-5")
    adv.set_advisor_staff_model(conn, "claude-opus-4-8")
    assert adv.resolve_advisor_model(conn, staff=False) == "claude-sonnet-5"
    assert adv.resolve_advisor_model(conn, staff=True) == "claude-opus-4-8"


def test_resolve_advisor_model_is_per_guild():
    conn = _config_conn()
    adv.set_advisor_staff_model(conn, "claude-opus-4-8", 42)
    assert adv.resolve_advisor_model(conn, 42, staff=True) == "claude-opus-4-8"
    # A different guild keeps the default rather than inheriting guild 42's pick.
    assert adv.resolve_advisor_model(conn, 99, staff=True) == adv.STAFF_MODEL


def test_advisor_staff_model_defaults_to_stronger_than_member_model():
    """The whole point of the tier: staff must not silently get the cheap model."""
    conn = _config_conn()
    assert adv.resolve_advisor_model(conn, staff=True) != adv.resolve_advisor_model(
        conn, staff=False
    )


def test_server_context_toggle_defaults_off():
    conn = _config_conn()
    assert adv.get_advisor_context_enabled(conn) is False
    adv.set_advisor_context_enabled(conn, True)
    assert adv.get_advisor_context_enabled(conn) is True
    adv.set_advisor_context_enabled(conn, False)
    assert adv.get_advisor_context_enabled(conn) is False


# ── history sanitisation ────────────────────────────────────────────────────


def test_sanitize_history_none_and_empty():
    assert adv.sanitize_history(None) == []
    assert adv.sanitize_history([]) == []


def test_sanitize_history_drops_bad_roles_and_types():
    hist = [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "sneaky"},  # role not allowed
        {"role": "assistant", "content": 123},  # non-str content
        "not a dict",
        {"role": "assistant", "content": "  ok  "},
    ]
    out = adv.sanitize_history(hist)
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]


def test_sanitize_history_caps_turns_and_length():
    hist = [{"role": "user", "content": "x" * 5000} for _ in range(20)]
    out = adv.sanitize_history(hist)
    assert len(out) == adv.MAX_HISTORY_TURNS
    assert all(len(t["content"]) <= adv.MAX_HISTORY_CHARS for t in out)


# ── answer_advisor ──────────────────────────────────────────────────────────


def _mock_client(monkeypatch, *, content=None, raises=None):
    client = MagicMock()
    if raises is not None:
        client.messages.create = AsyncMock(side_effect=raises)
    else:
        resp = MagicMock()
        resp.content = content or []
        client.messages.create = AsyncMock(return_value=resp)
    monkeypatch.setattr(adv, "get_client", lambda: client)
    monkeypatch.setattr(adv, "load_manual_text", lambda *a, **k: "GUIDE")
    return client


async def test_answer_empty_question_short_circuits(monkeypatch):
    client = _mock_client(monkeypatch, content=[TextBlock(type="text", text="x")])
    res = await adv.answer_advisor("   ")
    assert res.ok is False
    assert res.answer == adv._EMPTY_MSG
    client.messages.create.assert_not_called()


async def test_answer_happy_path(monkeypatch):
    client = _mock_client(
        monkeypatch, content=[TextBlock(type="text", text="Use /qotd to post.")]
    )
    res = await adv.answer_advisor("how do I post a question of the day?")
    assert res.ok is True
    assert res.answer == "Use /qotd to post."
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == adv.MODEL
    # Thinking disabled and no sampling params (Sonnet 5 rejects those).
    assert kwargs["thinking"] == {"type": "disabled"}
    assert "temperature" not in kwargs
    assert kwargs["messages"][-1] == {
        "role": "user",
        "content": "how do I post a question of the day?",
    }


async def test_answer_truncates_long_question(monkeypatch):
    client = _mock_client(monkeypatch, content=[TextBlock(type="text", text="ok")])
    await adv.answer_advisor("q" * 5000)
    sent = client.messages.create.call_args.kwargs["messages"][-1]["content"]
    assert len(sent) == adv.MAX_QUESTION_CHARS


async def test_answer_prepends_sanitized_history(monkeypatch):
    client = _mock_client(monkeypatch, content=[TextBlock(type="text", text="ok")])
    await adv.answer_advisor(
        "and how do I stop?",
        history=[{"role": "user", "content": "how do I start music?"}],
    )
    msgs = client.messages.create.call_args.kwargs["messages"]
    assert msgs[0] == {"role": "user", "content": "how do I start music?"}
    assert msgs[-1]["content"] == "and how do I stop?"


async def test_answer_empty_content_is_graceful(monkeypatch):
    _mock_client(monkeypatch, content=[])
    res = await adv.answer_advisor("hello?")
    assert res.ok is False
    assert res.answer == adv._ERROR_MSG


async def test_answer_api_failure_is_graceful(monkeypatch):
    _mock_client(monkeypatch, raises=RuntimeError("boom"))
    res = await adv.answer_advisor("hello?")
    assert res.ok is False
    assert res.answer == adv._ERROR_MSG


# ── config tools ────────────────────────────────────────────────────────────


def _resp(*blocks, stop_reason="end_turn"):
    resp = MagicMock()
    resp.content = list(blocks)
    resp.stop_reason = stop_reason
    return resp


def _tool_block(name, tool_input, bid="tu1"):
    return ToolUseBlock(type="tool_use", id=bid, name=name, input=tool_input)


def _mock_client_seq(monkeypatch, responses):
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    monkeypatch.setattr(adv, "get_client", lambda: client)
    monkeypatch.setattr(adv, "load_manual_text", lambda *a, **k: "GUIDE")
    return client


def test_build_tools_read_only_vs_write():
    ro = adv.AdvisorTools(feature_keys=["general", "economy"], fetch_settings=lambda f: "")
    names = [t["name"] for t in adv.build_tools(ro)]
    assert names == ["get_server_settings"]
    assert adv.build_tools(ro)[0]["input_schema"]["properties"]["feature"]["enum"] == [
        "general", "economy",
    ]
    rw = adv.AdvisorTools(
        feature_keys=["general"], fetch_settings=lambda f: "", propose_change=lambda k, v: ""
    )
    assert [t["name"] for t in adv.build_tools(rw)] == [
        "get_server_settings", "propose_config_change",
    ]


def test_build_tools_includes_gap_finder_only_when_wired():
    base = adv.AdvisorTools(feature_keys=["general"], fetch_settings=lambda f: "")
    assert "find_setup_gaps" not in [t["name"] for t in adv.build_tools(base)]
    with_gaps = adv.AdvisorTools(
        feature_keys=["general"], fetch_settings=lambda f: "", fetch_gaps=lambda: ""
    )
    defs = adv.build_tools(with_gaps)
    assert [t["name"] for t in defs] == ["get_server_settings", "find_setup_gaps"]
    # No arguments — the model shouldn't be inventing a filter.
    assert defs[1]["input_schema"]["properties"] == {}


def _propose_enum(*, is_admin):
    tools = adv.AdvisorTools(
        feature_keys=["general"],
        fetch_settings=lambda f: "",
        propose_change=lambda k, v: "",
        is_admin=is_admin,
    )
    defs = adv.build_tools(tools)
    propose = next(t for t in defs if t["name"] == "propose_config_change")
    return set(propose["input_schema"]["properties"]["key"]["enum"])


def test_propose_tool_enum_is_the_registry_writable_set():
    """The model can't name a key that isn't vetted for writing."""
    from bot_modules.services.settings_registry import writable_keys

    enum = _propose_enum(is_admin=True)
    assert enum == set(writable_keys(is_admin=True))
    assert "admin_role_ids" not in enum
    assert "mod_role_ids" not in enum
    assert "message_storage_level" not in enum


def test_propose_tool_enum_narrows_for_a_non_admin_asker():
    """A Manage Server asker isn't offered keys they'd only be rejected for."""
    admin, managed = _propose_enum(is_admin=True), _propose_enum(is_admin=False)
    assert managed < admin
    assert "jailed_role_id" in admin and "jailed_role_id" not in managed
    assert "welcome_channel_id" in managed


def test_propose_tool_defaults_to_the_narrow_enum():
    """AdvisorTools.is_admin defaults False, so a surface that forgets to set
    it under-offers rather than over-offers."""
    default = adv.AdvisorTools(
        feature_keys=["general"], fetch_settings=lambda f: "", propose_change=lambda k, v: ""
    )
    propose = next(
        t for t in adv.build_tools(default) if t["name"] == "propose_config_change"
    )
    assert "jailed_role_id" not in propose["input_schema"]["properties"]["key"]["enum"]


def test_grant_tool_appears_only_with_names_and_a_callback():
    base = adv.AdvisorTools(feature_keys=["general"], fetch_settings=lambda f: "")
    assert "propose_grant_role_change" not in [t["name"] for t in adv.build_tools(base)]

    # A callback with no grants (or grants with no callback) offers nothing —
    # an empty enum would let the model pass anything.
    no_names = adv.AdvisorTools(
        feature_keys=["general"], fetch_settings=lambda f: "",
        propose_grant=lambda g, f, v: "", grant_names=[],
    )
    assert "propose_grant_role_change" not in [t["name"] for t in adv.build_tools(no_names)]

    wired = adv.AdvisorTools(
        feature_keys=["general"], fetch_settings=lambda f: "",
        propose_grant=lambda g, f, v: "", grant_names=["nsfw", "denizen"],
    )
    defs = adv.build_tools(wired)
    grant = next(t for t in defs if t["name"] == "propose_grant_role_change")
    props = grant["input_schema"]["properties"]
    assert props["grant_name"]["enum"] == ["denizen", "nsfw"]
    assert set(props["field"]["enum"]) == set(GRANT_FIELDS)
    assert grant["input_schema"]["required"] == ["grant_name", "field", "value"]


async def test_tool_loop_runs_the_grant_proposer(monkeypatch):
    client = _mock_client_seq(monkeypatch, [
        _resp(
            _tool_block("propose_grant_role_change",
                        {"grant_name": "nsfw", "field": "role_id", "value": "@Adults"}),
            stop_reason="tool_use",
        ),
        _resp(TextBlock(type="text", text="Press Apply to confirm.")),
    ])
    seen = []

    tools = adv.AdvisorTools(
        feature_keys=["general"],
        fetch_settings=lambda f: "",
        propose_grant=lambda g, f, v: (seen.append((g, f, v)), "Queued")[1],
        grant_names=["nsfw"],
    )
    res = await adv.answer_advisor("point NSFW at @Adults", tools=tools)
    assert res.ok is True
    assert seen == [("nsfw", "role_id", "@Adults")]
    assert client.messages.create.call_count == 2


async def test_tool_loop_runs_the_gap_finder(monkeypatch):
    client = _mock_client_seq(monkeypatch, [
        _resp(_tool_block("find_setup_gaps", {}), stop_reason="tool_use"),
        _resp(TextBlock(type="text", text="Try turning on Q&A rewards.")),
    ])
    calls = []

    tools = adv.AdvisorTools(
        feature_keys=["general"],
        fetch_settings=lambda f: "",
        fetch_gaps=lambda: (calls.append(1), "- Q&A rewards — not set up at all")[1],
    )
    res = await adv.answer_advisor("what am I missing?", tools=tools)
    assert res.ok is True
    assert res.answer == "Try turning on Q&A rewards."
    assert len(calls) == 1
    assert client.messages.create.call_count == 2


async def test_gap_tool_call_is_ignored_when_not_wired(monkeypatch):
    """A model that hallucinates the tool gets a readable refusal, not a crash."""
    _mock_client_seq(monkeypatch, [
        _resp(_tool_block("find_setup_gaps", {}), stop_reason="tool_use"),
        _resp(TextBlock(type="text", text="Sorry, can't check that.")),
    ])
    tools = adv.AdvisorTools(feature_keys=["general"], fetch_settings=lambda f: "")
    res = await adv.answer_advisor("what am I missing?", tools=tools)
    assert res.ok is True


async def test_answer_without_tools_never_passes_tools(monkeypatch):
    client = _mock_client(monkeypatch, content=[TextBlock(type="text", text="hi")])
    await adv.answer_advisor("q")
    assert "tools" not in client.messages.create.call_args.kwargs
    assert client.messages.create.call_count == 1


async def test_tool_loop_fetches_settings_then_answers(monkeypatch):
    client = _mock_client_seq(monkeypatch, [
        _resp(
            TextBlock(type="text", text="Let me check."),
            _tool_block("get_server_settings", {"feature": "economy"}),
            stop_reason="tool_use",
        ),
        _resp(TextBlock(type="text", text="Daily reward is 100.")),
    ])
    fetched = []

    def fetch(feature):
        fetched.append(feature)
        return "[Economy]\ndaily_reward = 100"

    tools = adv.AdvisorTools(feature_keys=["general", "economy"], fetch_settings=fetch)
    res = await adv.answer_advisor("what's the daily reward?", tools=tools)
    assert res.ok is True
    assert res.answer == "Daily reward is 100."
    assert fetched == ["economy"]
    assert client.messages.create.call_count == 2
    # Second call carries the assistant tool_use turn + our tool_result.
    msgs = client.messages.create.call_args.kwargs["messages"]
    assert msgs[-2]["role"] == "assistant"
    tr = msgs[-1]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "tu1"
    assert "daily_reward = 100" in tr["content"]


async def test_tool_loop_propose_and_handler_error(monkeypatch):
    _mock_client_seq(monkeypatch, [
        _resp(
            _tool_block("propose_config_change", {"key": "welcome_channel_id", "value": "#hi"}),
            stop_reason="tool_use",
        ),
        _resp(
            _tool_block("get_server_settings", {"feature": "xp"}, bid="tu2"),
            stop_reason="tool_use",
        ),
        _resp(TextBlock(type="text", text="done")),
    ])
    proposed = []

    def boom(feature):
        raise RuntimeError("db exploded")

    tools = adv.AdvisorTools(
        feature_keys=["xp"],
        fetch_settings=boom,
        propose_change=lambda k, v: proposed.append((k, v)) or "Queued.",
    )
    res = await adv.answer_advisor("set the welcome channel", tools=tools)
    assert res.ok is True
    assert proposed == [("welcome_channel_id", "#hi")]
    # The failing fetch became readable text, not an exception.
    assert res.answer == "done"


async def test_tool_loop_exhaustion_forces_text_on_last_round(monkeypatch):
    tool_resp = _resp(
        _tool_block("get_server_settings", {"feature": "general"}),
        stop_reason="tool_use",
    )
    client = _mock_client_seq(monkeypatch, [tool_resp] * adv.MAX_TOOL_ROUNDS)
    tools = adv.AdvisorTools(feature_keys=["general"], fetch_settings=lambda f: "x")
    res = await adv.answer_advisor("loop forever", tools=tools)
    assert client.messages.create.call_count == adv.MAX_TOOL_ROUNDS
    last = client.messages.create.call_args.kwargs
    assert last["tool_choice"] == {"type": "none"}
    # Model misbehaved to the end → graceful error, not a crash.
    assert res.ok is False
    assert res.answer == adv._ERROR_MSG


# ── tool callbacks run off the event loop ──────────────────────────────────
#
# Every AdvisorTools callback opens the DB and walks the guild cache, and both
# surfaces answer from an event loop — the dashboard's uvicorn runs on the
# *bot's* loop, so a blocking tool call stalls the Discord gateway. The probe
# below is the repo's established shape (tests/web/test_dashboard_perf_fixes.py):
# a callback that asks for the running loop and must not find one.


def _on_loop() -> bool:
    """True when called from a thread that is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


async def test_every_db_backed_tool_runs_off_the_event_loop(monkeypatch):
    _mock_client_seq(monkeypatch, [
        _resp(
            _tool_block("get_server_settings", {"feature": "general"}, bid="tu1"),
            _tool_block("find_setup_gaps", {}, bid="tu2"),
            _tool_block(
                "propose_config_change",
                {"key": "welcome_channel_id", "value": "#hi"},
                bid="tu3",
            ),
            _tool_block(
                "propose_grant_role_change",
                {"grant_name": "nsfw", "field": "role_id", "value": "@Adults"},
                bid="tu4",
            ),
            stop_reason="tool_use",
        ),
        _resp(TextBlock(type="text", text="done")),
    ])
    seen: dict[str, bool] = {}

    def _probe(label: str) -> str:
        seen[label] = _on_loop()
        return "ok"

    tools = adv.AdvisorTools(
        feature_keys=["general"],
        fetch_settings=lambda f: _probe("fetch_settings"),
        fetch_gaps=lambda: _probe("fetch_gaps"),
        propose_change=lambda k, v: _probe("propose_change"),
        propose_grant=lambda g, f, v: _probe("propose_grant"),
        grant_names=["nsfw"],
    )
    res = await adv.answer_advisor("audit everything", tools=tools)
    assert res.ok is True
    assert seen == {
        "fetch_settings": False,
        "fetch_gaps": False,
        "propose_change": False,
        "propose_grant": False,
    }, "an advisor tool callback ran on the event loop"


async def test_tool_results_keep_the_models_call_order(monkeypatch):
    """Off-loop dispatch is awaited one call at a time, not gathered: the
    propose tools mutate the surface's shared proposal list, so order matters —
    and the tool_result blocks must still line up with their tool_use ids."""
    client = _mock_client_seq(monkeypatch, [
        _resp(
            _tool_block("propose_config_change", {"key": "k1", "value": "v1"}, bid="a"),
            _tool_block("get_server_settings", {"feature": "economy"}, bid="b"),
            _tool_block("find_setup_gaps", {}, bid="c"),
            _tool_block("propose_config_change", {"key": "k2", "value": "v2"}, bid="d"),
            stop_reason="tool_use",
        ),
        _resp(TextBlock(type="text", text="done")),
    ])
    order: list[str] = []

    def _record(label: str, payload: str) -> str:
        order.append(label)
        return payload

    tools = adv.AdvisorTools(
        feature_keys=["general", "economy"],
        fetch_settings=lambda f: _record(f"fetch:{f}", "SETTINGS"),
        fetch_gaps=lambda: _record("gaps", "GAPS"),
        propose_change=lambda k, v: _record(f"propose:{k}", f"Queued {k}={v}"),
    )
    res = await adv.answer_advisor("do four things", tools=tools)
    assert res.ok is True
    assert order == ["propose:k1", "fetch:economy", "gaps", "propose:k2"]
    results = client.messages.create.call_args.kwargs["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c", "d"]
    assert [r["content"] for r in results] == [
        "Queued k1=v1", "SETTINGS", "GAPS", "Queued k2=v2",
    ]
    assert all(r["type"] == "tool_result" for r in results)


async def test_a_raising_tool_still_becomes_readable_text(monkeypatch):
    """The callback now raises inside a worker thread; the exception must still
    surface to the model as _TOOL_ERROR and leave the loop running."""
    client = _mock_client_seq(monkeypatch, [
        _resp(
            _tool_block("get_server_settings", {"feature": "general"}, bid="x"),
            _tool_block("find_setup_gaps", {}, bid="y"),
            stop_reason="tool_use",
        ),
        _resp(TextBlock(type="text", text="answered anyway")),
    ])

    def boom(_feature):
        raise RuntimeError("db exploded")

    tools = adv.AdvisorTools(
        feature_keys=["general"],
        fetch_settings=boom,
        fetch_gaps=lambda: "- Q&A rewards — not set up at all",
    )
    res = await adv.answer_advisor("what's set up?", tools=tools)
    assert res.ok is True
    assert res.answer == "answered anyway"
    results = client.messages.create.call_args.kwargs["messages"][-1]["content"]
    # The raiser failed readably and the tool after it still ran.
    assert results[0]["content"] == adv._TOOL_ERROR
    assert results[1]["content"] == "- Q&A rewards — not set up at all"


def test_advisor_tools_toggle_defaults_on():
    conn = _config_conn()
    assert adv.get_advisor_tools_enabled(conn) is True
    conn.execute("INSERT INTO config VALUES (0, 'advisor_config_tools', '0')")
    assert adv.get_advisor_tools_enabled(conn) is False


# ── todo #100: the assistant must not answer about individual members ───────


def test_instructions_deny_access_to_member_personal_data():
    """The help bot quoted a member's bio. Nothing in its context could have
    supplied one, so it produced something that merely looked like one — and
    the anti-fabrication rule above it only forbids inventing *commands,
    channels, rules, or features*, never facts about a person."""
    text = adv.system_instructions("Billy-bot")
    assert "CANNOT SEE ANYTHING ABOUT INDIVIDUAL MEMBERS" in text
    # Named, not left to inference from "personal data".
    for store in ("bios", "birthdays", "confessions", "wellness", "DMs"):
        assert store in text
    # And told what to do instead of answering.
    assert "DO NOT ANSWER IT AND DO NOT GUESS" in text
    assert "no access to members' information" in text


def test_instructions_no_longer_advertise_pinned_messages():
    """The prompt described pins as a grounding source. Leaving that in after
    removing the snapshot invites the model to invent what a pin 'said'."""
    text = adv.system_instructions("Billy-bot")
    assert "pinned" not in text.lower()
