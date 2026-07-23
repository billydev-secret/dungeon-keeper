// Announcements — queue one-shot channel posts, preview them, schedule a time.
// Admin-only. Times are guild-local ("server time"); the server converts to a
// UTC epoch via the guild's fixed tz_offset_hours. The live preview mirrors
// what the bot will post: an optional plain-text line (where pings live)
// above a Discord-style embed.
import { api, apiPost, apiPut, apiDelete, esc } from "../api.js";
import {
  channelName, guardForm, loadChannels, loadRoles, mountChannelPicker,
  mountRolePicker, roleName,
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
        <div class="subtitle">Write an announcement, see exactly how it will look, and
          give it a time — Dungeon Keeper posts it for you. Anything without a time
          stays a draft until you set one.</div>
      </header>
      <div class="field-hint" style="margin-bottom:14px">🕒 <span data-clock></span></div>
      <section style="margin-bottom:20px">
        <div class="ticket-list-head">
          <h3>Queue</h3>
          <button class="act-btn" data-action="new">New Announcement</button>
        </div>
        <div data-queue>${renderLoading("Loading announcements…")}</div>
      </section>
      <section data-editor-wrap style="display:none;margin-bottom:20px"></section>
      <section>
        <div class="ticket-list-head"><h3>Already Posted</h3></div>
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
    buttons: [], buttonPickers: [], maxButtons: 5,
    busy: false,             // a mutating request is in flight
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
      ? (item.sent_at ? `Posted ${fmtEpochLocal(item.sent_at, state.tz)}` : "Posted")
      : item.status === "scheduled" ? `Posts ${fmtSlot(item) || fmtEpochLocal(item.post_at, state.tz)}`
      : item.status === "error" ? esc(item.error || "Posting failed")
      : "No time set yet";
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
          <button class="act-btn ghost" data-action="post-now" data-id="${i.id}">Post Now</button>
          <button class="doc-x" data-action="delete" data-id="${i.id}" title="Delete">✕</button>
        `)).join("")
      : renderEmpty("Nothing queued yet. Press New Announcement to write one.");

    historyEl.innerHTML = sent.length
      ? sent.map((i) => rowHtml(i, `
          ${i.jump_url ? `<a class="act-btn ghost" href="${esc(i.jump_url)}" target="_blank" rel="noopener">Open in Discord</a>` : ""}
          <button class="act-btn ghost" data-action="clone" data-id="${i.id}">Copy</button>
          <button class="doc-x" data-action="delete" data-id="${i.id}" title="Delete">✕</button>
        `)).join("")
      : renderEmpty("No announcements have been posted yet.");
  }

  async function refresh() {
    try {
      const data = await api(BASE);
      state.items = data.items || [];
      state.tz = data.tz_offset_hours || 0;
      state.defaultAccent = data.default_accent_hex || "5865F2";
      state.maxButtons = data.max_buttons || 5;
      tickClock();
      renderLists();
    } catch (err) {
      queueEl.innerHTML = `<div class="error" style="padding:20px">The announcement queue failed to load: ${esc(err.message)}</div>`;
    }
  }

  // ── editor ────────────────────────────────────────────────────────

  function field(sel) { return editorWrap.querySelector(sel); }

  function closeEditor() {
    // Nothing is left on screen to lose once the editor is gone.
    window.__dkDirtyReset?.();
    state.editingId = null;
    state.chPicker = null;
    state.rolePicker = null;
    state.buttons = [];
    state.buttonPickers = [];
    editorWrap.style.display = "none";
    editorWrap.innerHTML = "";
  }

  function openEditor(item) {
    state.editingId = item ? item.id : "new";
    const timeMin = item?.post_time_min;
    const timeVal = timeMin == null ? "" : `${pad2(Math.floor(timeMin / 60))}:${pad2(timeMin % 60)}`;
    editorWrap.innerHTML = `
      <div class="ticket-list-head"><h3>${item ? "Edit Announcement" : "New Announcement"}</h3></div>
      <div class="ann-editor">
        <div class="ann-form">
          <div class="field">
            <label>Channel</label>
            <span data-channel-slot></span>
            <div class="field-hint">Where the announcement is posted. Dungeon Keeper
              needs permission to post there.</div>
          </div>
          <div class="field">
            <label for="ann-title">Title</label>
            <input type="text" id="ann-title" data-f-title maxlength="256" placeholder="Big news!" value="${esc(item?.title || "")}">
            <div class="field-hint">The bold heading at the top of the card.</div>
          </div>
          <div class="field">
            <label for="ann-body">Message</label>
            <textarea id="ann-body" data-f-body rows="7" maxlength="4096" placeholder="What's happening…">${esc(item?.body || "")}</textarea>
            <div class="field-hint">Discord formatting works here — **bold**, *italic*,
              bullet lists, and links. The preview on the right updates as you type.</div>
          </div>
          <div class="field">
            <label for="ann-image">Image Address (optional)</label>
            <input type="text" id="ann-image" data-f-image placeholder="https://example.com/banner.png" value="${esc(item?.image_url || "")}">
            <div class="field-hint">A full web address starting with https:// pointing at
              an image, shown under the message. If the address stops working, the card
              is posted without a picture.</div>
          </div>
          <div class="field">
            <label for="ann-accent">Accent Color (optional)</label>
            <input type="text" id="ann-accent" data-f-accent maxlength="7" placeholder="#${esc(state.defaultAccent)}" value="${esc(item?.accent_hex || "")}">
            <div class="field-hint">The colored stripe down the left of the card, as a
              six-digit hex code such as #5865F2. Leave it empty to use your server's
              branding color.</div>
          </div>
          <div class="field">
            <label for="ann-plain">Line Above the Card (optional)</label>
            <input type="text" id="ann-plain" data-f-plain maxlength="300" placeholder="Heads up, adventurers!" value="${esc(item?.plain_text || "")}">
            <div class="field-hint">Plain text posted above the card. Any mention you
              choose below appears on this line, and this is the only part that actually
              notifies people.</div>
          </div>
          <div class="field">
            <label for="ann-mention">Who to Notify</label>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
              <select id="ann-mention" data-f-mention>
                <option value="none"${!item || item.mention_kind === "none" ? " selected" : ""}>Nobody</option>
                <option value="role"${item?.mention_kind === "role" ? " selected" : ""}>One role</option>
                <option value="everyone"${item?.mention_kind === "everyone" ? " selected" : ""}>Everyone in the server</option>
              </select>
              <span data-role-wrap style="${item?.mention_kind === "role" ? "" : "display:none"}"><span data-role-slot></span></span>
            </div>
            <div class="field-hint">"Everyone in the server" pings every single member,
              including people who are asleep. Use it sparingly.</div>
          </div>
          <div class="field">
            <label>Role Buttons (optional)</label>
            <div data-buttons-list></div>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
              <button class="act-btn ghost" data-action="add-button" type="button">Add Role Button</button>
              <span class="field-hint" data-buttons-hint></span>
            </div>
            <div class="field-hint">Members press a button to give themselves that role,
              and press it again to take it off. Nothing else happens.</div>
          </div>
          <div class="field">
            <label for="ann-date">When to Post</label>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              <input type="date" id="ann-date" data-f-date value="${esc(item?.post_date || "")}" aria-label="Post date">
              <input type="time" data-f-time value="${timeVal}" aria-label="Post time">
            </div>
            <div class="field-hint">Given in server time, shown at the top of this page.
              Leave both empty to keep this as a draft that never posts on its own.</div>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px">
            <button class="act-btn" data-action="save">Save</button>
            <button class="act-btn ghost" data-action="cancel">Cancel</button>
          </div>
        </div>
        <div class="field">
          <label>Preview</label>
          <div class="doc-preview" data-preview></div>
          <div class="field-hint">How the announcement will look in Discord.</div>
        </div>
      </div>`;
    editorWrap.style.display = "";

    state.chPicker = mountChannelPicker(
      field("[data-channel-slot]"), state.channels, String(item?.channel_id || "0"),
      { emptyLabel: "(pick a channel)", label: "Channel" },
    );
    state.rolePicker = mountRolePicker(
      field("[data-role-slot]"), state.roles, String(item?.mention_role_id || "0"),
      { emptyLabel: "(pick a role)", label: "Role to Notify" },
    );

    // Unsaved-edit protection: the editor is a div, not a <form>, and reports
    // through toast() rather than showStatus(), so the flag is cleared by hand
    // whenever the editor closes cleanly.
    guardForm(editorWrap);

    // Buttons are edited through `state.buttons`, not read back off the DOM —
    // adding or removing a row re-mounts every role picker, which would drop
    // unsaved text otherwise.
    state.buttons = (item?.buttons || []).map((b) => ({
      role_id: String(b.role_id || "0"),
      label: b.label || "",
      emoji: b.emoji || "",
      style: b.style || "primary",
    }));
    renderButtonRows();

    editorWrap.addEventListener("input", (ev) => {
      syncButtonField(ev.target);
      schedulePreview();
    });
    field("[data-f-mention]").addEventListener("change", () => {
      field("[data-role-wrap]").style.display =
        field("[data-f-mention]").value === "role" ? "" : "none";
      schedulePreview();
    });
    renderPreviewNow();
    editorWrap.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ── role buttons ──────────────────────────────────────────────────

  // Re-render the whole list; each row's role picker is a fresh mount, so its
  // value is read back into state.buttons before the rebuild (see addButton).
  function renderButtonRows() {
    const listEl = field("[data-buttons-list]");
    if (!listEl) return;
    listEl.innerHTML = state.buttons.map((b, i) => `
      <div class="ann-btn-row" data-b-index="${i}">
        <span data-b-role-slot></span>
        <input type="text" data-b-label maxlength="80" aria-label="Button text (leave empty to use the role's name)"
               placeholder="Button text (blank = role name)" value="${esc(b.label)}">
        <input type="text" data-b-emoji maxlength="64" aria-label="Button emoji" placeholder="🔔" value="${esc(b.emoji)}">
        <select data-b-style aria-label="Button color">
          <option value="primary"${b.style === "primary" ? " selected" : ""}>Blue</option>
          <option value="secondary"${b.style === "secondary" ? " selected" : ""}>Grey</option>
          <option value="success"${b.style === "success" ? " selected" : ""}>Green</option>
        </select>
        <button class="doc-x" data-action="remove-button" data-index="${i}" title="Remove" type="button">✕</button>
      </div>`).join("");

    state.buttonPickers = Array.from(listEl.querySelectorAll("[data-b-role-slot]"))
      .map((slot, i) => mountRolePicker(slot, state.roles, state.buttons[i].role_id,
        { emptyLabel: "(pick a role)", label: `Role for button ${i + 1}` }));

    const hint = field("[data-buttons-hint]");
    const addBtn = editorWrap.querySelector('[data-action="add-button"]');
    const full = state.buttons.length >= state.maxButtons;
    if (addBtn) addBtn.disabled = full;
    if (hint) {
      hint.textContent = full
        ? `That is the limit — only ${state.maxButtons} buttons fit on one row.`
        : "Roles that carry moderator powers cannot be handed out by a button.";
    }
    schedulePreview();
  }

  // Pull the current role-picker values back into state before any rebuild.
  function captureButtonRoles() {
    state.buttonPickers.forEach((p, i) => {
      if (state.buttons[i]) state.buttons[i].role_id = p.getValue() || "0";
    });
  }

  // Mirror a label/emoji/style edit into state (the role pickers self-report).
  function syncButtonField(target) {
    const row = target?.closest?.("[data-b-index]");
    if (!row) return;
    const b = state.buttons[Number(row.dataset.bIndex)];
    if (!b) return;
    if (target.matches("[data-b-label]")) b.label = target.value;
    else if (target.matches("[data-b-emoji]")) b.emoji = target.value;
    else if (target.matches("[data-b-style]")) b.style = target.value;
  }

  function addButton() {
    if (state.buttons.length >= state.maxButtons) return;
    captureButtonRoles();
    state.buttons.push({ role_id: "0", label: "", emoji: "", style: "primary" });
    renderButtonRows();
  }

  function removeButton(index) {
    captureButtonRoles();
    state.buttons.splice(index, 1);
    renderButtonRows();
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

    // Button pills sit below the embed, where Discord renders components.
    // Labels mirror the bot's fallback: blank label → the role's own name.
    const pills = state.buttons.map((b, i) => {
      const roleId = state.buttonPickers[i]?.getValue() || b.role_id;
      const label = b.label.trim() || (roleId && roleId !== "0"
        ? roleName(state.roles, roleId) : "Get role");
      const style = ["primary", "secondary", "success"].includes(b.style) ? b.style : "primary";
      return `<span class="ann-btn-pill ${style}">${esc(b.emoji)} ${esc(label)}</span>`;
    }).join("");

    previewEl.innerHTML = `
      ${contentLine}
      <div class="dp-embed" style="border-left-color:${esc(bar)}">
        ${title ? `<div class="dp-title">${esc(title)}</div>` : ""}
        ${body ? `<div class="dp-desc">${mdToHtml(body)}</div>` : ""}
        ${imageHtml}
      </div>
      ${pills ? `<div class="ann-btns-preview">${pills}</div>` : ""}`;
  }

  function schedulePreview() {
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(renderPreviewNow, 200);
  }

  // ── actions ───────────────────────────────────────────────────────

  async function save() {
    const channelId = state.chPicker.getValue();
    if (!channelId || channelId === "0") { toast("Choose a Channel to post in", "error"); return; }
    const title = field("[data-f-title]").value.trim();
    const body = field("[data-f-body]").value;
    if (!title && !body.trim()) { toast("Write a Title or a Message — an empty announcement cannot be posted", "error"); return; }
    const accent = field("[data-f-accent]").value.trim();
    if (accent && !/^#?[0-9a-fA-F]{6}$/.test(accent)) {
      toast("Accent Color must be a six-digit hex code, such as #5865F2", "error");
      field("[data-f-accent]").focus();
      return;
    }
    const image = field("[data-f-image]").value.trim();
    if (image && !/^https?:\/\//i.test(image)) {
      toast("Image Address must be a full web address starting with https://", "error");
      field("[data-f-image]").focus();
      return;
    }
    const kind = field("[data-f-mention]").value;
    const roleId = state.rolePicker.getValue();
    if (kind === "role" && (!roleId || roleId === "0")) { toast("Choose which role to notify", "error"); return; }
    const date = field("[data-f-date]").value;
    const time = field("[data-f-time]").value;
    if (!!date !== !!time) { toast("Set both a date and a time, or leave both empty to save a draft", "error"); return; }

    captureButtonRoles();
    if (state.buttons.some((b) => !b.role_id || b.role_id === "0")) {
      toast("Every role button needs a role — pick one, or remove the empty button", "error");
      return;
    }
    const roleIds = state.buttons.map((b) => b.role_id);
    if (new Set(roleIds).size !== roleIds.length) {
      toast("Two buttons cannot hand out the same role", "error");
      return;
    }

    const payload = {
      channel_id: channelId,
      title,
      body,
      image_url: image || null,
      accent_hex: accent || null,
      plain_text: field("[data-f-plain]").value.trim() || null,
      mention_kind: kind,
      mention_role_id: kind === "role" ? roleId : null,
      post_date: date || null,
      post_time: time || null,
      buttons: state.buttons.map((b) => ({
        role_id: b.role_id,
        label: b.label.trim(),
        emoji: b.emoji.trim(),
        style: b.style,
      })),
    };
    try {
      const res = state.editingId === "new"
        ? await apiPost(BASE, payload)
        : await apiPut(`${BASE}/${state.editingId}`, payload);
      toast(res.status === "scheduled" ? "Scheduled" : "Saved as a draft");
      closeEditor();
      await refresh();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function postNow(id) {
    const item = state.items.find((i) => i.id === id);
    const ok = await confirmDialog(
      `"${item?.title || "This announcement"}" goes out in ${channelName(state.channels, item?.channel_id)} `
      + "within the next minute, notifying whoever you chose. It cannot be recalled once posted.",
      { title: "Post this now?", confirmLabel: "Post Now" },
    );
    if (!ok) return;
    try {
      await apiPost(`${BASE}/${id}/post-now`);
      toast("Posting within the next minute");
      await refresh();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function remove(id) {
    const item = state.items.find((i) => i.id === id);
    const posted = item?.status === "sent";
    const ok = await confirmDialog(
      `"${item?.title || "This announcement"}" is removed from this page for good. `
      + (posted
        ? "The message already posted in Discord stays where it is — delete it there if you want it gone."
        : "It will never be posted."),
      { title: "Delete this announcement?", danger: true, confirmLabel: "Delete" },
    );
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
      toast("Copied into a new draft");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // Every action that writes goes through one in-flight guard: a fast double
  // click would otherwise fire two POSTs and create two announcements.
  const MUTATING = {
    "save": () => save(),
    "post-now": (id) => postNow(id),
    "delete": (id) => remove(id),
    "clone": (id) => clone(id),
  };

  container.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const id = btn.dataset.id ? Number(btn.dataset.id) : null;
    const action = btn.dataset.action;
    const mutate = Object.prototype.hasOwnProperty.call(MUTATING, action)
      ? MUTATING[action] : null;
    if (mutate) {
      if (state.busy) return;
      state.busy = true;
      btn.disabled = true;
      try {
        await mutate(id);
      } finally {
        state.busy = false;
        // A refresh re-renders the list, so the button may be gone by now.
        if (btn.isConnected) btn.disabled = false;
      }
      return;
    }
    if (action === "new") openEditor(null);
    else if (action === "cancel") closeEditor();
    else if (action === "edit") openEditor(state.items.find((i) => i.id === id));
    else if (action === "add-button") addButton();
    else if (action === "remove-button") removeButton(Number(btn.dataset.index));
  });

  Promise.all([
    loadChannels().then((chs) => { state.channels = chs || []; }),
    loadRoles().then((rs) => { state.roles = rs || []; }),
  ]).then(refresh);

  return { unmount() { clearInterval(clockTimer); clearTimeout(state.previewTimer); } };
}
