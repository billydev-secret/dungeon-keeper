import { loadConfig, loadChannels, channelSelect, channelName, apiPut, apiDelete, showStatus } from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";
import { esc } from "../api.js";

// ── Constants ────────────────────────────────────────────────────────────────

const DEFAULT_TITLE_TYPE    = "first_fifty";
const DEFAULT_DELETE_BEHAVIOR = "archive_if_empty";
const DEFAULT_REPLY_TYPE    = "default";

const TITLE_TYPES = [
  { value: "first_fifty", label: "First 50 chars (default)" },
  { value: "first_line",  label: "First line of message" },
  { value: "user_date",   label: "Username + date" },
  { value: "custom",      label: "Custom template" },
];

const DELETE_BEHAVIORS = [
  { value: "archive_if_empty", label: "Delete if empty, else archive (default)" },
  { value: "archive",          label: "Always archive" },
  { value: "delete",           label: "Always delete" },
  { value: "nothing",          label: "Do nothing" },
];

const REPLY_TYPES = [
  { value: "default", label: "Default server message" },
  { value: "custom",  label: "Custom message" },
  { value: "none",    label: "No message" },
];

const VALID_TITLE_TYPES    = new Set(TITLE_TYPES.map(t => t.value));
const VALID_DELETE_BEHAVIORS = new Set(DELETE_BEHAVIORS.map(t => t.value));
const VALID_REPLY_TYPES    = new Set(REPLY_TYPES.map(t => t.value));

// ── Helpers ──────────────────────────────────────────────────────────────────

function safeOf(set, v, fallback) {
  return set.has(v) ? v : fallback;
}

function selectOptions(choices, selected) {
  return choices.map(
    c => `<option value="${esc(c.value)}"${c.value === selected ? " selected" : ""}>${esc(c.label)}</option>`
  ).join("");
}

function checkbox(name, checked, label) {
  return `<label class="checkbox-label" style="display:flex;align-items:center;gap:8px;cursor:pointer;">
    <input type="checkbox" name="${esc(name)}"${checked ? " checked" : ""} />
    ${esc(label)}
  </label>`;
}

// ── Channel card ─────────────────────────────────────────────────────────────

function channelCard(r, channels) {
  const chName    = channelName(channels, r.channel_id);
  const titleType = safeOf(VALID_TITLE_TYPES, r.title_type, "first_fifty");
  const delBeh    = safeOf(VALID_DELETE_BEHAVIORS, r.delete_behavior, "archive_if_empty");
  const replyType = safeOf(VALID_REPLY_TYPES, r.reply_type, "default");
  const isCustomTitle = titleType === "custom";
  const isCustomReply = replyType === "custom";
  const reactionsOn   = !!r.status_reactions;

  return `
    <form class="form card" style="margin-bottom:16px;" data-needle-channel="${r.channel_id}">
      <div class="section-label">${esc(chName)}</div>

      <div class="field-row">
        <div class="field">
          <label>Thread Title</label>
          <select name="title_type">${selectOptions(TITLE_TYPES, titleType)}</select>
        </div>
        <div class="field" data-custom-title-field style="${isCustomTitle ? "" : "display:none"}">
          <label>Title Template</label>
          <input type="text" name="custom_title" value="${esc(r.custom_title)}" placeholder="$USER on $DATE" />
          <div class="field-hint">Variables: <code>$USER</code>, <code>$DATE</code></div>
        </div>
      </div>

      <div class="field-row">
        <div class="field">
          <label>When Original Deleted</label>
          <select name="delete_behavior">${selectOptions(DELETE_BEHAVIORS, delBeh)}</select>
        </div>
        <div class="field">
          <label>Slowmode (seconds)</label>
          <input type="number" name="slowmode" value="${r.slowmode}" min="0" max="21600" style="max-width:100px;" />
        </div>
      </div>

      <div class="field-row">
        <div class="field">
          <label>Welcome Message</label>
          <select name="reply_type">${selectOptions(REPLY_TYPES, replyType)}</select>
        </div>
        <div class="field" data-custom-reply-field style="${isCustomReply ? "" : "display:none"}">
          <label>Custom Welcome Text</label>
          <input type="text" name="custom_reply" value="${esc(r.custom_reply)}" placeholder="e.g. Welcome $USER! — $THREAD" />
          <div class="field-hint">Variables: <code>$USER</code>, <code>$CHANNEL</code>, <code>$THREAD</code></div>
        </div>
      </div>

      <div class="field">
        <label>Default Emoji Reactions</label>
        <input type="text" name="default_reactions" value="${esc(r.default_reactions)}" placeholder="👍,👎" />
        <div class="field-hint">Comma-separated emoji added to every new thread's starter message.</div>
      </div>

      <div class="field-row" style="flex-wrap:wrap;gap:16px;">
        ${checkbox("include_bots", r.include_bots, "Auto-thread bot messages")}
        ${checkbox("status_reactions", r.status_reactions, "Status reactions")}
        <span data-archive-immediately-wrap style="${reactionsOn ? "" : "display:none"}">
          ${checkbox("archive_immediately", r.archive_immediately, "Mark answered when non-OP replies")}
        </span>
      </div>

      <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
        <button type="submit" class="btn btn-primary">Save</button>
        <button type="button" class="btn btn-danger" data-needle-remove="${r.channel_id}">Remove</button>
        <span data-needle-status></span>
      </div>
    </form>`;
}

// ── Global settings section ───────────────────────────────────────────────────

function globalSettingsCard(needle) {
  return `
    <form class="form card" data-needle-global>
      <div class="section-label">Server-wide Defaults</div>

      <div class="field-row">
        <div class="field">
          <label>Unanswered Emoji</label>
          <input type="text" name="emoji_unanswered" value="${esc(needle.emoji_unanswered)}" style="max-width:80px;" placeholder="🔵" />
          <div class="field-hint">Added when thread is created.</div>
        </div>
        <div class="field">
          <label>Archived Emoji</label>
          <input type="text" name="emoji_archived" value="${esc(needle.emoji_archived)}" style="max-width:80px;" placeholder="✅" />
          <div class="field-hint">Shown when thread archives.</div>
        </div>
        <div class="field">
          <label>Locked Emoji</label>
          <input type="text" name="emoji_locked" value="${esc(needle.emoji_locked)}" style="max-width:80px;" placeholder="🔒" />
          <div class="field-hint">Shown when thread is locked.</div>
        </div>
      </div>

      <div class="field">
        <label>Default Welcome Message</label>
        <input type="text" name="default_reply" value="${esc(needle.default_reply)}" placeholder="Thread created by $USER in $CHANNEL" />
        <div class="field-hint">Used when a channel's welcome message is set to "Default". Variables: <code>$USER</code>, <code>$CHANNEL</code>, <code>$THREAD</code></div>
      </div>

      <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
        <button type="submit" class="btn btn-primary">Save</button>
        <span data-global-status></span>
      </div>
    </form>`;
}

// ── Mount / render ────────────────────────────────────────────────────────────

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading…</div></div>`;
  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    render(container, config.needle || { channels: [], emoji_unanswered: "🔵", emoji_archived: "✅", emoji_locked: "🔒", default_reply: "" }, channels);
  })();
}

function render(container, needle, channels) {
  const cfgs = needle.channels || [];

  const existingCards = cfgs.length
    ? cfgs.map(r => channelCard(r, channels)).join("")
    : `<div class="empty">No channels configured for auto-threading.</div>`;

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Auto-Thread</h2>
        <div class="subtitle">Automatically create a thread on every message in designated channels</div>
      </header>

      ${globalSettingsCard(needle)}

      <div class="section-label" style="margin-top:24px;">Configured Channels</div>
      <div data-needle-rules>${existingCards}</div>

      <div class="section-label" style="margin-top:16px;">Add Channel</div>
      <form class="form card" data-needle-add>
        <div class="field-row">
          <div class="field">
            <label>Channel</label>
            <select name="channel_id">${channelSelect(channels, "0", { allowNone: false })}</select>
          </div>
          <div class="field">
            <label>Thread Title</label>
            <select name="title_type">${selectOptions(TITLE_TYPES, DEFAULT_TITLE_TYPE)}</select>
          </div>
          <div class="field">
            <label>When Original Deleted</label>
            <select name="delete_behavior">${selectOptions(DELETE_BEHAVIORS, DEFAULT_DELETE_BEHAVIOR)}</select>
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Welcome Message</label>
            <select name="reply_type">${selectOptions(REPLY_TYPES, DEFAULT_REPLY_TYPE)}</select>
          </div>
          <div class="field">
            <label>Slowmode (seconds)</label>
            <input type="number" name="slowmode" value="0" min="0" max="21600" style="max-width:100px;" />
          </div>
        </div>
        <div class="field">
          <label>Default Emoji Reactions</label>
          <input type="text" name="default_reactions" value="" placeholder="👍,👎" />
          <div class="field-hint">Comma-separated emoji added to every new thread's starter message.</div>
        </div>
        <div class="field-row" style="flex-wrap:wrap;gap:16px;">
          ${checkbox("include_bots", false, "Auto-thread bot messages")}
          ${checkbox("status_reactions", false, "Status reactions")}
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
          <button type="submit" class="btn btn-primary">Add</button>
          <span data-needle-add-status></span>
        </div>
      </form>
    </div>`;

  wireGlobal(container);
  wireShowHide(container);
  wireExistingCards(container, cfgs);
  wireRemove(container, channels);
  wireAdd(container, channels);
}

// ── Wire: global settings form ────────────────────────────────────────────────

function wireGlobal(container) {
  const form = container.querySelector("[data-needle-global]");
  const status = form.querySelector("[data-global-status]");
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(form);
    try {
      await apiPut("/api/config/needle/settings", {
        emoji_unanswered: fd.get("emoji_unanswered") || "",
        emoji_archived:   fd.get("emoji_archived")   || "",
        emoji_locked:     fd.get("emoji_locked")     || "",
        default_reply:    fd.get("default_reply")    || "",
      });
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── Wire: show/hide conditional fields ───────────────────────────────────────

function wireShowHide(container) {
  container.querySelectorAll("select[name=title_type]").forEach(sel => {
    const form = sel.closest("form");
    sel.addEventListener("change", () => {
      const f = form.querySelector("[data-custom-title-field]");
      if (f) f.style.display = sel.value === "custom" ? "" : "none";
    });
  });
  container.querySelectorAll("select[name=reply_type]").forEach(sel => {
    const form = sel.closest("form");
    sel.addEventListener("change", () => {
      const f = form.querySelector("[data-custom-reply-field]");
      if (f) f.style.display = sel.value === "custom" ? "" : "none";
    });
  });
  container.querySelectorAll("input[name=status_reactions]").forEach(chk => {
    const form = chk.closest("form");
    chk.addEventListener("change", () => {
      const wrap = form.querySelector("[data-archive-immediately-wrap]");
      if (wrap) wrap.style.display = chk.checked ? "" : "none";
    });
  });
}

// ── Wire: save existing channel cards ────────────────────────────────────────

function wireExistingCards(container, cfgs) {
  for (const r of cfgs) {
    const form   = container.querySelector(`[data-needle-channel="${r.channel_id}"]`);
    if (!form) continue;
    const status = form.querySelector("[data-needle-status]");
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut(`/api/config/needle/${r.channel_id}`, buildPayload(fd));
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }
}

// ── Wire: remove buttons ──────────────────────────────────────────────────────

function wireRemove(container, channels) {
  container.querySelectorAll("[data-needle-remove]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!(await confirmDialog("Remove auto-threading from this channel?", { danger: true, confirmLabel: "Remove" }))) return;
      try {
        await apiDelete(`/api/config/needle/${btn.dataset.needleRemove}`);
        const fresh = await loadConfig();
        render(container, fresh.needle || {}, channels);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });
}

// ── Wire: add form ────────────────────────────────────────────────────────────

function wireAdd(container, channels) {
  const form      = container.querySelector("[data-needle-add]");
  const addStatus = container.querySelector("[data-needle-add-status]");
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(form);
    const channelId = fd.get("channel_id");
    if (!channelId || channelId === "0") {
      showStatus(addStatus, false, "Select a channel first.");
      return;
    }
    try {
      await apiPut(`/api/config/needle/${channelId}`, buildPayload(fd));
      const fresh = await loadConfig();
      render(container, fresh.needle || {}, channels);
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });
}

// ── Build payload from FormData ───────────────────────────────────────────────

function buildPayload(fd) {
  return {
    title_type:          fd.get("title_type")          || DEFAULT_TITLE_TYPE,
    custom_title:        fd.get("custom_title")        || "",
    include_bots:        fd.has("include_bots"),
    slowmode:            parseInt(fd.get("slowmode")   || "0", 10),
    delete_behavior:     fd.get("delete_behavior")     || DEFAULT_DELETE_BEHAVIOR,
    reply_type:          fd.get("reply_type")          || DEFAULT_REPLY_TYPE,
    custom_reply:        fd.get("custom_reply")        || "",
    status_reactions:    fd.has("status_reactions"),
    archive_immediately: fd.has("archive_immediately"),
    default_reactions:   fd.get("default_reactions")   || "",
  };
}
