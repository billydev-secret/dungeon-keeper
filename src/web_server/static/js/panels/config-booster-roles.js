import { loadConfig, loadRoles, loadChannels, channelSelect, roleSelect, apiPut, apiDelete, showStatus } from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

function _esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, roles, channels] = await Promise.all([
      loadConfig(),
      loadRoles(),
      loadChannels(),
    ]);
    render(
      container,
      config.booster_roles || [],
      config.booster_panel_channel_id || "0",
      roles,
      channels,
    );
  })();
}

function render(container, boosterRoles, panelChannelId, roles, channels) {
  function roleCard(r) {
    return `
      <form class="form card" style="margin-bottom:16px;" data-key="${r.role_key}">
        <div class="section-label">${r.role_key}</div>
        <div class="field">
          <label>Label</label>
          <input type="text" name="label" value="${r.label}" />
        </div>
        <div class="field">
          <label>Role</label>
          <select name="role_id">${roleSelect(roles, r.role_id)}</select>
        </div>
        <div class="field">
          <label>Image Path</label>
          <input type="text" name="image_path" value="${r.image_path || ""}" />
        </div>
        <div class="field">
          <label>Sort Order</label>
          <input type="number" name="sort_order" value="${r.sort_order}" min="0" />
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Save</button>
          <button type="button" class="btn btn-danger" data-remove="${r.role_key}">Remove</button>
          <span data-status></span>
        </div>
      </form>`;
  }

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Booster Roles</h2>
        <div class="subtitle">Custom color roles for server boosters.</div>
      </header>
      <div data-roles>${boosterRoles.length ? boosterRoles.map(roleCard).join("") : '<div class="empty">No booster roles configured.</div>'}</div>

      <div class="section-label">Swatch Folder</div>
      <form class="form card" data-upload-form>
        <div class="field-hint" style="margin-bottom:10px;">
          Upload swatch images for this server. Name each file
          <code>ColorName_HEX1_HEX2.png</code> (e.g. <code>Ruby_ff0000_8b0000.png</code>)
          — the two hex codes become the role's gradient. Files are stored in a
          folder unique to this server.
        </div>
        <div data-swatch-active class="field-hint" style="margin-bottom:10px;"></div>
        <div data-swatch-list style="margin-bottom:12px;"><div class="empty">Loading swatches…</div></div>
        <div class="field">
          <label>Add images</label>
          <input type="file" name="files" accept="image/png,image/jpeg,image/gif,image/webp" multiple data-swatch-input />
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary" data-upload-btn>Upload</button>
          <span data-upload-status></span>
        </div>
      </form>

      <div class="section-label">Sync Swatches</div>
      <form class="form card" data-sync-form>
        <div class="field-hint" style="margin-bottom:10px;">
          Scans the swatch folder and creates/updates/removes booster roles
          (with gradient colors) to match the uploaded files.
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary" data-sync-btn>Sync Swatches</button>
          <span data-sync-status></span>
        </div>
      </form>

      <div class="section-label">Add Booster Role</div>
      <form class="form card" data-add-form>
        <div class="field">
          <label>Key (short lowercase identifier)</label>
          <input type="text" name="role_key" required placeholder="e.g. ruby, sapphire" pattern="[a-z0-9_]+" />
        </div>
        <div class="field">
          <label>Label</label>
          <input type="text" name="label" required placeholder="e.g. Ruby" />
        </div>
        <div class="field">
          <label>Role</label>
          <select name="role_id">${roleSelect(roles, "0")}</select>
        </div>
        <div class="field">
          <label>Image Path</label>
          <input type="text" name="image_path" placeholder="/path/to/image.png" />
        </div>
        <div class="field">
          <label>Sort Order</label>
          <input type="number" name="sort_order" value="0" min="0" />
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Add</button>
          <span data-add-status></span>
        </div>
      </form>

      <div class="section-label">Repost Panel</div>
      <form class="form card" data-repost-form>
        <div class="field-hint" style="margin-bottom:10px;">
          Deletes the previously posted booster panel messages and posts a fresh
          set in the chosen channel.
        </div>
        <div class="field">
          <label>Channel</label>
          <select name="channel_id">${channelSelect(channels, panelChannelId, { allowNone: false })}</select>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary" data-repost-btn>Repost Panel</button>
          <span data-repost-status></span>
        </div>
      </form>
    </div>`;

  // Save handlers for existing roles
  for (const r of boosterRoles) {
    const form = container.querySelector(`[data-key="${r.role_key}"]`);
    const status = form.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut(`/api/config/booster-roles/${r.role_key}`, {
          label: fd.get("label"),
          role_id: fd.get("role_id"),
          image_path: fd.get("image_path"),
          sort_order: parseInt(fd.get("sort_order"), 10) || 0,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  // Remove handlers
  container.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!(await confirmDialog(`Remove booster role "${btn.dataset.remove}"?`, { danger: true, confirmLabel: "Remove" }))) return;
      try {
        await apiDelete(`/api/config/booster-roles/${btn.dataset.remove}`);
        const fresh = await loadConfig();
        render(
          container,
          fresh.booster_roles || [],
          fresh.booster_panel_channel_id || "0",
          roles,
          channels,
        );
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });

  // Add handler
  const addForm = container.querySelector("[data-add-form]");
  const addStatus = container.querySelector("[data-add-status]");
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(addForm);
    const key = fd.get("role_key").trim().toLowerCase().replace(/\s+/g, "_");
    if (!key) return;
    try {
      await apiPut(`/api/config/booster-roles/${key}`, {
        label: fd.get("label"),
        role_id: fd.get("role_id"),
        image_path: fd.get("image_path"),
        sort_order: parseInt(fd.get("sort_order"), 10) || 0,
      });
      const fresh = await loadConfig();
      render(
        container,
        fresh.booster_roles || [],
        fresh.booster_panel_channel_id || "0",
        roles,
        channels,
      );
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });

  // ── Managed swatch uploads ──────────────────────────────────────────
  const swatchList = container.querySelector("[data-swatch-list]");
  const swatchActive = container.querySelector("[data-swatch-active]");

  function renderSwatchList(data) {
    const files = data.files || [];
    if (!files.length) {
      swatchList.innerHTML = `<div class="empty">No swatches uploaded yet.</div>`;
    } else {
      swatchList.innerHTML = files
        .map((f) => {
          const chip = f.valid
            ? `<span style="display:inline-block;width:28px;height:18px;border-radius:4px;border:1px solid var(--border,#333);background:linear-gradient(135deg,#${_esc(f.hex1)},#${_esc(f.hex2)});flex:none;"></span>`
            : `<span style="display:inline-block;width:28px;height:18px;border-radius:4px;border:1px solid var(--border,#333);background:repeating-linear-gradient(45deg,#555,#555 4px,#333 4px,#333 8px);flex:none;"></span>`;
          const meta = f.valid
            ? `<span>${_esc(f.label)}</span>`
            : `<span style="color:var(--danger,#e55)">⚠ rename to ColorName_HEX1_HEX2.ext — won't sync</span>`;
          return `
            <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border,#2a2a2a);">
              ${chip}
              <span style="flex:none;font-family:monospace;opacity:.85;">${_esc(f.name)}</span>
              ${meta}
              <button type="button" class="btn btn-danger" style="margin-left:auto;padding:2px 8px;" data-swatch-del="${_esc(f.name)}">Delete</button>
            </div>`;
        })
        .join("");
    }
    if (data.using_managed) {
      swatchActive.textContent = "";
    } else {
      swatchActive.innerHTML = `<strong>Sync currently scans an external folder:</strong> <code>${_esc(data.active_dir)}</code>. Upload at least one validly named swatch to switch syncing to this server's uploaded set.`;
    }
    // Delete handlers
    swatchList.querySelectorAll("[data-swatch-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.swatchDel;
        if (!(await confirmDialog(`Delete swatch "${name}"?`, { danger: true, confirmLabel: "Delete" }))) return;
        try {
          const fresh = await apiDelete(
            `/api/config/booster-roles/swatches/${encodeURIComponent(name)}`,
          );
          renderSwatchList(fresh);
        } catch (err) {
          toast(err.message, "error");
        }
      });
    });
  }

  async function loadSwatches() {
    try {
      const res = await fetch("/api/config/booster-roles/swatches", {
        credentials: "same-origin",
      });
      if (!res.ok) throw new Error(res.statusText);
      renderSwatchList(await res.json());
    } catch (err) {
      swatchList.innerHTML = `<div class="error">${_esc(err.message)}</div>`;
    }
  }

  loadSwatches();

  const uploadForm = container.querySelector("[data-upload-form]");
  const uploadBtn = container.querySelector("[data-upload-btn]");
  const uploadStatus = container.querySelector("[data-upload-status]");
  const swatchInput = container.querySelector("[data-swatch-input]");
  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!swatchInput.files.length) {
      showStatus(uploadStatus, false, "Pick a file");
      return;
    }
    const fd = new FormData();
    for (const file of swatchInput.files) fd.append("files", file);
    uploadBtn.disabled = true;
    showStatus(uploadStatus, true, "Uploading…");
    try {
      const res = await fetch("/api/config/booster-roles/swatches", {
        method: "POST",
        credentials: "same-origin",
        body: fd,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.statusText);
      }
      const data = await res.json();
      renderSwatchList(data);
      swatchInput.value = "";
      showStatus(uploadStatus, true, `Uploaded ${data.saved?.length || 0}`);
    } catch (err) {
      showStatus(uploadStatus, false, err.message);
    } finally {
      uploadBtn.disabled = false;
    }
  });

  // Sync swatches handler
  const syncForm = container.querySelector("[data-sync-form]");
  const syncBtn = container.querySelector("[data-sync-btn]");
  const syncStatus = container.querySelector("[data-sync-status]");
  syncForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    syncBtn.disabled = true;
    showStatus(syncStatus, true, "Syncing…");
    try {
      const res = await fetch("/api/config/booster-roles/sync-swatches", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.statusText);
      }
      const data = await res.json();
      const parts = [];
      if (data.created?.length) parts.push(`created ${data.created.length}`);
      if (data.removed?.length) parts.push(`removed ${data.removed.length}`);
      const msg = parts.length ? parts.join(", ") : "already in sync";
      showStatus(syncStatus, true, msg);
      const fresh = await loadConfig();
      render(
        container,
        fresh.booster_roles || [],
        fresh.booster_panel_channel_id || "0",
        roles,
        channels,
      );
    } catch (err) {
      showStatus(syncStatus, false, err.message);
    } finally {
      syncBtn.disabled = false;
    }
  });

  // Repost panel handler
  const repostForm = container.querySelector("[data-repost-form]");
  const repostBtn = container.querySelector("[data-repost-btn]");
  const repostStatus = container.querySelector("[data-repost-status]");
  repostForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(repostForm);
    const channelId = fd.get("channel_id");
    if (!channelId || channelId === "0") {
      showStatus(repostStatus, false, "Pick a channel");
      return;
    }
    if (!(await confirmDialog("Repost the booster panel? The previously posted panel messages will be deleted.", { danger: true, confirmLabel: "Repost" }))) return;
    repostBtn.disabled = true;
    try {
      const res = await fetch("/api/config/booster-roles/post-panel", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel_id: channelId }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.statusText);
      }
      const data = await res.json();
      showStatus(repostStatus, true, `Posted ${data.message_count || ""} message(s)`);
    } catch (err) {
      showStatus(repostStatus, false, err.message);
    } finally {
      repostBtn.disabled = false;
    }
  });
}
