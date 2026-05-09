# Bot Identity Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Bot Identity (this server)" section to the Global Config panel that lets admins change the bot's per-guild nickname and guild member avatar (via URL or file upload).

**Architecture:** A new `POST /api/config/bot-identity` endpoint accepts multipart form data and calls `guild.me.edit()` on the Discord bot. The GET `/api/config` response gains a `bot_identity` section so the panel can pre-fill the current nickname and render an avatar preview. The frontend section is appended to the existing `config-global.js` panel with its own Apply button, independent of the main config form. User-supplied strings are HTML-escaped before insertion.

**Tech Stack:** FastAPI (`UploadFile`, `Form`, `File`), discord.py `Member.edit()`, `httpx` (async HTTP client, already in requirements.txt), vanilla JS (existing panel pattern).

---

## File Map

- **Modify:** `web/routes/config.py` - add `_bot_identity_section()` helper, add `bot_identity` key to GET `/api/config`, add `POST /api/config/bot-identity` endpoint
- **Modify:** `web/static/js/panels/config-global.js` - append Bot Identity section after existing form
- **Modify:** `tests/test_web_routes.py` - tests for new GET section and POST endpoint

---

### Task 1: GET /api/config - bot_identity section (backend)

**Files:**
- Modify: `web/routes/config.py`
- Test: `tests/test_web_routes.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_routes.py`:

```python
def test_get_config_includes_bot_identity_with_bot(ctx, make_client):
    from types import SimpleNamespace
    guild = SimpleNamespace(
        me=SimpleNamespace(
            nick="DungeonBot",
            guild_avatar=None,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/1/abc.png"),
        )
    )
    ctx.bot = SimpleNamespace(get_guild=lambda gid: guild)
    client = make_client()
    resp = client.get("/api/config")
    assert resp.status_code == 200
    bi = resp.json()["bot_identity"]
    assert bi["nick"] == "DungeonBot"
    assert bi["avatar_url"] == "https://cdn.discordapp.com/avatars/1/abc.png"

def test_get_config_bot_identity_falls_back_when_no_bot(ctx, make_client):
    ctx.bot = None
    client = make_client()
    resp = client.get("/api/config")
    assert resp.status_code == 200
    bi = resp.json()["bot_identity"]
    assert bi["nick"] == ""
    assert bi["avatar_url"] == ""

def test_get_config_bot_identity_empty_nick_when_no_guild_nick(ctx, make_client):
    from types import SimpleNamespace
    guild = SimpleNamespace(
        me=SimpleNamespace(
            nick=None,
            guild_avatar=None,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/1/abc.png"),
        )
    )
    ctx.bot = SimpleNamespace(get_guild=lambda gid: guild)
    client = make_client()
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["bot_identity"]["nick"] == ""
```

- [ ] **Step 2: Run tests to confirm they fail**

```
pytest tests/test_web_routes.py::test_get_config_includes_bot_identity_with_bot tests/test_web_routes.py::test_get_config_bot_identity_falls_back_when_no_bot tests/test_web_routes.py::test_get_config_bot_identity_empty_nick_when_no_guild_nick -v
```

Expected: FAIL - `KeyError: 'bot_identity'`

- [ ] **Step 3: Add the helper and section to config.py**

In `web/routes/config.py`, add this helper after `_birthday_section` (around line 222):

```python
def _bot_identity_section(guild) -> dict:
    if guild is None:
        return {"nick": "", "avatar_url": ""}
    return {
        "nick": guild.me.nick or "",
        "avatar_url": str(guild.me.display_avatar.url),
    }
```

In the `get_config` route's `_q()` function, add `bot_identity` to the returned dict after the `"birthday"` key. The variable `prune_guild` is already computed earlier in `_q()` as `bot.get_guild(guild_id) if bot is not None else None`:

```python
                "birthday": _birthday_section(conn, guild_id),
                "bot_identity": _bot_identity_section(prune_guild),
```

- [ ] **Step 4: Run tests to confirm they pass**

```
pytest tests/test_web_routes.py::test_get_config_includes_bot_identity_with_bot tests/test_web_routes.py::test_get_config_bot_identity_falls_back_when_no_bot tests/test_web_routes.py::test_get_config_bot_identity_empty_nick_when_no_guild_nick -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```
git add web/routes/config.py tests/test_web_routes.py
git commit -m "feat(config): expose bot_identity section in GET /api/config"
```

---

### Task 2: POST /api/config/bot-identity endpoint (backend)

**Files:**
- Modify: `web/routes/config.py`
- Test: `tests/test_web_routes.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_routes.py`:

```python
def test_post_bot_identity_updates_nick(ctx, make_client):
    from types import SimpleNamespace
    edit_calls = []
    async def mock_edit(**kwargs): edit_calls.append(kwargs)
    guild = SimpleNamespace(
        me=SimpleNamespace(
            nick="OldName",
            guild_avatar=None,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/1/abc.png"),
            edit=mock_edit,
        )
    )
    ctx.bot = SimpleNamespace(get_guild=lambda gid: guild)
    client = make_client()
    resp = client.post("/api/config/bot-identity", data={"nick": "NewName"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert edit_calls == [{"nick": "NewName"}]

def test_post_bot_identity_fetches_avatar_url(ctx, make_client):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    edit_calls = []
    async def mock_edit(**kwargs): edit_calls.append(kwargs)
    guild = SimpleNamespace(
        me=SimpleNamespace(
            nick=None, guild_avatar=None,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/1/abc.png"),
            edit=mock_edit,
        )
    )
    ctx.bot = SimpleNamespace(get_guild=lambda gid: guild)
    client = make_client()
    fake_image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    mock_response = SimpleNamespace(
        status_code=200, content=fake_image, raise_for_status=lambda: None,
    )
    async def mock_get(url, **kwargs): return mock_response
    with patch("web.routes.config.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = mock_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance
        resp = client.post("/api/config/bot-identity", data={"avatar_url": "https://example.com/image.png"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert edit_calls[0]["avatar"] == fake_image

def test_post_bot_identity_file_takes_priority_over_url(ctx, make_client):
    from io import BytesIO
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    edit_calls = []
    async def mock_edit(**kwargs): edit_calls.append(kwargs)
    guild = SimpleNamespace(
        me=SimpleNamespace(
            nick=None, guild_avatar=None,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/1/abc.png"),
            edit=mock_edit,
        )
    )
    ctx.bot = SimpleNamespace(get_guild=lambda gid: guild)
    client = make_client()
    file_bytes = b"\x89PNG\r\n\x1a\nFILE"
    with patch("web.routes.config.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock()
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = client.post(
            "/api/config/bot-identity",
            data={"avatar_url": "https://example.com/image.png"},
            files={"avatar_file": ("avatar.png", BytesIO(file_bytes), "image/png")},
        )
    assert resp.status_code == 200
    MockClient.return_value.__aenter__.assert_not_called()
    assert edit_calls[0]["avatar"] == file_bytes

def test_post_bot_identity_503_when_no_bot(ctx, make_client):
    ctx.bot = None
    client = make_client()
    resp = client.post("/api/config/bot-identity", data={"nick": "NewName"})
    assert resp.status_code == 503

def test_post_bot_identity_400_on_bad_avatar_url(ctx, make_client):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    import httpx as _httpx
    async def mock_edit(**kwargs): pass
    guild = SimpleNamespace(
        me=SimpleNamespace(
            nick=None, guild_avatar=None,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/1/abc.png"),
            edit=mock_edit,
        )
    )
    ctx.bot = SimpleNamespace(get_guild=lambda gid: guild)
    client = make_client()
    with patch("web.routes.config.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=_httpx.RequestError("connection failed"))
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance
        resp = client.post("/api/config/bot-identity", data={"avatar_url": "https://bad.example.com/img.png"})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to confirm they fail**

```
pytest tests/test_web_routes.py::test_post_bot_identity_updates_nick tests/test_web_routes.py::test_post_bot_identity_fetches_avatar_url tests/test_web_routes.py::test_post_bot_identity_file_takes_priority_over_url tests/test_web_routes.py::test_post_bot_identity_503_when_no_bot tests/test_web_routes.py::test_post_bot_identity_400_on_bad_avatar_url -v
```

Expected: FAIL - 404 (route not found)

- [ ] **Step 3: Add imports and endpoint to config.py**

At the top of `web/routes/config.py`, add `httpx` after the stdlib imports:

```python
import httpx
```

Extend the existing `from fastapi import ...` line to include file upload types:

```python
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
```

Add the endpoint at the end of the file, after `update_birthday`:

```python
# ---- Bot identity (per-guild) ---


@router.post("/config/bot-identity")
async def update_bot_identity(
    request: Request,
    nick: str | None = Form(default=None),
    avatar_url: str | None = Form(default=None),
    avatar_file: UploadFile | None = File(default=None),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(503, "Bot not available")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(503, "Discord guild not available")

    # Resolve avatar bytes: file takes priority over URL
    avatar_bytes: bytes | None = None
    if avatar_file is not None:
        content = await avatar_file.read()
        if content:
            avatar_bytes = content
    elif avatar_url:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(avatar_url, follow_redirects=True, timeout=10.0)
                response.raise_for_status()
                avatar_bytes = response.content
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise HTTPException(400, f"Failed to fetch avatar URL: {exc}")

    edit_kwargs: dict = {}
    if nick is not None:
        edit_kwargs["nick"] = nick
    if avatar_bytes is not None:
        edit_kwargs["avatar"] = avatar_bytes

    if edit_kwargs:
        try:
            await guild.me.edit(**edit_kwargs)
        except Exception as exc:
            raise HTTPException(400, f"Discord rejected the update: {exc}")

    return {
        "ok": True,
        "nick": guild.me.nick or "",
        "avatar_url": str(guild.me.display_avatar.url),
    }
```

- [ ] **Step 4: Run tests to confirm they pass**

```
pytest tests/test_web_routes.py::test_post_bot_identity_updates_nick tests/test_web_routes.py::test_post_bot_identity_fetches_avatar_url tests/test_web_routes.py::test_post_bot_identity_file_takes_priority_over_url tests/test_web_routes.py::test_post_bot_identity_503_when_no_bot tests/test_web_routes.py::test_post_bot_identity_400_on_bad_avatar_url -v
```

Expected: PASS

- [ ] **Step 5: Run the full web routes suite to check for regressions**

```
pytest tests/test_web_routes.py -v
```

Expected: all previously-passing tests still PASS

- [ ] **Step 6: Commit**

```
git add web/routes/config.py tests/test_web_routes.py
git commit -m "feat(config): add POST /api/config/bot-identity for per-guild nick and avatar"
```

---

### Task 3: Frontend - Bot Identity section in config-global.js

**Files:**
- Modify: `web/static/js/panels/config-global.js`

No automated tests. Manual verification steps are listed at the end.

- [ ] **Step 1: Replace the contents of config-global.js**

User-supplied values (nick, avatar_url) are HTML-escaped via a small `_esc` helper before
being placed into the template string. Replace the entire file with:

```javascript
import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelectMulti, apiPut, showStatus } from "../config-helpers.js";

function _esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config...</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const g = config.global;
    const bi = config.bot_identity || { nick: "", avatar_url: "" };

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Global Config</h2>
          <div class="subtitle">Timezone, mod channel, bypass roles, and recorded bots</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Timezone Offset (hours from UTC)</label>
            <input type="number" step="0.5" name="tz_offset_hours" value="${_esc(g.tz_offset_hours)}" />
            <div class="field-hint">e.g. -5 for EST, 1 for CET</div>
          </div>
          <div class="field">
            <label>Mod Channel</label>
            <select name="mod_channel_id">${channelSelect(channels, g.mod_channel_id)}</select>
          </div>
          <div class="field">
            <label>Bypass Roles</label>
            <select name="bypass_role_ids" multiple size="6">${roleSelectMulti(roles, g.bypass_role_ids)}</select>
            <div class="field-hint">Roles that bypass spoiler guard and other restrictions (Ctrl/Cmd-click to select multiple)</div>
          </div>
          <div class="field">
            <label>Recorded Bot User IDs</label>
            <input type="text" name="recorded_bot_user_ids" value="${_esc((g.recorded_bot_user_ids || []).join(", "))}" />
            <div class="field-hint">Bot accounts whose messages should be stored (e.g. Risky Roller). Comma-separated user IDs. These bots still don't earn XP or trigger wellness/moderation.</div>
          </div>
          <div class="field">
            <label>Booster Swatch Directory</label>
            <input type="text" name="booster_swatch_dir" value="${_esc(g.booster_swatch_dir || "")}" />
            <div class="field-hint">Folder with booster color swatch images</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>

        <section class="form" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--border,#333)">
          <h3 style="margin:0 0 1rem">Bot Identity <span style="font-weight:400;font-size:.85em;opacity:.6">(this server)</span></h3>
          ${bi.avatar_url ? `<img data-avatar-preview src="${_esc(bi.avatar_url)}" alt="Bot avatar" style="width:64px;height:64px;border-radius:50%;object-fit:cover;margin-bottom:1rem;display:block" />` : ""}
          <div class="field">
            <label>Nickname</label>
            <input type="text" data-nick value="${_esc(bi.nick)}" placeholder="Leave blank to clear nickname" />
          </div>
          <div class="field">
            <label>Avatar URL</label>
            <input type="url" data-avatar-url placeholder="https://example.com/image.png" />
            <div class="field-hint">Paste an image URL, or upload a file below (file takes priority if both are set)</div>
          </div>
          <div class="field">
            <label>Upload Avatar</label>
            <input type="file" data-avatar-file accept="image/*" />
          </div>
          <div><button type="button" class="btn btn-primary" data-identity-apply>Apply</button><span data-identity-status></span></div>
        </section>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/global", {
          tz_offset_hours: parseFloat(fd.get("tz_offset_hours")) || 0,
          mod_channel_id: fd.get("mod_channel_id"),
          bypass_role_ids: Array.from(form.querySelector('select[name="bypass_role_ids"]').selectedOptions).map((o) => o.value),
          recorded_bot_user_ids: fd.get("recorded_bot_user_ids").split(",").map((s) => s.trim()).filter(Boolean),
          booster_swatch_dir: fd.get("booster_swatch_dir"),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    const applyBtn = container.querySelector("[data-identity-apply]");
    const identityStatus = container.querySelector("[data-identity-status]");
    const avatarPreview = container.querySelector("[data-avatar-preview]");

    applyBtn.addEventListener("click", async () => {
      const nickInput = container.querySelector("[data-nick]");
      const avatarUrlInput = container.querySelector("[data-avatar-url]");
      const avatarFileInput = container.querySelector("[data-avatar-file]");

      const fd = new FormData();
      fd.append("nick", nickInput.value);
      if (avatarFileInput.files.length > 0) {
        fd.append("avatar_file", avatarFileInput.files[0]);
      } else if (avatarUrlInput.value.trim()) {
        fd.append("avatar_url", avatarUrlInput.value.trim());
      }

      try {
        const res = await fetch("/api/config/bot-identity", {
          method: "POST",
          credentials: "same-origin",
          body: fd,
        });
        if (!res.ok) {
          let detail = res.statusText;
          try { const b = await res.json(); if (b.detail) detail = b.detail; } catch (_) {}
          throw new Error(`${res.status}: ${detail}`);
        }
        const data = await res.json();
        if (avatarPreview && data.avatar_url) avatarPreview.src = data.avatar_url;
        nickInput.value = data.nick || "";
        avatarUrlInput.value = "";
        avatarFileInput.value = "";
        showStatus(identityStatus, true, "Applied");
      } catch (err) {
        showStatus(identityStatus, false, err.message);
      }
    });
  })();
}
```

- [ ] **Step 2: Manually verify in the browser**

Start the dev server and navigate to the Global Config panel. Confirm:

1. The "Bot Identity (this server)" section renders below the main form separated by a horizontal rule.
2. The avatar preview shows the current bot avatar (or is absent when bot is offline).
3. The Nickname field is pre-filled with the bot's current guild nickname (empty if none set).
4. Entering a new nickname and clicking Apply POSTs to `/api/config/bot-identity`, shows "Applied", and updates the nick input to the server-returned value.
5. Pasting a valid image URL and clicking Apply sends it as `avatar_url`.
6. Selecting an image file and clicking Apply sends it as `avatar_file`; the URL field is ignored even if filled.
7. The avatar preview `src` updates after a successful apply.
8. A bad URL surfaces a readable error message in the status span.
9. The main Save button still works independently.

- [ ] **Step 3: Commit**

```
git add web/static/js/panels/config-global.js
git commit -m "feat(config-global): add Bot Identity section for per-guild nick and avatar"
```

---

## Final check

```
pytest tests/ -x -q
```

Expected: all tests pass.