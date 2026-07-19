// Announcements — queue one-shot channel posts, preview them, schedule a time.
// Admin-only. Times are guild-local ("server time"); the server converts to a
// UTC epoch via the guild's fixed tz_offset_hours. The live preview mirrors
// what the bot will post: an optional plain-text line (where pings live)
// above a Discord-style embed.
import { api, apiPost, apiPut, apiDelete, esc } from "../api.js";
import {
  channelName, loadChannels, loadRoles, mountChannelPicker, mountRolePicker,
  roleName,
} from "../config-helpers.js";
import { mdToHtml } from "../md-preview.js";
import { renderEmpty, renderLoading } from "../states.js";
import { confirmDialog, toast } from "../ui.js";

const BASE = "/api/announcements";

const BADGES = {
  draft: ["Draft", "ann-badge-draft"],
  scheduled: ["Scheduled", "ann-badge-scheduled"],
  error: ["Failed", "ann-badge-error"],
  sent: ["Sent", "ann-badge-sent"],
};

function pad2(n) { return String(n).padStart(2, "0"); }

function offsetLabel(tz) {
  if (!tz) return "UTC";
  const sign = tz > 0 ? "+" : "−";
  const abs = Math.abs(tz);
  return `UTC${sign}${Number.isInteger(abs) ? abs : abs.toFixed(1)}`;
}

// Format a UTC epoch as guild-local wall-clock (shift, then read UTC fields).
function fmtEpochLocal(epoch, tz) {
  const d = new Date((epoch + tz * 3600) * 1000);
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ` +
    `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}

function fmtSlot(item) {
  if (!item.post_date || item.post_time_min == null) return "";
  const h = Math.floor(item.post_time_min / 60);
  const m = item.post_time_min % 60;
  return `${item.post_date} ${pad2(h)}:${pad2(m)}`;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Announcements</h2>
        <div class="subtitle">Queue server announcements, preview them, and set a time — the bot posts them for you. Drafts wait until you give them a time.</div>
      </header>
      <div class="field-hint" style="margin-bottom:14px">🕒 <span data-clock></span></div>
      <section style="margin-bottom:20px">
        <div class="ticket-list-head">
          <h3>Queue</h3>
          <button class="act-btn" data-action="new">New Announcement</button>
        </div>
        <div data-queue>${renderLoading("Loading…")}</div>
      </section>
      <section data-editor-wrap style="display:none;margin-bottom:20px"></section>
      <section>
        <div class="ticket-list-head"><h3>Sent</h3></div>
        <div data-history></div>
      </section>
    </div>`;

  const queueEl = container.querySelector("[data-queue]");
  const historyEl = container.querySelector("[data-history]");
  const editorWrap = container.querySelector("[data-editor-wrap]");
  const clockEl = container.querySelector("[data-clock]");

  const state = {
    items: [], channels: [], roles: [],
    tz: 0, defaultAccent: "5865F2",
    editingId: null,          // null | "new" | numeric id
    chPicker: null, rolePicker: null, previewTimer: null,
  };

  function tickClock() {
    clockEl.textContent =
      `Times are server-local (${offsetLabel(state.tz)}). Current server time: ` +
      fmtEpochLocal(Date.now() / 1000, state.tz);
  }
  const clockTimer = setInterval(tickClock, 30_000);

  // ── lists ─────────────────────────────────────────────────────────

  function mentionBadge(item) {
    if (item.mention_kind === "everyone") return `<span class="ann-mention">@everyone</span>`;
    if (item.mention_kind === "role") {
      return `<span class="ann-mention">${esc(roleName(state.roles, item.mention_role_id))}</span>`;
    }
    return "";
  }

  function badgeHtml(status) {
    const [label, cls] = BADGES[status] || [status, ""];
    return `<span class="ann-badge ${cls}">${esc(label)}</span>`;
  }

  function rowHtml(item, actions) {
    const slot = item.status === "sent"
      ? (item.sent_at ? `posted ${fmtEpochLocal(item.sent_at, state.tz)}` : "posted")
      : item.status === "scheduled" ? `posts ${fmtSlot(item) || fmtEpochLocal(item.post_at, state.tz)}`
      : item.status === "error" ? esc(item.error || "failed")
      : "no time set";
    return `
      <div class="ann-row" data-id="${item.id}">
        <div class="ann-row-main">
          <div class="ann-row-title">${badgeHtml(item.status)} ${esc(item.title || "(untitled)")} ${mentionBadge(item)}</div>
          <div class="ann-row-sub">${esc(channelName(state.channels, item.channel_id))} · ${slot}</div>
        </div>
        <div class="ann-row-acts">${actions}</div>
      </div>`;
  }

  function renderLists() {
    const pending = state.items.filter((i) => i.status !== "sent");
    const sent = state.items.filter((i) => i.status === "sent");

    queueEl.innerHTML = pending.length
      ? pending.map((i) => rowHtml(i, `
          <button class="act-btn ghost" data-action="edit" data-id="${i.id}">Edit</button>
          <button class="act-btn ghost" data-action="post-now" data-id="${i.id}">Post now</button>
          <button class="doc-x" data-action="delete" data-id="${i.id}" title="Delete">✕</button>
        `)).join("")
      : renderEmpty("Nothing queued. Create an announcement to get started.");

    historyEl.innerHTML = sent.length
      ? sent.map((i) => rowHtml(i, `
          ${i.jump_url ? `<a class="act-btn ghost" href="${esc(i.jump_url)}" target="_blank" rel="noopener">Open in Discord</a>` : ""}
          <button class="act-btn ghost" data-action="clone" data-id="${i.id}">Clone</button>
          <button class="doc-x" data-action="delete" data-id="${i.id}" title="Delete">✕</button>
        `)).join("")
      : renderEmpty("Nothing sent yet.");
  }

  async function refresh() {
    try {
      const data = await api(BASE);
      state.items = data.items || [];
      state.tz = data.tz_offset_hours || 0;
      state.defaultAccent = data.default_accent_hex || "5865F2";
      tickClock();
      renderLists();
    } catch (err) {
      queueEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
    }
  }

  // ── editor ────────────────────────────────────────────────────────

  function field(sel) { return editorWrap.querySelector(sel); }

  function closeEditor() {
    state.editingId = null;
    state.chPicker = null;
    state.rolePicker = null;
    editorWrap.style.display = "none";
    editorWrap.innerHTML = "";
  }

  function openEditor(item) {
    state.editingId = item ? item.id : "new";
    const timeMin = item?.post_time_min;
    const timeVal = timeMin == null ? "" : `${pad2(Math.floor(timeMin / 60))}:${pad2(timeMin % 60)}`;
    editorWrap.innerHTML = `
      <div class="ticket-list-head"><h3>${item ? "Edit announcement" : "New announcement"}</h3></div>
      <div class="ann-editor">
        <div class="ann-form">
          <div class="field">
            <label>Channel</label>
            <span data-channel-slot></span>
          </div>
          <div class="field">
            <label>Embed title</label>
            <input type="text" data-f-title maxlength="256" placeholder="Big news!" value="${esc(item?.title || "")}">
          </div>
          <div class="field">
            <label>Embed body (markdown)</label>
            <textarea data-f-body rows="7" maxlength="4096" placeholder="What's happening…">${esc(item?.body || "")}</textarea>
          </div>
          <div class="field">
            <label>Image URL (optional)</label>
            <input type="text" data-f-image placeholder="https://…" value="${esc(item?.image_url || "")}">
          </div>
          <div class="field">
            <label>Accent color (hex — blank = server branding)</label>
            <input type="text" data-f-accent maxlength="7" placeholder="#${esc(state.defaultAccent)}" value="${esc(item?.accent_hex || "")}">
          </div>
          <div class="field">
            <label>Plain-text line (shown above the embed — mentions ping from here)</label>
            <input type="text" data-f-plain maxlength="300" placeholder="Heads up, adventurers!" value="${esc(item?.plain_text || "")}">
          </div>
          <div class="field">
            <label>Mention</label>
            <div style="display:flex;gap:8px;align-items:center">
              <select data-f-mention>
                <option value="none"${!item || item.mention_kind === "none" ? " selected" : ""}>No mentions</option>
                <option value="role"${item?.mention_kind === "role" ? " selected" : ""}>Ping a role</option>
                <option value="everyone"${item?.mention_kind === "everyone" ? " selected" : ""}>@everyone</option>
              </select>
              <span data-role-wrap style="${item?.mention_kind === "role" ? "" : "display:none"}"><span data-role-slot></span></span>
            </div>
          </div>
          <div class="field">
            <label>Post at (server-local — leave blank to save as a draft)</label>
            <div style="display:flex;gap:8px">
              <input type="date" data-f-date value="${esc(item?.post_date || "")}">
              <input type="time" data-f-time value="${timeVal}">
            </div>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px">
            <button class="act-btn" data-action="save">Save</button>
            <button class="act-btn ghost" data-action="cancel">Cancel</button>
          </div>
        </div>
        <div class="field">
          <label>Preview</label>
          <div class="doc-preview" data-preview></div>
        </div>
      </div>`;
    editorWrap.style.display = "";

    state.chPicker = mountChannelPicker(
      field("[data-channel-slot]"), state.channels, String(item?.channel_id || "0"),
      { emptyLabel: "(pick a channel)" },
    );
    state.rolePicker = mountRolePicker(
      field("[data-role-slot]"), state.roles, String(item?.mention_role_id || "0"),
      { emptyLabel: "(pick a role)" },
    );

    editorWrap.addEventListener("input", schedulePreview);
    field("[data-f-mention]").addEventListener("change", () => {
      field("[data-role-wrap]").style.display =
        field("[data-f-mention]").value === "role" ? "" : "none";
      schedulePreview();
    });
    renderPreviewNow();
    editorWrap.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ── live preview ──────────────────────────────────────────────────

  function renderPreviewNow() {
    const previewEl = field("[data-preview]");
    if (!previewEl) return;
    const title = field("[data-f-title]").value;
    const body = field("[data-f-body]").value;
    const image = field("[data-f-image]").value.trim();
    const accent = field("[data-f-accent]").value.trim();
    const plain = field("[data-f-plain]").value.trim();
    const kind = field("[data-f-mention]").value;

    let pill = "";
    if (kind === "everyone") pill = `<span class="ann-mention">@everyone</span> `;
    else if (kind === "role") {
      pill = `<span class="ann-mention">${esc(roleName(state.roles, state.rolePicker?.getValue()))}</span> `;
    }
    const contentLine = (pill || plain)
      ? `<div class="ann-plain">${pill}${esc(plain)}</div>` : "";

    const bar = /^#?[0-9a-fA-F]{6}$/.test(accent)
      ? (accent[0] === "#" ? accent : "#" + accent)
      : "#" + state.defaultAccent;
    const imageHtml = /^https?:/i.test(image)
      ? `<img class="dp-image" src="${esc(image)}" alt="" loading="lazy">` : "";

    previewEl.innerHTML = `
      ${contentLine}
      <div class="dp-embed" style="border-left-color:${esc(bar)}">
        ${title ? `<div class="dp-title">${esc(title)}</div>` : ""}
        ${body ? `<div class="dp-desc">${mdToHtml(body)}</div>` : ""}
        ${imageHtml}
      </div>`;
  }

  function schedulePreview() {
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(renderPreviewNow, 200);
  }

  // ── actions ───────────────────────────────────────────────────────

  async function save() {
    const channelId = state.chPicker.getValue();
    if (!channelId || channelId === "0") { toast("Pick a channel", "error"); return; }
    const title = field("[data-f-title]").value.trim();
    const body = field("[data-f-body]").value;
    if (!title && !body.trim()) { toast("Give it a title or a body", "error"); return; }
    const kind = field("[data-f-mention]").value;
    const roleId = state.rolePicker.getValue();
    if (kind === "role" && (!roleId || roleId === "0")) { toast("Pick a role to mention", "error"); return; }
    const date = field("[data-f-date]").value;
    const time = field("[data-f-time]").value;
    if (!!date !== !!time) { toast("Set both a date and a time, or neither", "error"); return; }

    const payload = {
      channel_id: channelId,
      title,
      body,
      image_url: field("[data-f-image]").value.trim() || null,
      accent_hex: field("[data-f-accent]").value.trim() || null,
      plain_text: field("[data-f-plain]").value.trim() || null,
      mention_kind: kind,
      mention_role_id: kind === "role" ? roleId : null,
      post_date: date || null,
      post_time: time || null,
    };
    try {
      const res = state.editingId === "new"
        ? await apiPost(BASE, payload)
        : await apiPut(`${BASE}/${state.editingId}`, payload);
      toast(res.status === "scheduled" ? "Scheduled ✓" : "Saved as draft ✓");
      closeEditor();
      await refresh();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function postNow(id) {
    const item = state.items.find((i) => i.id === id);
    const ok = await confirmDialog(
      `Post "${item?.title || "this announcement"}" to ${channelName(state.channels, item?.channel_id)} now? It goes out within a minute.`,
    );
    if (!ok) return;
    try {
      await apiPost(`${BASE}/${id}/post-now`);
      toast("Posting within a minute ✓");
      await refresh();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function remove(id) {
    const item = state.items.find((i) => i.id === id);
    const ok = await confirmDialog(`Delete "${item?.title || "this announcement"}"?`);
    if (!ok) return;
    try {
      await apiDelete(`${BASE}/${id}`);
      if (state.editingId === id) closeEditor();
      await refresh();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function clone(id) {
    try {
      const res = await apiPost(`${BASE}/${id}/clone`);
      await refresh();
      const fresh = state.items.find((i) => i.id === res.id);
      if (fresh) openEditor(fresh);
      toast("Cloned to a new draft ✓");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  container.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const id = btn.dataset.id ? Number(btn.dataset.id) : null;
    const action = btn.dataset.action;
    if (action === "new") openEditor(null);
    else if (action === "cancel") closeEditor();
    else if (action === "save") save();
    else if (action === "edit") openEditor(state.items.find((i) => i.id === id));
    else if (action === "post-now") postNow(id);
    else if (action === "delete") remove(id);
    else if (action === "clone") clone(id);
  });

  Promise.all([
    loadChannels().then((chs) => { state.channels = chs || []; }),
    loadRoles().then((rs) => { state.roles = rs || []; }),
  ]).then(refresh);

  return { unmount() { clearInterval(clockTimer); clearTimeout(state.previewTimer); } };
}
