"""Tests for the grounded AI advisor service."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

from anthropic.types import TextBlock

from bot_modules.services import advisor_service as adv


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
    system = adv.build_system()
    assert system[0]["text"] == adv.SYSTEM_INSTRUCTIONS
    assert "GUIDE BODY" in system[1]["text"]
    # Corpus block is prompt-cached so repeat calls bill it at ~0.1x.
    assert system[1]["cache_control"] == {"type": "ephemeral"}


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
