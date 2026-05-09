# Veil Config Web Panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a web dashboard config panel so all Veil NSFW-game settings can be managed from the Config section, instead of only through the `/veil setup` Discord command.

**Architecture:** Add a `"veil"` block to the existing `GET /api/config` response and a new `PUT /api/config/veil` endpoint in `web/routes/config.py` (same pattern as confessions, spoiler, starboard). A new JS panel `config-veil.js` reads `config.veil` from `loadConfig()` and submits to the PUT endpoint. One line added to `app.js` registers it in the Config sidebar.

**Tech Stack:** Python / FastAPI / Pydantic (backend), vanilla ES-module JS with DOM manipulation (frontend), SQLite via `services/veil_repo.py`

---

## Files

| File | Action |
|------|--------|
| `web/routes/config.py` | Add `_veil_section()` helper + `"veil"` key in GET; add `VeilConfigUpdate` model + `PUT /config/veil` endpoint |
| `tests/web/test_config_routes.py` | Add tests for veil GET section + PUT endpoint |
| `web/static/js/panels/config-veil.js` | Create: single flat form panel using DOM manipulation |
| `web/static/js/app.js` | Register panel in Config section |

---

## Task 1: Backend GET — veil section in `/api/config`

**Files:**
- Modify: `web/routes/config.py`
- Test: `tests/web/test_config_routes.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/web/test_config_routes.py`:

```python
def test_get_config_includes_veil_section(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    v = resp.json()["veil"]
    assert v["channel_id"] == "0"
    assert v["role_id"] == "0"
    assert v["crop_difficulty"] == "medium"
    assert v["guess_cooldown_seconds"] == 30
    assert v["min_image_dimension_px"] == 400
    assert v["max_image_size_mb"] == 10
    assert v["reuse_enabled"] is True
    assert v["reuse_quiet_hours"] == 24
    assert v["reuse_min_age_days"] == 30
    assert v["reuse_min_post_interval_hours"] == 48
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/web/test_config_routes.py::test_get_config_includes_veil_section -v
```

Expected: `FAILED` — `KeyError: 'veil'`

- [ ] **Step 3: Add import to `web/routes/config.py`**

At the top of `web/routes/config.py`, alongside the other service imports (near the `from services.confessions_service import ...` block), add:

```python
from services.veil_repo import get_veil_config as _get_veil_config
```

- [ ] **Step 4: Add `_veil_section` helper to `web/routes/config.py`**

Add this function near the other `_*_section` helpers (before the `@router.get("/config")` route, alongside `_birthday_section`, `_starboard_section`, etc.):

```python
def _veil_section(conn, guild_id: int) -> dict:
    vc = _get_veil_config(conn, guild_id)
    return {
        "channel_id": str(vc.veil_channel_id),
        "role_id": str(vc.veil_role_id),
        "crop_difficulty": vc.crop_difficulty,
        "guess_cooldown_seconds": vc.guess_cooldown_seconds,
        "min_image_dimension_px": vc.min_image_dimension_px,
        "max_image_size_mb": vc.max_image_size_mb,
        "reuse_enabled": vc.reuse_enabled,
        "reuse_quiet_hours": vc.reuse_quiet_hours,
        "reuse_min_age_days": vc.reuse_min_age_days,
        "reuse_min_post_interval_hours": vc.reuse_min_post_interval_hours,
    }
```

- [ ] **Step 5: Wire `_veil_section` into the GET /api/config response**

Inside the `_q()` closure of `get_config`, the return dict ends with:

```python
                "confessions": _confessions_section(guild_id, bot, conn),
                "dms": _dms_section_with_conn(conn, guild_id),
                "starboard": _starboard_section(conn, guild_id),
                "birthday": _birthday_section(conn, guild_id),
```

Add `"veil"` after `"birthday"`:

```python
                "confessions": _confessions_section(guild_id, bot, conn),
                "dms": _dms_section_with_conn(conn, guild_id),
                "starboard": _starboard_section(conn, guild_id),
                "birthday": _birthday_section(conn, guild_id),
                "veil": _veil_section(conn, guild_id),
```

- [ ] **Step 6: Run test to verify it passes**

```
pytest tests/web/test_config_routes.py::test_get_config_includes_veil_section -v
```

Expected: `PASSED`

- [ ] **Step 7: Commit**

```bash
git add web/routes/config.py tests/web/test_config_routes.py
git commit -m "feat(veil): expose veil config in GET /api/config"
```

---

## Task 2: Backend PUT — `/api/config/veil` endpoint

**Files:**
- Modify: `web/routes/config.py`
- Test: `tests/web/test_config_routes.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/web/test_config_routes.py`:

```python
# -- PUT /api/config/veil ------------------------------------------------------


def test_update_veil_channel(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/veil", json={"channel_id": "555"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        val = get_config_value(conn, "veil_channel_id", "0", fake_ctx.guild_id)
    assert val == "555"


def test_update_veil_crop_difficulty_hard(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/veil", json={"crop_difficulty": "hard"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        val = get_config_value(conn, "veil_crop_difficulty", "medium", fake_ctx.guild_id)
    assert val == "hard"


def test_update_veil_invalid_difficulty_returns_error(authed_client):
    resp = authed_client.put("/api/config/veil", json={"crop_difficulty": "insane"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "crop_difficulty" in data["detail"]


def test_update_veil_reuse_disabled(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/veil", json={"reuse_enabled": False})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        val = get_config_value(conn, "veil_reuse_enabled", "1", fake_ctx.guild_id)
    assert val == "0"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/web/test_config_routes.py::test_update_veil_channel tests/web/test_config_routes.py::test_update_veil_crop_difficulty_hard tests/web/test_config_routes.py::test_update_veil_invalid_difficulty_returns_error tests/web/test_config_routes.py::test_update_veil_reuse_disabled -v
```

Expected: all `FAILED` — 404 or similar (endpoint doesn't exist yet)

- [ ] **Step 3: Add `VeilConfigUpdate` model and `PUT /config/veil` to `web/routes/config.py`**

Add at the end of `web/routes/config.py` (after the last `@router.put` block):

```python
_VEIL_VALID_DIFFICULTIES = {"easy", "medium", "hard"}


class VeilConfigUpdate(BaseModel):
    channel_id: str | None = None
    role_id: str | None = None
    crop_difficulty: str | None = None
    guess_cooldown_seconds: int | None = None
    min_image_dimension_px: int | None = None
    max_image_size_mb: int | None = None
    reuse_enabled: bool | None = None
    reuse_quiet_hours: int | None = None
    reuse_min_age_days: int | None = None
    reuse_min_post_interval_hours: int | None = None


@router.put("/config/veil")
async def update_veil_config(
    request: Request,
    body: VeilConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    _require_primary_guild(request)
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        from services.veil_repo import set_veil_config_value
        if body.crop_difficulty is not None and body.crop_difficulty not in _VEIL_VALID_DIFFICULTIES:
            return {"ok": False, "detail": f"crop_difficulty must be one of {sorted(_VEIL_VALID_DIFFICULTIES)}"}
        with ctx.open_db() as conn:
            if body.channel_id is not None:
                set_veil_config_value(conn, guild_id, "veil_channel_id", body.channel_id)
            if body.role_id is not None:
                set_veil_config_value(conn, guild_id, "veil_role_id", body.role_id)
            if body.crop_difficulty is not None:
                set_veil_config_value(conn, guild_id, "veil_crop_difficulty", body.crop_difficulty)
            if body.guess_cooldown_seconds is not None:
                set_veil_config_value(conn, guild_id, "veil_guess_cooldown_seconds", str(body.guess_cooldown_seconds))
            if body.min_image_dimension_px is not None:
                set_veil_config_value(conn, guild_id, "veil_min_image_dimension_px", str(body.min_image_dimension_px))
            if body.max_image_size_mb is not None:
                set_veil_config_value(conn, guild_id, "veil_max_image_size_mb", str(body.max_image_size_mb))
            if body.reuse_enabled is not None:
                set_veil_config_value(conn, guild_id, "veil_reuse_enabled", "1" if body.reuse_enabled else "0")
            if body.reuse_quiet_hours is not None:
                set_veil_config_value(conn, guild_id, "veil_reuse_quiet_hours", str(body.reuse_quiet_hours))
            if body.reuse_min_age_days is not None:
                set_veil_config_value(conn, guild_id, "veil_reuse_min_age_days", str(body.reuse_min_age_days))
            if body.reuse_min_post_interval_hours is not None:
                set_veil_config_value(conn, guild_id, "veil_reuse_min_post_interval_hours", str(body.reuse_min_post_interval_hours))
        return {"ok": True}

    return await run_query(_q)
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/web/test_config_routes.py::test_update_veil_channel tests/web/test_config_routes.py::test_update_veil_crop_difficulty_hard tests/web/test_config_routes.py::test_update_veil_invalid_difficulty_returns_error tests/web/test_config_routes.py::test_update_veil_reuse_disabled -v
```

Expected: all `PASSED`

- [ ] **Step 5: Run full web test suite to check for regressions**

```
pytest tests/web/test_config_routes.py -v
```

Expected: all passing

- [ ] **Step 6: Commit**

```bash
git add web/routes/config.py tests/web/test_config_routes.py
git commit -m "feat(veil): add PUT /api/config/veil endpoint"
```

---

## Task 3: Frontend — `config-veil.js` panel + register in `app.js`

**Files:**
- Create: `web/static/js/panels/config-veil.js`
- Modify: `web/static/js/app.js`

No automated tests for JS panels in this codebase. Verify manually after the server is running (see Step 3).

- [ ] **Step 1: Create `web/static/js/panels/config-veil.js`**

Uses DOM manipulation throughout (no dynamic value interpolation into markup strings). Helpers `mkSel`, `mkOpt`, `mkNum`, `mkField` mirror the structure used by `config-confessions.js`.

```javascript
import { loadConfig, loadChannels, loadRoles, apiPut, showStatus } from "../config-helpers.js";

function mkSel(name) {
  const s = document.createElement("select");
  s.name = name;
  return s;
}

function mkOpt(value, text, selected) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = text;
  o.selected = !!selected;
  return o;
}

function mkNum(name, min, value) {
  const i = document.createElement("input");
  i.type = "number";
  i.name = name;
  i.min = String(min);
  i.value = String(value);
  return i;
}

function mkField(labelText, ctrl, hint) {
  const d = document.createElement("div");
  d.className = "field";
  const l = document.createElement("label");
  l.textContent = labelText;
  d.appendChild(l);
  d.appendChild(ctrl);
  if (hint) {
    const h = document.createElement("div");
    h.className = "field-hint";
    h.textContent = hint;
    d.appendChild(h);
  }
  return d;
}

export function mount(container) {
  container.textContent = "";
  const wrap = document.createElement("div");
  wrap.className = "panel";
  const loading = document.createElement("div");
  loading.className = "empty";
  loading.textContent = "Loading config…";
  wrap.appendChild(loading);
  container.appendChild(wrap);

  (async () => {
    const [config, channels, roles] = await Promise.all([
      loadConfig(),
      loadChannels(),
      loadRoles(),
    ]);
    const v = config.veil;

    container.textContent = "";
    const panel = document.createElement("div");
    panel.className = "panel";

    const hdr = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Veil";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "NSFW guessing game settings";
    hdr.append(h2, sub);
    panel.appendChild(hdr);

    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    // Game Channel
    const chSel = mkSel("channel_id");
    chSel.appendChild(mkOpt("0", "(disabled)", v.channel_id === "0" || !v.channel_id));
    for (const ch of channels) {
      chSel.appendChild(mkOpt(ch.id, "#" + ch.name, ch.id === v.channel_id));
    }
    form.appendChild(mkField("Game Channel", chSel, "Channel where rounds are posted. Required for the game to work."));

    // Required Role
    const roleSel = mkSel("role_id");
    roleSel.appendChild(mkOpt("0", "(none)", v.role_id === "0" || !v.role_id));
    for (const r of roles) {
      roleSel.appendChild(mkOpt(r.id, "@" + r.name, r.id === v.role_id));
    }
    form.appendChild(mkField("Required Role", roleSel, "Role required to submit images. \"(none)\" allows everyone."));

    // Crop Difficulty
    const diffSel = mkSel("crop_difficulty");
    for (const d of ["easy", "medium", "hard"]) {
      diffSel.appendChild(mkOpt(d, d.charAt(0).toUpperCase() + d.slice(1), d === v.crop_difficulty));
    }
    form.appendChild(mkField("Crop Difficulty", diffSel, "How tightly the crop frames the detected region."));

    form.appendChild(mkField(
      "Guess Cooldown (seconds)",
      mkNum("guess_cooldown_seconds", 0, v.guess_cooldown_seconds),
      "Per-user cooldown between guesses.",
    ));
    form.appendChild(mkField("Min Image Dimension (px)", mkNum("min_image_dimension_px", 1, v.min_image_dimension_px)));
    form.appendChild(mkField("Max Image Size (MB)", mkNum("max_image_size_mb", 1, v.max_image_size_mb)));

    const reuseLbl = document.createElement("div");
    reuseLbl.className = "section-label";
    reuseLbl.textContent = "Reuse Settings";
    form.appendChild(reuseLbl);

    const reuseSel = mkSel("reuse_enabled");
    reuseSel.append(mkOpt("true", "Yes", v.reuse_enabled), mkOpt("false", "No", !v.reuse_enabled));
    form.appendChild(mkField("Reuse Enabled", reuseSel, "Allow the bot to recycle old crops during quiet stretches."));
    form.appendChild(mkField(
      "Reuse Quiet Hours",
      mkNum("reuse_quiet_hours", 0, v.reuse_quiet_hours),
      "Hours of inactivity before a reuse round can be posted.",
    ));
    form.appendChild(mkField(
      "Reuse Min Age (days)",
      mkNum("reuse_min_age_days", 0, v.reuse_min_age_days),
      "Original round must be at least this many days old to be reused.",
    ));
    form.appendChild(mkField(
      "Reuse Min Post Interval (hours)",
      mkNum("reuse_min_post_interval_hours", 0, v.reuse_min_post_interval_hours),
      "Minimum hours between consecutive reuse posts.",
    ));

    const row = document.createElement("div");
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save";
    const statusEl = document.createElement("span");
    row.append(saveBtn, statusEl);
    form.appendChild(row);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/veil", {
          channel_id: fd.get("channel_id"),
          role_id: fd.get("role_id"),
          crop_difficulty: fd.get("crop_difficulty"),
          guess_cooldown_seconds: parseInt(fd.get("guess_cooldown_seconds")) || 0,
          min_image_dimension_px: parseInt(fd.get("min_image_dimension_px")) || 1,
          max_image_size_mb: parseInt(fd.get("max_image_size_mb")) || 1,
          reuse_enabled: fd.get("reuse_enabled") === "true",
          reuse_quiet_hours: parseInt(fd.get("reuse_quiet_hours")) || 0,
          reuse_min_age_days: parseInt(fd.get("reuse_min_age_days")) || 0,
          reuse_min_post_interval_hours: parseInt(fd.get("reuse_min_post_interval_hours")) || 0,
        });
        showStatus(statusEl, true);
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    container.appendChild(panel);
  })();
}
```

- [ ] **Step 2: Register the panel in `web/static/js/app.js`**

In `app.js`, find the Config section (search for `id: "config-ai"`). Add a "Veil" entry immediately after it:

Before:
```javascript
      { id: "config-ai",          label: "AI Commands",      module: "./panels/config-ai.js" },
```

After:
```javascript
      { id: "config-ai",          label: "AI Commands",      module: "./panels/config-ai.js" },
      { id: "config-veil",        label: "Veil",             module: "./panels/config-veil.js" },
```

- [ ] **Step 3: Run backend tests to confirm nothing regressed**

```
pytest tests/web/ -v
```

Expected: all passing

- [ ] **Step 4: Commit**

```bash
git add web/static/js/panels/config-veil.js web/static/js/app.js
git commit -m "feat(veil): add Veil config panel to web dashboard"
```
