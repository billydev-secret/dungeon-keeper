import { api, apiPost, apiPut, apiDelete, esc } from "../api.js";
import { loadChannels, channelName, mountChannelPicker, showStatus } from "../config-helpers.js";
import { mdToHtml } from "../md-preview.js";
import { renderLoading, renderEmpty } from "../states.js";

const MODES = [
  ["toggle", "Toggle", "Click to get the role, click again to drop it."],
  ["unique", "Unique", "Only ever one role from this menu at a time."],
  ["verify", "Verify", "Roles can only be gained here, never removed."],
  ["drop", "Drop", "Roles can only be removed here, never gained."],
  ["binding", "Binding", "First choice is permanent — one pick, ever."],
];
const BUTTON_COLORS = [
  ["secondary", "Gray"], ["primary", "Blurple"], ["success", "Green"], ["danger", "Red"],
];
const MAX_OPTIONS = 25;

// Render an emoji field for preview: custom `<:name:id>` forms become CDN
// images, anything else is shown as typed.
function emojiHtml(raw) {
  const m = (raw || "").trim().match(/^<(a?):(\w+):(\d+)>$/);
  if (m) {
    const ext = m[1] ? "gif" : "png";
    return `<img class="rm-emoji" src="https://cdn.discordapp.com/emojis/${m[3]}.${ext}" alt=":${esc(m[2])}:">`;
  }
  return esc(raw || "");
}

function renderList(menus, activeId) {
  if (!menus.length) return renderEmpty("No role menus yet. Create one to get started.");
  return menus.map((m) => {
    const cls = m.id === activeId ? " active" : "";
    const where = m.published ? "published" : "draft";
    return `
      <div class="ticket-item med${cls}" data-menu-id="${m.id}">
        <div class="pri"></div>
        <div class="body">
          <div class="subj">${m.enabled ? "" : "⏸ "}${esc(m.title || "(untitled menu)")}${m.health.length ? " ⚠️" : ""}</div>
          <div class="row">
            <span>${esc(m.style)} · ${esc(m.mode)} · ${m.option_count} role${m.option_count === 1 ? "" : "s"}</span>
            <span>${esc(where)}</span>
          </div>
        </div>
      </div>`;
  }).join("");
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Role Menus</h2>
        <div class="subtitle">Self-service roles: members click buttons or pick from a dropdown on a DK-posted embed. Build here, publish to a channel — no commands, no reactions.</div>
      </header>
      <section class="mod-split">
        <div class="ticket-list-wrap">
          <div class="ticket-list-head">
            <h3>Menus</h3>
            <button class="act-btn" data-new-btn>New Menu</button>
          </div>
          <div class="ticket-list" data-list>${renderLoading("Loading…")}</div>
        </div>
        <div class="ticket-detail" data-editor>
          <div class="empty" style="padding:24px">Select a menu, or create one.</div>
        </div>
      </section>
    </div>`;

  const listEl = container.querySelector("[data-list]");
  const editorEl = container.querySelector("[data-editor]");
  const newBtn = container.querySelector("[data-new-btn]");

  const state = {
    menus: [], activeId: null, menu: null,
    channels: [], roles: [], saving: false, previewTimer: null,
  };

  loadChannels().then((chs) => { state.channels = chs || []; });
  api("/api/role-menus/roles")
    .then((data) => { state.roles = data.roles || []; })
    .catch(() => { state.roles = []; });

  function renderMenuList() {
    listEl.innerHTML = renderList(state.menus, state.activeId);
  }

  async function refreshList() {
    try {
      const data = await api("/api/role-menus");
      state.menus = data.menus || [];
      renderMenuList();
    } catch (err) {
      listEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
    }
  }

  async function selectMenu(id) {
    state.activeId = id;
    renderMenuList();
    editorEl.innerHTML = renderLoading("Loading…");
    try {
      state.menu = await api(`/api/role-menus/${id}`);
      renderEditor();
    } catch (err) {
      editorEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
    }
  }

  // ── options table ───────────────────────────────────────────────

  function roleSelectHtml(opt, idx) {
    const current = String(opt.role_id || "0");
    const showDangerous = !!opt.elevated;
    let found = false;
    let html = `<select class="rm-role-sel" data-opt="${idx}" data-field="role_id">`;
    html += `<option value="0">(pick a role)</option>`;
    for (const r of state.roles) {
      if (!r.assignable) continue;
      if (r.dangerous && !showDangerous && r.id !== current) continue;
      const sel = r.id === current ? " selected" : "";
      if (sel) found = true;
      const warn = r.dangerous ? " ⚠" : "";
      html += `<option value="${r.id}"${sel}>@${esc(r.name)}${warn}</option>`;
    }
    if (!found && current !== "0") {
      const known = state.roles.find((r) => r.id === current);
      const label = known ? `@${known.name} (unmanageable)` : `role ${current} (missing)`;
      html += `<option value="${current}" selected>${esc(label)} ⚠</option>`;
    }
    return html + "</select>";
  }

  function optionRowHtml(opt, idx, style) {
    const variable = style === "dropdown"
      ? `<input type="text" class="rm-opt-desc" data-opt="${idx}" data-field="description"
               maxlength="100" placeholder="Short description (optional)" value="${esc(opt.description || "")}">`
      : `<select class="rm-opt-color" data-opt="${idx}" data-field="button_color">${
          BUTTON_COLORS.map(([v, l]) =>
            `<option value="${v}"${(opt.button_color || "secondary") === v ? " selected" : ""}>${l}</option>`
          ).join("")}</select>`;
    return `
      <div class="rm-opt-row" data-opt-row="${idx}">
        <div class="rm-opt-move">
          <button class="rm-mv" data-move-up="${idx}" title="Move up" ${idx === 0 ? "disabled" : ""}>▲</button>
          <button class="rm-mv" data-move-down="${idx}" title="Move down">▼</button>
        </div>
        <input type="text" class="rm-opt-emoji" data-opt="${idx}" data-field="emoji"
               maxlength="64" placeholder="🙂" title="Emoji (optional) — paste a unicode emoji or a custom <:name:id>"
               value="${esc(opt.emoji || "")}">
        <input type="text" class="rm-opt-label" data-opt="${idx}" data-field="label"
               maxlength="80" placeholder="Label" value="${esc(opt.label || "")}">
        ${roleSelectHtml(opt, idx)}
        ${variable}
        <label class="rm-elevated" title="Allow a role with elevated permissions. Logged loudly.">
          <input type="checkbox" data-opt="${idx}" data-field="elevated" ${opt.elevated ? "checked" : ""}>⚠
        </label>
        <button class="doc-x" data-remove-opt="${idx}" title="Remove choice">✕</button>
      </div>`;
  }

  function renderOptions() {
    const wrap = editorEl.querySelector("[data-options]");
    if (!wrap || !state.menu) return;
    const style = currentStyle();
    const opts = state.menu.options;
    wrap.innerHTML = opts.map((o, i) => optionRowHtml(o, i, style)).join("")
      + `<div class="rm-opt-foot">
           <button class="act-btn ghost" data-add-opt ${opts.length >= MAX_OPTIONS ? "disabled" : ""}>+ Add choice</button>
           <span class="field-hint">${opts.length}/${MAX_OPTIONS} choices</span>
         </div>`;
  }

  // ── live preview (all client-side) ─────────────────────────────

  function currentStyle() {
    return editorEl.querySelector("[data-style]")?.value || state.menu.style || "buttons";
  }
  function currentMode() {
    return editorEl.querySelector("[data-mode]")?.value || state.menu.mode || "toggle";
  }

  function renderPreviewNow() {
    const previewEl = editorEl.querySelector("[data-preview]");
    if (!previewEl || !state.menu) return;
    const title = editorEl.querySelector("[data-title]")?.value ?? "";
    const desc = editorEl.querySelector("[data-desc]")?.value ?? "";
    const accent = editorEl.querySelector("[data-accent]")?.value ?? "";
    const thumb = editorEl.querySelector("[data-thumb]")?.value ?? "";
    const placeholder = editorEl.querySelector("[data-placeholder]")?.value ?? "";
    const style = currentStyle();
    const opts = state.menu.options.filter((o) => o.label || o.role_id !== "0");

    const bar = /^#?[0-9a-fA-F]{6}$/.test(accent) ? (accent[0] === "#" ? accent : "#" + accent) : "var(--accent, #E6B84C)";
    const thumbHtml = /^https?:/i.test(thumb)
      ? `<img class="rm-thumb" src="${esc(thumb)}" alt="" loading="lazy">` : "";
    let components = "";
    if (style === "buttons") {
      const rows = [];
      for (let i = 0; i < opts.length; i += 5) {
        rows.push(`<div class="rm-btnrow">${opts.slice(i, i + 5).map((o) => `
          <span class="rm-btn rm-btn-${esc(o.button_color || "secondary")}">${emojiHtml(o.emoji)}${o.emoji ? " " : ""}${esc(o.label || "…")}</span>`).join("")}</div>`);
      }
      components = rows.join("");
    } else if (opts.length) {
      components = `
        <div class="rm-select">
          <span class="rm-select-ph">${esc(placeholder || "Make a selection")}</span><span class="rm-select-chev">▾</span>
        </div>
        <div class="rm-select-opts">${opts.map((o) => `
          <div class="rm-sel-opt">
            <span class="rm-sel-emoji">${emojiHtml(o.emoji)}</span>
            <span class="rm-sel-main"><span class="rm-sel-label">${esc(o.label || "…")}</span>
            ${o.description ? `<span class="rm-sel-desc">${esc(o.description)}</span>` : ""}</span>
          </div>`).join("")}</div>`;
    }
    previewEl.innerHTML = `
      <div class="dp-embed" style="border-left-color:${esc(bar)}">
        ${thumbHtml}
        ${title ? `<div class="dp-title">${esc(title)}</div>` : ""}
        <div class="dp-desc">${mdToHtml(desc)}</div>
      </div>
      ${components || '<div class="field-hint" style="padding:6px 2px">Add choices to see the components.</div>'}`;
  }

  function schedulePreview() {
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(renderPreviewNow, 200);
  }

  // ── editor ──────────────────────────────────────────────────────

  function healthHtml(health) {
    if (!health || !health.length) return "";
    return `<div class="rm-health">${health.map((h) =>
      `<div class="rm-health-row">⚠️ ${esc(h.detail)}</div>`).join("")}</div>`;
  }

  function renderEditor() {
    const menu = state.menu;
    if (!menu) return;
    const modeMeta = MODES.find(([k]) => k === menu.mode) || MODES[0];
    editorEl.innerHTML = `
      <div class="doc-ed">
        ${healthHtml(menu.health)}
        <div class="doc-ed-head">
          <input class="doc-ed-title" data-title type="text" maxlength="256"
                 placeholder="Menu title" value="${esc(menu.title)}" />
          <input class="doc-ed-accent" data-accent type="text" maxlength="7"
                 placeholder="#hex" value="${esc(menu.accent)}" title="Accent color (blank = server branding)" />
        </div>
        <div class="doc-ed-grid">
          <div class="doc-ed-col">
            <label class="doc-ed-lbl">Description (markdown)</label>
            <textarea class="doc-ed-area rm-desc" data-desc spellcheck="true"
                      placeholder="Pick your colors…">${esc(menu.description)}</textarea>
            <label class="doc-ed-lbl">Thumbnail URL (optional)</label>
            <input type="text" class="rm-wide-input" data-thumb placeholder="https://…" value="${esc(menu.thumbnail_url)}">
            <div class="rm-settings">
              <label>Style
                <select data-style>
                  <option value="buttons"${menu.style === "buttons" ? " selected" : ""}>Buttons</option>
                  <option value="dropdown"${menu.style === "dropdown" ? " selected" : ""}>Dropdown</option>
                </select>
              </label>
              <label>Mode
                <select data-mode>${MODES.map(([v, l]) =>
                  `<option value="${v}"${menu.mode === v ? " selected" : ""}>${l}</option>`).join("")}</select>
              </label>
              <label>Max roles
                <input type="number" data-max-roles min="0" max="25" value="${menu.max_roles}" title="0 = no cap">
              </label>
              <label>Cooldown (s)
                <input type="number" data-cooldown min="0" max="3600" value="${menu.cooldown_seconds}" title="0 = none">
              </label>
              <label class="rm-required">Required role
                <span data-required-picker></span>
              </label>
              <label class="rm-placeholder-field"${menu.style === "dropdown" ? "" : " hidden"}>Dropdown placeholder
                <input type="text" data-placeholder maxlength="150" value="${esc(menu.placeholder)}" placeholder="Pick your colors…">
              </label>
            </div>
            <div class="field-hint" data-mode-hint>${esc(modeMeta[2])}</div>
            <label class="doc-ed-lbl" style="margin-top:8px">Choices</label>
            <div data-options></div>
            <div class="doc-ed-actions">
              <button class="act-btn" data-save>Save${menu.published ? " &amp; update live" : ""}</button>
              <span class="save-status" data-status></span>
              <span class="act-spacer" style="flex:1"></span>
              <button class="doc-danger" data-delete>Delete</button>
            </div>
          </div>
          <div class="doc-ed-col">
            <label class="doc-ed-lbl">Preview</label>
            <div class="doc-preview" data-preview></div>
            <div class="rm-publish">
              <div class="doc-place-head"><h4>Publishing</h4></div>
              ${menu.published ? `
                <div class="doc-place-row">
                  <span class="doc-place-ch">#${esc(channelName(state.channels, menu.channel_id) || menu.channel_id)}</span>
                  <span class="doc-place-count">${menu.enabled ? "live" : "off (post stays as decor)"}</span>
                </div>` : `
                <div class="field-hint" style="padding:4px 0">Draft — not posted anywhere yet.</div>`}
              <div class="doc-add-row">
                <span data-ch-picker></span>
                <button class="act-btn" data-publish>${menu.published ? "Move / repost" : "Publish"}</button>
              </div>
              <div class="doc-add-row">
                ${menu.published ? `
                  <button class="act-btn ghost" data-toggle-enabled>${menu.enabled ? "Unpublish (turn off)" : "Turn back on"}</button>
                  <button class="act-btn ghost" data-update-live>Update Live Message</button>` : ""}
              </div>
            </div>
          </div>
        </div>
      </div>`;

    const picker = mountChannelPicker(
      editorEl.querySelector("[data-ch-picker]"), state.channels,
      menu.channel_id !== "0" ? menu.channel_id : "0",
      { placeholder: "Pick a channel…" });
    editorEl._picker = picker;

    const reqRoles = state.roles.filter((r) => !r.dangerous || r.id === menu.required_role_id);
    const reqPicker = mountChannelPickerLikeRole(
      editorEl.querySelector("[data-required-picker]"), reqRoles, menu.required_role_id);
    editorEl._reqPicker = reqPicker;

    renderOptions();
    renderPreviewNow();
  }

  // A role picker with the same look as the channel picker (searchable).
  function mountChannelPickerLikeRole(slotEl, roles, value) {
    // config-helpers has mountRolePicker but it expects /api/meta/roles shape;
    // our roles endpoint matches ({id, name}), so reuse it directly.
    const sel = document.createElement("select");
    sel.className = "rm-role-sel";
    sel.dataset.requiredRole = "1";
    let html = `<option value="0">(none — open to everyone)</option>`;
    for (const r of roles) {
      html += `<option value="${r.id}"${r.id === value ? " selected" : ""}>@${esc(r.name)}</option>`;
    }
    sel.innerHTML = html;
    slotEl.replaceWith(sel);
    return { getValue: () => sel.value };
  }

  function collectInputs() {
    const menu = state.menu;
    return {
      title: editorEl.querySelector("[data-title]")?.value ?? "",
      description: editorEl.querySelector("[data-desc]")?.value ?? "",
      accent: editorEl.querySelector("[data-accent]")?.value ?? "",
      thumbnail_url: editorEl.querySelector("[data-thumb]")?.value ?? "",
      style: currentStyle(),
      mode: currentMode(),
      max_roles: parseInt(editorEl.querySelector("[data-max-roles]")?.value || "0", 10) || 0,
      required_role_id: editorEl._reqPicker ? editorEl._reqPicker.getValue() : "0",
      cooldown_seconds: parseInt(editorEl.querySelector("[data-cooldown]")?.value || "0", 10) || 0,
      placeholder: editorEl.querySelector("[data-placeholder]")?.value ?? "",
      options: menu.options
        .filter((o) => o.role_id && o.role_id !== "0")
        .map((o) => ({
          role_id: String(o.role_id),
          label: o.label || "",
          emoji: o.emoji || "",
          description: o.description || "",
          button_color: o.button_color || "secondary",
          elevated: !!o.elevated,
        })),
    };
  }

  // ── list + new-menu interactions ────────────────────────────────

  listEl.addEventListener("click", (e) => {
    const row = e.target.closest(".ticket-item");
    if (!row) return;
    selectMenu(parseInt(row.dataset.menuId, 10));
  });

  newBtn.addEventListener("click", async () => {
    const title = prompt("Title for the new menu — e.g. Colors, Ping Roles:");
    if (title == null) return;
    try {
      const menu = await apiPost("/api/role-menus", { title: title.trim() });
      await refreshList();
      selectMenu(menu.id);
    } catch (err) {
      alert(err.message);
    }
  });

  // ── editor interactions (delegated) ─────────────────────────────

  editorEl.addEventListener("input", (e) => {
    const t = e.target;
    const idx = t.dataset.opt;
    if (idx !== undefined && state.menu) {
      const opt = state.menu.options[parseInt(idx, 10)];
      if (opt) {
        const field = t.dataset.field;
        opt[field] = t.type === "checkbox" ? t.checked : t.value;
        if (field === "elevated") renderOptions();
      }
    }
    if (t.matches("[data-style]")) {
      editorEl.querySelector(".rm-placeholder-field")?.toggleAttribute("hidden", t.value !== "dropdown");
      renderOptions();
    }
    if (t.matches("[data-mode]")) {
      const meta = MODES.find(([k]) => k === t.value);
      const hint = editorEl.querySelector("[data-mode-hint]");
      if (hint && meta) hint.textContent = meta[2];
    }
    schedulePreview();
  });

  editorEl.addEventListener("click", async (e) => {
    const menu = state.menu;
    if (!menu) return;
    const statusEl = editorEl.querySelector("[data-status]");

    const up = e.target.closest("[data-move-up]");
    const down = e.target.closest("[data-move-down]");
    if (up || down) {
      const idx = parseInt((up || down).dataset.moveUp ?? (up || down).dataset.moveDown, 10);
      const to = up ? idx - 1 : idx + 1;
      if (to >= 0 && to < menu.options.length) {
        const [row] = menu.options.splice(idx, 1);
        menu.options.splice(to, 0, row);
        renderOptions();
        schedulePreview();
      }
      return;
    }

    if (e.target.closest("[data-add-opt]")) {
      if (menu.options.length >= MAX_OPTIONS) return;
      menu.options.push({
        role_id: "0", label: "", emoji: "", description: "",
        button_color: "secondary", elevated: false,
      });
      renderOptions();
      return;
    }

    const rm = e.target.closest("[data-remove-opt]");
    if (rm) {
      menu.options.splice(parseInt(rm.dataset.removeOpt, 10), 1);
      renderOptions();
      schedulePreview();
      return;
    }

    if (e.target.closest("[data-save]") || e.target.closest("[data-update-live]")) {
      if (state.saving) return;
      state.saving = true;
      const btn = e.target.closest("[data-save]") || e.target.closest("[data-update-live]");
      btn.disabled = true;
      try {
        const res = await apiPut(`/api/role-menus/${menu.id}`, collectInputs());
        state.menu = res.menu;
        const syncNote = res.sync && res.sync.status !== "ok"
          ? ` ⚠️ ${res.sync.detail || res.sync.status}` : "";
        showStatus(statusEl, !syncNote, `Saved${res.sync && res.sync.status === "ok" ? " — live message updated" : ""}${syncNote}`);
        renderEditor();
        await refreshList();
      } catch (err) {
        showStatus(statusEl, false, err.message);
      } finally {
        state.saving = false;
        btn.disabled = false;
      }
      return;
    }

    if (e.target.closest("[data-publish]")) {
      const channelId = editorEl._picker ? editorEl._picker.getValue() : "0";
      if (!channelId || channelId === "0") { showStatus(statusEl, false, "Pick a channel first."); return; }
      const btn = e.target.closest("[data-publish]");
      btn.disabled = true;
      try {
        // Publish what's on screen, not a stale save: save first.
        const saveRes = await apiPut(`/api/role-menus/${menu.id}`, collectInputs());
        state.menu = saveRes.menu;
        const res = await apiPost(`/api/role-menus/${menu.id}/publish`, { channel_id: channelId });
        showStatus(statusEl, res.ok, res.ok ? "Published." : (res.sync.detail || res.sync.status));
        await selectMenu(menu.id);
        await refreshList();
      } catch (err) {
        showStatus(statusEl, false, err.message);
        btn.disabled = false;
      }
      return;
    }

    if (e.target.closest("[data-toggle-enabled]")) {
      const btn = e.target.closest("[data-toggle-enabled]");
      btn.disabled = true;
      try {
        await apiPut(`/api/role-menus/${menu.id}/enabled`, { enabled: !menu.enabled });
        await selectMenu(menu.id);
        await refreshList();
      } catch (err) {
        showStatus(statusEl, false, err.message);
        btn.disabled = false;
      }
      return;
    }

    if (e.target.closest("[data-delete]")) {
      if (!confirm(`Delete "${menu.title || "this menu"}"? The posted message comes down too. Grant history is kept.`)) return;
      try {
        await apiDelete(`/api/role-menus/${menu.id}`);
        state.menu = null;
        state.activeId = null;
        editorEl.innerHTML = '<div class="empty" style="padding:24px">Menu deleted.</div>';
        await refreshList();
      } catch (err) {
        alert(err.message);
      }
    }
  });

  refreshList();

  return { unmount() { clearTimeout(state.previewTimer); } };
}
