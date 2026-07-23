import {
  loadConfig, loadChannels, channelName, apiPut, apiDelete, showStatus,
  guardForm, renderMetaWarning, mountChannelPicker,
} from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";
import { esc } from "../api.js";

// ── Constants ────────────────────────────────────────────────────────────────

const DEFAULT_TITLE_TYPE    = "first_fifty";
const DEFAULT_DELETE_BEHAVIOR = "archive_if_empty";
const DEFAULT_REPLY_TYPE    = "default";

const TITLE_TYPES = [
  { value: "first_fifty", label: "First 50 characters of the message (default)" },
  { value: "first_line",  label: "First line of the message" },
  { value: "user_date",   label: "Member name and date" },
  { value: "custom",      label: "A template you write" },
];

const DELETE_BEHAVIORS = [
  { value: "archive_if_empty", label: "Delete the thread if nobody replied, otherwise archive it (default)" },
  { value: "archive",          label: "Always archive the thread" },
  { value: "delete",           label: "Always delete the thread" },
  { value: "nothing",          label: "Leave the thread open" },
];

const REPLY_TYPES = [
  { value: "default", label: "The server-wide message set above" },
  { value: "custom",  label: "A message you write for this channel" },
  { value: "none",    label: "No message at all" },
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

// The one toggle idiom: a checkbox row with a hint that states what changes.
function checkbox(name, checked, label, hint) {
  return `<div class="field">
    <label class="checkbox-label" style="display:flex;align-items:center;gap:8px;cursor:pointer;">
      <input type="checkbox" name="${esc(name)}"${checked ? " checked" : ""} />
      ${esc(label)}
    </label>
    ${hint ? `<div class="field-hint">${esc(hint)}</div>` : ""}
  </div>`;
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

  const uid = `nd-${esc(String(r.channel_id))}`;

  return `
    <form class="form card" style="margin-bottom:16px;" data-needle-channel="${r.channel_id}">
      <div class="section-label">${esc(chName)}</div>

      <div class="field-row">
        <div class="field">
          <label for="${uid}-title-type">Name the Thread After</label>
          <select name="title_type" id="${uid}-title-type">${selectOptions(TITLE_TYPES, titleType)}</select>
          <div class="field-hint">Members can rename their own thread afterwards.</div>
        </div>
        <div class="field" data-custom-title-field style="${isCustomTitle ? "" : "display:none"}">
          <label for="${uid}-custom-title">Your Title Template</label>
          <input type="text" name="custom_title" id="${uid}-custom-title" value="${esc(r.custom_title)}" placeholder="$USER on $DATE" />
          <div class="field-hint">Placeholders: <code>$USER</code> is the poster's name, <code>$DATE</code> is today's date.</div>
        </div>
      </div>

      <div class="field-row">
        <div class="field">
          <label for="${uid}-delete-behavior">If the Original Message Is Deleted</label>
          <select name="delete_behavior" id="${uid}-delete-behavior">${selectOptions(DELETE_BEHAVIORS, delBeh)}</select>
          <div class="field-hint">Deleting a thread also deletes every reply in it, permanently.</div>
        </div>
        <div class="field">
          <label for="${uid}-slowmode">Slow Mode Inside Threads (seconds)</label>
          <input type="number" name="slowmode" id="${uid}-slowmode" required value="${esc(String(r.slowmode ?? 0))}" min="0" max="21600" step="1" style="max-width:120px;" />
          <div class="field-hint">Members must wait this long between replies in the thread. Enter 0 for no wait. Maximum 21600 (6 hours).</div>
        </div>
      </div>

      <div class="field-row">
        <div class="field">
          <label for="${uid}-reply-type">Message Posted in the New Thread</label>
          <select name="reply_type" id="${uid}-reply-type">${selectOptions(REPLY_TYPES, replyType)}</select>
          <div class="field-hint">A short greeting the bot posts as the thread's first reply.</div>
        </div>
        <div class="field" data-custom-reply-field style="${isCustomReply ? "" : "display:none"}">
          <label for="${uid}-custom-reply">Your Thread Message</label>
          <input type="text" name="custom_reply" id="${uid}-custom-reply" value="${esc(r.custom_reply)}" placeholder="Welcome $USER! — $THREAD" />
          <div class="field-hint">Placeholders: <code>$USER</code> the poster, <code>$CHANNEL</code> the channel, <code>$THREAD</code> the new thread.</div>
        </div>
      </div>

      <div class="field">
        <label for="${uid}-reactions">Emoji Added to Every Post</label>
        <input type="text" name="default_reactions" id="${uid}-reactions" value="${esc(r.default_reactions)}" placeholder="👍,👎" />
        <div class="field-hint">Separate emoji with commas. The bot reacts with each one on the message that started the thread — handy for quick voting. Leave blank for none.</div>
      </div>

      <div style="display:flex;flex-direction:column;gap:4px;">
        ${checkbox("include_bots", r.include_bots, "Also Thread Messages From Bots",
          "Unchecked, messages posted by bots and webhooks are left alone.")}
        ${checkbox("status_reactions", r.status_reactions, "Show Answered / Unanswered Emoji",
          "The bot marks each thread with the emoji set under Server-Wide Defaults so members can see at a glance what still needs an answer.")}
        <span data-archive-immediately-wrap style="${reactionsOn ? "" : "display:none"}">
          ${checkbox("archive_immediately", r.archive_immediately, "Mark Answered as Soon as Someone Else Replies",
            "The first reply from anyone other than the person who started the thread flips it to answered and archives it.")}
        </span>
      </div>

      <div style="display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap;">
        <button type="submit" class="btn btn-primary">Save</button>
        <button type="button" class="btn btn-danger" data-needle-remove="${r.channel_id}">Stop Auto-Threading</button>
        <span data-needle-status></span>
      </div>
    </form>`;
}

// ── Global settings section ───────────────────────────────────────────────────

function globalSettingsCard(needle) {
  return `
    <form class="form card" data-needle-global>
      <div class="section-label">Server-Wide Defaults</div>
      <div class="field-hint" style="margin-bottom:10px;">These apply to every auto-threaded channel below.</div>

      <div class="field-row">
        <div class="field">
          <label for="nd-emoji-unanswered">Waiting for an Answer</label>
          <input type="text" name="emoji_unanswered" id="nd-emoji-unanswered" value="${esc(needle.emoji_unanswered)}" style="max-width:80px;" placeholder="🔵" />
          <div class="field-hint">Added to a thread the moment it's created.</div>
        </div>
        <div class="field">
          <label for="nd-emoji-archived">Answered or Archived</label>
          <input type="text" name="emoji_archived" id="nd-emoji-archived" value="${esc(needle.emoji_archived)}" style="max-width:80px;" placeholder="✅" />
          <div class="field-hint">Replaces the waiting emoji once the thread is archived.</div>
        </div>
        <div class="field">
          <label for="nd-emoji-locked">Locked</label>
          <input type="text" name="emoji_locked" id="nd-emoji-locked" value="${esc(needle.emoji_locked)}" style="max-width:80px;" placeholder="🔒" />
          <div class="field-hint">Shown when a moderator locks the thread so nobody can reply.</div>
        </div>
      </div>

      <div class="field">
        <label for="nd-default-reply">Default Thread Message</label>
        <input type="text" name="default_reply" id="nd-default-reply" value="${esc(needle.default_reply)}" placeholder="Thread created by $USER in $CHANNEL" />
        <div class="field-hint">Posted in any channel whose thread message is set to "The server-wide message set above". Placeholders: <code>$USER</code> the poster, <code>$CHANNEL</code> the channel, <code>$THREAD</code> the new thread.</div>
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
    : `<div class="empty">No channels are auto-threaded yet. Add your first one below.</div>`;

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Auto-Thread</h2>
        <div class="subtitle">Give every message in a channel its own thread, so replies stay tidy</div>
      </header>
      ${renderMetaWarning()}

      ${globalSettingsCard(needle)}

      <div class="section-label" style="margin-top:24px;">Auto-Threaded Channels</div>
      <div data-needle-rules>${existingCards}</div>

      <div class="section-label" style="margin-top:16px;">Add a Channel</div>
      <form class="form card" data-needle-add>
        <div class="field">
          <label>Channel</label>
          <span data-picker="channel_id"></span>
          <div class="field-hint">Every message posted here gets its own thread from now on. Older messages are left alone.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="nd-new-title-type">Name the Thread After</label>
            <select name="title_type" id="nd-new-title-type">${selectOptions(TITLE_TYPES, DEFAULT_TITLE_TYPE)}</select>
          </div>
          <div class="field">
            <label for="nd-new-delete-behavior">If the Original Message Is Deleted</label>
            <select name="delete_behavior" id="nd-new-delete-behavior">${selectOptions(DELETE_BEHAVIORS, DEFAULT_DELETE_BEHAVIOR)}</select>
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="nd-new-reply-type">Message Posted in the New Thread</label>
            <select name="reply_type" id="nd-new-reply-type">${selectOptions(REPLY_TYPES, DEFAULT_REPLY_TYPE)}</select>
          </div>
          <div class="field">
            <label for="nd-new-slowmode">Slow Mode Inside Threads (seconds)</label>
            <input type="number" name="slowmode" id="nd-new-slowmode" required value="0" min="0" max="21600" step="1" style="max-width:120px;" />
            <div class="field-hint">Enter 0 for no wait. Maximum 21600 (6 hours).</div>
          </div>
        </div>
        <div class="field">
          <label for="nd-new-reactions">Emoji Added to Every Post</label>
          <input type="text" name="default_reactions" id="nd-new-reactions" value="" placeholder="👍,👎" />
          <div class="field-hint">Separate emoji with commas. Leave blank for none.</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;">
          ${checkbox("include_bots", false, "Also Thread Messages From Bots",
            "Unchecked, messages posted by bots and webhooks are left alone.")}
          ${checkbox("status_reactions", false, "Show Answered / Unanswered Emoji",
            "The bot marks each thread with the emoji set under Server-Wide Defaults.")}
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
          <button type="submit" class="btn btn-primary">Add Channel</button>
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
  guardForm(form);
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
    guardForm(form);
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const fd = new FormData(form);
      const payload = buildPayload(fd, status, form);
      if (!payload) return;
      try {
        await apiPut(`/api/config/needle/${r.channel_id}`, payload);
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
      const name = channelName(channels, btn.dataset.needleRemove);
      const ok = await confirmDialog(
        `Stop creating threads for new messages in ${name}? Threads that already exist are left alone.`,
        { title: "Stop Auto-Threading", danger: true, confirmLabel: "Stop Auto-Threading" },
      );
      if (!ok) return;
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
  // Searchable picker replaces the old plain <select> (W-C4). Snowflakes stay
  // strings and "0" remains the unset sentinel.
  const picker = mountChannelPicker(
    form.querySelector('[data-picker="channel_id"]'), channels, "0",
    { emptyValue: "0", emptyLabel: "(pick a channel)", label: "Channel" },
  );
  guardForm(form);
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(form);
    const channelId = picker.getValue() || "0";
    if (channelId === "0") {
      showStatus(addStatus, false, "Pick a channel first.");
      return;
    }
    const payload = buildPayload(fd, addStatus, form);
    if (!payload) return;
    try {
      await apiPut(`/api/config/needle/${channelId}`, payload);
      const fresh = await loadConfig();
      render(container, fresh.needle || {}, channels);
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });
}

// ── Build payload from FormData ───────────────────────────────────────────────

function buildPayload(fd, statusEl, form) {
  // Validate the one numeric field before posting, naming it (W-C5).
  const rawSlow = String(fd.get("slowmode") ?? "").trim();
  const slowmode = rawSlow === "" ? 0 : parseInt(rawSlow, 10);
  if (!Number.isFinite(slowmode) || slowmode < 0 || slowmode > 21600) {
    showStatus(statusEl, false, "Slow Mode Inside Threads must be a whole number of seconds between 0 and 21600.");
    const el = form.querySelector('[name="slowmode"]');
    if (el) el.focus();
    return null;
  }
  return {
    title_type:          fd.get("title_type")          || DEFAULT_TITLE_TYPE,
    custom_title:        fd.get("custom_title")        || "",
    include_bots:        fd.has("include_bots"),
    slowmode,
    delete_behavior:     fd.get("delete_behavior")     || DEFAULT_DELETE_BEHAVIOR,
    reply_type:          fd.get("reply_type")          || DEFAULT_REPLY_TYPE,
    custom_reply:        fd.get("custom_reply")        || "",
    status_reactions:    fd.has("status_reactions"),
    archive_immediately: fd.has("archive_immediately"),
    default_reactions:   fd.get("default_reactions")   || "",
  };
}
