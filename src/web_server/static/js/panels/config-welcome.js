import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, showStatus } from "../config-helpers.js";
import { api, esc } from "../api.js";

// Template placeholders accepted by the welcome / leave message
// templates. Order is the order they appear in the chip strip.
const PLACEHOLDERS = [
  { token: "{member}",          description: "@-mention of the member" },
  { token: "{member_name}",     description: "The member's display name" },
  { token: "{member_id}",       description: "Numeric Discord user ID" },
  { token: "{server}",          description: "Server name" },
  { token: "{member_count}",    description: "Current member count" },
  { token: "{bios_channel}",    description: "Clickable #mention of the bios channel" },
  { token: "{bio_link}",        description: "Direct jump URL to the bios trigger button" },
  {
    token: "{member_bio_link}",
    description: "Jump URL to this member's own bio post. Empty for new members. For returning members whose bio was archived, the bot resurrects it automatically.",
  },
  {
    token: "{server_guide}",
    description: "Clickable #mention of the configured server-guide channel. Empty if no server-guide channel is set below.",
  },
];

function chipsHtml(targetName) {
  const chipStyle = "cursor:pointer; border:1px solid var(--rule, #444); background:var(--rule-soft, rgba(255,255,255,0.05)); font-family:var(--font-mono, monospace); font-size:12px; padding:3px 10px; border-radius:999px;";
  const chips = PLACEHOLDERS.map((p) => `
    <button type="button" class="chip placeholder-chip" data-insert="${esc(p.token)}" data-target="${esc(targetName)}" title="${esc(p.description)}" style="${chipStyle}">
      ${esc(p.token)}
    </button>
  `).join("");
  return `
    <div class="placeholder-strip" style="display:flex; flex-wrap:wrap; gap:6px; margin-top:6px;">
      ${chips}
    </div>
  `;
}

function legendHtml() {
  const rows = PLACEHOLDERS.map((p) => `
    <tr>
      <td><code>${esc(p.token)}</code></td>
      <td>${esc(p.description)}</td>
    </tr>
  `).join("");
  return `
    <details style="margin-top:8px;">
      <summary style="cursor:pointer;">Show placeholder reference</summary>
      <table class="table" style="margin-top:8px;">
        <thead><tr><th>Placeholder</th><th>What it inserts</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </details>
  `;
}

function insertAtCursor(textarea, snippet) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  const before = textarea.value.slice(0, start);
  const after = textarea.value.slice(end);
  textarea.value = `${before}${snippet}${after}`;
  const caret = start + snippet.length;
  textarea.focus();
  textarea.setSelectionRange(caret, caret);
  // Trigger an input event so any listeners (e.g. preview hooks) update.
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const w = config.welcome;

    const trigger = w.welcome_trigger || "join";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Welcome & Leave</h2>
          <div class="subtitle">Welcome/leave channels, messages, greeter settings</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Welcome Channel</label>
            <select name="welcome_channel_id">${channelSelect(channels, w.welcome_channel_id)}</select>
          </div>
          <div class="field">
            <label>Welcome Trigger</label>
            <select name="welcome_trigger">
              <option value="join"${trigger === "join" ? " selected" : ""}>At join</option>
              <option value="verified"${trigger === "verified" ? " selected" : ""}>After verification (unverified role removed)</option>
            </select>
            <div class="field-hint">When to send the welcome message. "After verification" fires when the unverified role below is removed — e.g. once DoubleCounter finishes its scan and lifts the gate. No bio is required.</div>
          </div>
          <div class="field" data-verified-only style="${trigger === "verified" ? "" : "display:none"}">
            <label>Unverified Role</label>
            <select name="unverified_role_id">${roleSelect(roles, w.unverified_role_id)}</select>
            <div class="field-hint">Role that is removed by your verification process (e.g. DoubleCounter). The welcome fires the moment this role is stripped.</div>
          </div>
          <div class="field">
            <label>Welcome Message</label>
            <textarea name="welcome_message" data-template>${esc(w.welcome_message || "")}</textarea>
            <div class="field-hint">Tap a placeholder below to insert it at the cursor:</div>
            ${chipsHtml("welcome_message")}
            ${legendHtml()}
          </div>
          <div class="field">
            <label>Welcome Ping Role</label>
            <select name="welcome_ping_role_id">${roleSelect(roles, w.welcome_ping_role_id)}</select>
          </div>
          <div class="field">
            <label>Leave Channel</label>
            <select name="leave_channel_id">${channelSelect(channels, w.leave_channel_id)}</select>
          </div>
          <div class="field">
            <label>Leave Message</label>
            <textarea name="leave_message" data-template>${esc(w.leave_message || "")}</textarea>
            <div class="field-hint">Tap a placeholder below to insert it at the cursor:</div>
            ${chipsHtml("leave_message")}
          </div>
          <div class="field">
            <label>Greeter Role</label>
            <select name="greeter_role_id">${roleSelect(roles, w.greeter_role_id)}</select>
          </div>
          <div class="field">
            <label>Greeter Chat Channel</label>
            <select name="greeter_chat_channel_id">${channelSelect(channels, w.greeter_chat_channel_id)}</select>
          </div>
          <div class="field">
            <label>Server Guide Channel</label>
            <select name="server_guide_channel_id">${channelSelect(channels, w.server_guide_channel_id)}</select>
            <div class="field-hint">Optional — the channel that introduces newcomers to your server. When set, the {server_guide} placeholder above resolves to a clickable #mention of this channel.</div>
          </div>
          <div class="field">
            <label>Join / Leave Log Channel</label>
            <select name="join_leave_log_channel_id">${channelSelect(channels, w.join_leave_log_channel_id)}</select>
            <div class="field-hint">Used by the Greeter Response report to time joins, greetings, and early departures.</div>
          </div>
          <div>
            <button type="submit" class="btn btn-primary">Save</button>
            <button type="button" class="btn" data-action="preview">Preview</button>
            <span data-status></span>
          </div>
        </form>
        <div data-preview-wrap style="margin-top:16px;"></div>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const previewWrap = container.querySelector("[data-preview-wrap]");
    const previewBtn = container.querySelector('[data-action="preview"]');

    // Show/hide the unverified role selector based on trigger selection.
    const triggerSelect = form.querySelector("select[name='welcome_trigger']");
    const verifiedOnlyFields = form.querySelectorAll("[data-verified-only]");
    function syncVerifiedFields() {
      const show = triggerSelect.value === "verified";
      verifiedOnlyFields.forEach((el) => {
        el.style.display = show ? "" : "none";
      });
    }
    triggerSelect.addEventListener("change", syncVerifiedFields);

    // Wire placeholder chips → insert at cursor of the right textarea.
    container.querySelectorAll(".placeholder-strip .chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        const targetName = btn.dataset.target;
        const snippet = btn.dataset.insert;
        const textarea = form.querySelector(`textarea[name="${targetName}"]`);
        if (textarea) insertAtCursor(textarea, snippet);
      });
    });

    function renderEmbed(label, embed) {
      const colorHex = embed.color != null ? `#${embed.color.toString(16).padStart(6, "0")}` : "#5865F2";
      const thumb = embed.thumbnail_url ? `<img src="${esc(embed.thumbnail_url)}" alt="" style="width:64px; height:64px; border-radius:8px; float:right; margin-left:12px;" />` : "";
      return `
        <div style="border-left:4px solid ${colorHex}; background:rgba(255,255,255,0.03); padding:12px 16px; margin-bottom:12px; border-radius:4px;">
          <div class="subtitle" style="margin-bottom:6px;">${label}</div>
          ${thumb}
          ${embed.title ? `<div style="font-weight:bold; margin-bottom:4px;">${esc(embed.title)}</div>` : ""}
          <div style="white-space:pre-wrap;">${esc(embed.description || "")}</div>
          ${embed.footer ? `<div class="subtitle" style="margin-top:8px; font-size:0.85em;">${esc(embed.footer)}</div>` : ""}
        </div>
      `;
    }

    previewBtn.addEventListener("click", async () => {
      previewWrap.textContent = "Rendering preview…";
      try {
        const data = await api("/api/config/welcome/preview", {});
        previewWrap.innerHTML =
          `<div class="subtitle">Sample member: ${esc(data.sample_user_name || "(you)")}</div>` +
          renderEmbed("Welcome embed", data.welcome) +
          renderEmbed("Leave embed", data.leave);
      } catch (err) {
        previewWrap.textContent = `Error: ${err.message}`;
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/welcome", {
          welcome_channel_id: fd.get("welcome_channel_id"),
          welcome_message: fd.get("welcome_message"),
          welcome_ping_role_id: fd.get("welcome_ping_role_id"),
          welcome_trigger: fd.get("welcome_trigger"),
          unverified_role_id: fd.get("unverified_role_id"),
          leave_channel_id: fd.get("leave_channel_id"),
          leave_message: fd.get("leave_message"),
          greeter_role_id: fd.get("greeter_role_id"),
          greeter_chat_channel_id: fd.get("greeter_chat_channel_id"),
          server_guide_channel_id: fd.get("server_guide_channel_id"),
          join_leave_log_channel_id: fd.get("join_leave_log_channel_id"),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
