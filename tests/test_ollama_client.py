"""Backend selection and the remote-endpoint privacy gate for ollama_client.

The guard model sees raw conversation windows, so the rule these tests pin
down is: content only goes to an endpoint we can prove is on the local
network, unless an operator overrides it on purpose.
"""

from __future__ import annotations

import json

import pytest

from bot_modules.services import ollama_client


# ── is_private_endpoint ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://127.1.2.3:8080",
        "http://[::1]:8080",
        "http://192.168.1.20:8080",
        "http://10.0.0.5:8080",
        "http://172.16.4.4:8080",
        "http://169.254.10.10:8080",  # link-local
        "http://[fd00::1]:8080",      # unique-local v6
        "http://gpubox.local:8080",
        "http://gpubox.lan:8080",
        "http://inference.internal:8080",
    ],
)
def test_private_endpoints_accepted(url):
    assert ollama_client.is_private_endpoint(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com",
        "https://api.anthropic.com/v1",
        "http://8.8.8.8:8080",
        "http://[2606:4700::1111]:8080",
        "http://gpubox.example.com:8080",
        "http://inference-server:8080",  # bare name — unresolvable, so refused
        "",
        "not-a-url",
    ],
)
def test_public_or_unclassifiable_endpoints_rejected(url):
    assert ollama_client.is_private_endpoint(url) is False


def test_private_check_is_case_insensitive():
    assert ollama_client.is_private_endpoint("http://GPUBox.LOCAL:8080") is True


# ── get_server_url ─────────────────────────────────────────────────────────────


def test_no_url_means_in_process(monkeypatch):
    monkeypatch.delenv("LLAMA_SERVER_URL", raising=False)
    assert ollama_client.get_server_url() == ""


def test_private_url_is_used_and_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://192.168.1.20:8080/")
    assert ollama_client.get_server_url() == "http://192.168.1.20:8080"


def test_public_url_refused_without_override(monkeypatch, caplog):
    monkeypatch.setenv("LLAMA_SERVER_URL", "https://api.anthropic.com")
    monkeypatch.delenv("LLAMA_SERVER_ALLOW_PUBLIC", raising=False)

    with caplog.at_level("ERROR"):
        assert ollama_client.get_server_url() == ""

    assert "refusing to send" in caplog.text


@pytest.mark.parametrize("flag", ["1", "true", "yes"])
def test_public_url_allowed_with_explicit_override(monkeypatch, flag):
    monkeypatch.setenv("LLAMA_SERVER_URL", "https://api.anthropic.com")
    monkeypatch.setenv("LLAMA_SERVER_ALLOW_PUBLIC", flag)
    assert ollama_client.get_server_url() == "https://api.anthropic.com"


def test_override_ignores_unrecognised_values(monkeypatch):
    """A typo'd override must fail closed, not open."""
    monkeypatch.setenv("LLAMA_SERVER_URL", "https://api.anthropic.com")
    monkeypatch.setenv("LLAMA_SERVER_ALLOW_PUBLIC", "maybe")
    assert ollama_client.get_server_url() == ""


# ── backend selection ──────────────────────────────────────────────────────────


def test_is_available_true_for_remote_without_any_local_model(monkeypatch):
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://192.168.1.20:8080")
    monkeypatch.setenv("LLAMA_MODEL_PATH", "")
    monkeypatch.setenv("LLAMA_HF_REPO", "")
    monkeypatch.setenv("LLAMA_HF_FILE", "")
    assert ollama_client.is_available() is True


def test_status_reports_backend(monkeypatch):
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://192.168.1.20:8080")
    assert ollama_client.status()["backend"] == "remote"

    monkeypatch.delenv("LLAMA_SERVER_URL", raising=False)
    assert ollama_client.status()["backend"] == "in-process"


def test_default_model_names_the_remote_host(monkeypatch):
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://192.168.1.20:8080")
    assert ollama_client.default_model() == "llama-server (192.168.1.20:8080)"


@pytest.mark.asyncio
async def test_start_loading_marks_ready_without_loading_a_model(monkeypatch):
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://192.168.1.20:8080")
    monkeypatch.setattr(ollama_client, "_phase", "idle")
    monkeypatch.setattr(ollama_client, "_ready_event", None)

    ollama_client.start_loading()

    assert ollama_client._phase == "ready"
    assert ollama_client._model is None
    assert ollama_client._ready_event is not None
    assert ollama_client._ready_event.is_set()


# ── remote chat ────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in that records the request."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        _FakeClient.captured["timeout"] = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _FakeClient.captured["url"] = url
        _FakeClient.captured["json"] = json
        return _FakeResponse(
            {
                "choices": [{"message": {"content": "  {\"verdict\": \"ok\"}  "}}],
                "usage": {"completion_tokens": 7},
            }
        )


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx

    _FakeClient.captured = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return _FakeClient


@pytest.mark.asyncio
async def test_chat_routes_to_remote_and_strips_whitespace(monkeypatch, fake_httpx):
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://192.168.1.20:8080")
    monkeypatch.setattr(ollama_client, "_phase", "ready")

    import asyncio
    event = asyncio.Event()
    event.set()
    monkeypatch.setattr(ollama_client, "_ready_event", event)

    out = await ollama_client.chat(system="SYS", user_content="USER", max_tokens=256, temperature=0.0)

    assert out == '{"verdict": "ok"}'
    assert fake_httpx.captured["url"] == "http://192.168.1.20:8080/v1/chat/completions"

    sent = fake_httpx.captured["json"]
    assert sent["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert sent["max_tokens"] == 256
    assert sent["temperature"] == 0.0


@pytest.mark.asyncio
async def test_caller_model_arg_is_never_forwarded(monkeypatch, fake_httpx):
    """A hosted model ID left in the DB must not reach the wire."""
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://192.168.1.20:8080")
    monkeypatch.setattr(ollama_client, "_phase", "ready")

    import asyncio
    event = asyncio.Event()
    event.set()
    monkeypatch.setattr(ollama_client, "_ready_event", event)

    await ollama_client.chat(
        model="claude-sonnet-4-6", system="SYS", user_content="USER"
    )

    assert fake_httpx.captured["json"]["model"] == "local"
    assert "claude" not in json.dumps(fake_httpx.captured["json"])


@pytest.mark.asyncio
async def test_remote_timeout_is_configurable(monkeypatch, fake_httpx):
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://192.168.1.20:8080")
    monkeypatch.setenv("LLAMA_SERVER_TIMEOUT", "45")
    monkeypatch.setattr(ollama_client, "_phase", "ready")

    import asyncio
    event = asyncio.Event()
    event.set()
    monkeypatch.setattr(ollama_client, "_ready_event", event)

    await ollama_client.chat(system="SYS", user_content="USER")

    assert fake_httpx.captured["timeout"] == 45.0
