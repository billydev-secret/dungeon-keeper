import { api, apiPost, esc } from "../api.js";
import {
  loadConfig, loadRoles, loadChannels, apiPut, apiDelete, showStatus,
  guardForm, renderMetaWarning, mountRolePicker, mountChannelPicker,
} from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading booster roles…</div></div>`;

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
    const key = esc(r.role_key);
    return `
      <form class="form card" style="margin-bottom:16px;" data-key="${key}">
        <div class="section-label">${esc(r.label || r.role_key)}</div>
        <div class="field">
          <label for="br-label-${key}">Display Name</label>
          <input type="text" name="label" id="br-label-${key}" required value="${esc(r.label || "")}" />
          <div class="field-hint">The color's name on the booster panel button. Its internal key is <code>${key}</code> and can't be changed.</div>
        </div>
        <div class="field">
          <label>Discord Role</label>
          <span data-picker="role_id" data-key="${key}"></span>
          <div class="field-hint">The role a booster receives when they choose this color. Choose "(none)" and the button does nothing.</div>
        </div>
        <div class="field">
          <label for="br-image-${key}">Swatch Image File</label>
          <input type="text" name="image_path" id="br-image-${key}" value="${esc(r.image_path || "")}" />
          <div class="field-hint">The file name of the color swatch shown next to this option, e.g. <code>Ruby_ff0000_8b0000.png</code>. Upload the file under Swatch Images below; leave this blank for no swatch.</div>
        </div>
        <div class="field">
          <label for="br-sort-${key}">Position in the List</label>
          <input type="number" name="sort_order" id="br-sort-${key}" required value="${esc(String(r.sort_order ?? 0))}" min="0" step="1" style="max-width:140px;" />
          <div class="field-hint">Lower numbers appear first on the panel. Ties are broken by display name.</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <button type="submit" class="btn btn-primary">Save</button>
          <button type="button" class="btn btn-danger" data-remove="${key}">Delete Color</button>
          <span data-status></span>
        </div>
      </form>`;
  }

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Booster Roles</h2>
        <div class="subtitle">Name colors your server boosters can choose from as a thank-you perk</div>
      </header>
      ${renderMetaWarning()}
      <div data-roles>${boosterRoles.length
        ? boosterRoles.map(roleCard).join("")
        : '<div class="empty">No booster colors yet. Upload swatch images below and sync, or add one by hand.</div>'}</div>

      <div class="section-label">Swatch Images</div>
      <form class="form card" data-upload-form>
        <div class="field-hint" style="margin-bottom:10px;">
          Upload one image per color. Name each file
          <code>ColorName_HEX1_HEX2.png</code> — for example
          <code>Ruby_ff0000_8b0000.png</code> — and the two hex codes become that
          role's gradient. Images are stored for this server only.
        </div>
        <div data-swatch-active class="field-hint" style="margin-bottom:10px;"></div>
        <div data-swatch-list style="margin-bottom:12px;"><div class="empty">Loading swatch images…</div></div>
        <div class="field">
          <label for="br-swatch-input">Image Files</label>
          <input type="file" id="br-swatch-input" name="files" accept="image/png,image/jpeg,image/gif,image/webp" multiple data-swatch-input />
          <div class="field-hint">You can select several files at once. Uploading doesn't create any roles on its own — press Sync Colors afterwards.</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary" data-upload-btn>Upload Images</button>
          <span data-upload-status></span>
        </div>
      </form>

      <div class="section-label">Sync Colors From Swatches</div>
      <form class="form card" data-sync-form>
        <div class="field-hint" style="margin-bottom:10px;">
          Reads the swatch images above and makes the booster colors match them:
          creating Discord roles with the right gradient for new images, updating
          existing ones, and <strong>deleting colors whose image is gone</strong>.
          Boosters wearing a deleted color lose it.
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary" data-sync-btn>Sync Colors</button>
          <span data-sync-status></span>
        </div>
      </form>

      <div class="section-label">Add a Color by Hand</div>
      <form class="form card" data-add-form>
        <div class="field">
          <label for="br-new-key">Internal Key</label>
          <input type="text" name="role_key" id="br-new-key" required placeholder="ruby" pattern="[a-z0-9_]+" />
          <div class="field-hint">A short lowercase name — letters, numbers, and underscores only. It can't be changed later.</div>
        </div>
        <div class="field">
          <label for="br-new-label">Display Name</label>
          <input type="text" name="label" id="br-new-label" required placeholder="Ruby" />
          <div class="field-hint">What boosters see on the panel button.</div>
        </div>
        <div class="field">
          <label>Discord Role</label>
          <span data-picker="role_id" data-key="__new__"></span>
          <div class="field-hint">The role a booster receives when they choose this color.</div>
        </div>
        <div class="field">
          <label for="br-new-image">Swatch Image File</label>
          <input type="text" name="image_path" id="br-new-image" placeholder="Ruby_ff0000_8b0000.png" />
          <div class="field-hint">The file name of the swatch shown next to this option. Leave blank for no swatch.</div>
        </div>
        <div class="field">
          <label for="br-new-sort">Position in the List</label>
          <input type="number" name="sort_order" id="br-new-sort" required value="0" min="0" step="1" style="max-width:140px;" />
          <div class="field-hint">Lower numbers appear first on the panel.</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Add Color</button>
          <span data-add-status></span>
        </div>
      </form>

      <div class="section-label">Booster Panel</div>
      <form class="form card" data-repost-form>
        <div class="field-hint" style="margin-bottom:10px;">
          Posts the panel where boosters pick their color. The panel messages
          posted last time are <strong>deleted</strong> first, so post it again
          whenever you change the colors above.
        </div>
        <div class="field">
          <label>Channel</label>
          <span data-picker="panel_channel_id"></span>
          <div class="field-hint">Where the panel is posted. Boosters press its buttons to claim a color.</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary" data-repost-btn>Post Panel</button>
          <span data-repost-status></span>
        </div>
      </form>
    </div>`;

  // ── Searchable pickers replace the old plain <select>s (W-C4). ────────
  const rolePickers = {};
  for (const r of boosterRoles) {
    const slot = container.querySelector(
      `[data-picker="role_id"][data-key="${CSS.escape(r.role_key)}"]`,
    );
    rolePickers[r.role_key] = mountRolePicker(
      slot, roles, String(r.role_id || "0"), { label: "Discord Role" },
    );
  }
  rolePickers.__new__ = mountRolePicker(
    container.querySelector('[data-picker="role_id"][data-key="__new__"]'),
    roles, "0", { label: "Discord Role" },
  );

  // Read + validate one color's fields. Returns null after reporting an error.
  function readColor(form, status, key) {
    const fd = new FormData(form);
    const label = String(fd.get("label") || "").trim();
    if (!label) {
      showStatus(status, false, "Display Name cannot be empty.");
      form.querySelector('[name="label"]').focus();
      return null;
    }
    const rawSort = String(fd.get("sort_order") ?? "").trim();
    const sort = parseInt(rawSort, 10);
    if (rawSort === "" || !Number.isFinite(sort) || sort < 0) {
      showStatus(status, false, "Position in the List must be a whole number of 0 or more.");
      form.querySelector('[name="sort_order"]').focus();
      return null;
    }
    return {
      label,
      // Snowflakes stay strings; "0" is the unset sentinel.
      role_id: rolePickers[key].getValue() || "0",
      image_path: fd.get("image_path"),
      sort_order: sort,
    };
  }

  // Save handlers for existing roles
  for (const r of boosterRoles) {
    const form = container.querySelector(`[data-key="${CSS.escape(r.role_key)}"]`);
    const status = form.querySelector("[data-status]");
    guardForm(form);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = readColor(form, status, r.role_key);
      if (!payload) return;
      try {
        await apiPut(`/api/config/booster-roles/${r.role_key}`, payload);
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  // Remove handlers
  container.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.remove;
      const entry = boosterRoles.find((r) => r.role_key === key);
      const ok = await confirmDialog(
        `Delete the "${entry ? entry.label || key : key}" booster color? It disappears from the panel `
        + "and boosters can no longer choose it.",
        { title: "Delete Booster Color", danger: true, confirmLabel: "Delete Color" },
      );
      if (!ok) return;
      try {
        await apiDelete(`/api/config/booster-roles/${key}`);
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
  guardForm(addForm);
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(addForm);
    const key = String(fd.get("role_key") || "").trim().toLowerCase().replace(/\s+/g, "_");
    if (!key) {
      showStatus(addStatus, false, "Internal Key cannot be empty.");
      addForm.querySelector('[name="role_key"]').focus();
      return;
    }
    if (!/^[a-z0-9_]+$/.test(key)) {
      showStatus(addStatus, false, "Internal Key may only contain lowercase letters, numbers, and underscores.");
      addForm.querySelector('[name="role_key"]').focus();
      return;
    }
    if (boosterRoles.some((r) => r.role_key === key)) {
      showStatus(addStatus, false, `A color with the key "${key}" already exists.`);
      addForm.querySelector('[name="role_key"]').focus();
      return;
    }
    const payload = readColor(addForm, addStatus, "__new__");
    if (!payload) return;
    try {
      await apiPut(`/api/config/booster-roles/${key}`, payload);
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
      swatchList.innerHTML = `<div class="empty">No swatch images uploaded yet.</div>`;
    } else {
      swatchList.innerHTML = files
        .map((f) => {
          const chip = f.valid
            ? `<span style="display:inline-block;width:28px;height:18px;border-radius:4px;border:1px solid var(--border,#333);background:linear-gradient(135deg,#${esc(f.hex1)},#${esc(f.hex2)});flex:none;"></span>`
            : `<span style="display:inline-block;width:28px;height:18px;border-radius:4px;border:1px solid var(--border,#333);background:repeating-linear-gradient(45deg,#555,#555 4px,#333 4px,#333 8px);flex:none;"></span>`;
          const meta = f.valid
            ? `<span>${esc(f.label)}</span>`
            : `<span style="color:var(--danger,#e55)">⚠ This file is skipped when syncing — rename it to ColorName_HEX1_HEX2 plus its extension.</span>`;
          return `
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 0;border-bottom:1px solid var(--border,#2a2a2a);">
              ${chip}
              <span style="flex:none;font-family:monospace;opacity:.85;">${esc(f.name)}</span>
              ${meta}
              <button type="button" class="btn btn-danger" style="margin-left:auto;padding:2px 8px;" data-swatch-del="${esc(f.name)}">Delete</button>
            </div>`;
        })
        .join("");
    }
    if (data.using_managed) {
      swatchActive.textContent = "";
    } else {
      swatchActive.innerHTML = `<strong>Syncing currently reads a folder on the server:</strong> <code>${esc(data.active_dir)}</code>. Upload at least one correctly named image here to switch syncing over to this server's own set.`;
    }
    // Delete handlers
    swatchList.querySelectorAll("[data-swatch-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.swatchDel;
        const ok = await confirmDialog(
          `Delete the image "${name}"? The next sync will remove the color it belongs to, and boosters wearing it lose the role.`,
          { title: "Delete Swatch Image", danger: true, confirmLabel: "Delete Image" },
        );
        if (!ok) return;
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
      renderSwatchList(await api("/api/config/booster-roles/swatches"));
    } catch (err) {
      swatchList.innerHTML = `<div class="error">Couldn't load the swatch images: ${esc(err.message)}</div>`;
    }
  }

  loadSwatches();

  const uploadForm = container.querySelector("[data-upload-form]");
  const uploadBtn = container.querySelector("[data-upload-btn]");
  const uploadStatus = container.querySelector("[data-upload-status]");
  const swatchInput = container.querySelector("[data-swatch-input]");
  guardForm(uploadForm);
  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!swatchInput.files.length) {
      showStatus(uploadStatus, false, "Choose at least one image file first.");
      swatchInput.focus();
      return;
    }
    const fd = new FormData();
    for (const file of swatchInput.files) fd.append("files", file);
    uploadBtn.disabled = true;
    uploadStatus.textContent = "Uploading…";
    try {
      const data = await apiPost("/api/config/booster-roles/swatches", fd);
      renderSwatchList(data);
      swatchInput.value = "";
      const n = data.saved?.length || 0;
      showStatus(uploadStatus, true, `Uploaded ${n} image${n === 1 ? "" : "s"}`);
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
    const ok = await confirmDialog(
      "Make the booster colors match the uploaded swatch images? Colors whose image is missing "
      + "are deleted, and any booster wearing one loses that role.",
      { title: "Sync Colors", danger: true, confirmLabel: "Sync Colors" },
    );
    if (!ok) return;
    syncBtn.disabled = true;
    syncStatus.textContent = "Syncing…";
    try {
      const data = await apiPost("/api/config/booster-roles/sync-swatches");
      const parts = [];
      if (data.created?.length) parts.push(`added ${data.created.length}`);
      if (data.removed?.length) parts.push(`deleted ${data.removed.length}`);
      const msg = parts.length ? parts.join(", ") : "Already up to date";
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
  const repostPicker = mountChannelPicker(
    repostForm.querySelector('[data-picker="panel_channel_id"]'),
    channels, String(panelChannelId || "0"),
    { emptyValue: "0", emptyLabel: "(pick a channel)", label: "Channel" },
  );
  guardForm(repostForm);
  repostForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const channelId = repostPicker.getValue() || "0";
    if (channelId === "0") {
      showStatus(repostStatus, false, "Pick a channel first.");
      return;
    }
    const ok = await confirmDialog(
      "Post the booster panel? The panel posted last time is deleted first, so its buttons stop working.",
      { title: "Post Booster Panel", danger: true, confirmLabel: "Post Panel" },
    );
    if (!ok) return;
    repostBtn.disabled = true;
    try {
      const data = await apiPost("/api/config/booster-roles/post-panel", { channel_id: channelId });
      const n = data.message_count || 0;
      showStatus(repostStatus, true, `Posted ${n} message${n === 1 ? "" : "s"}`);
    } catch (err) {
      showStatus(repostStatus, false, err.message);
    } finally {
      repostBtn.disabled = false;
    }
  });
}
