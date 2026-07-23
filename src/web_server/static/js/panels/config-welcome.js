import {
  loadConfig,
  loadChannels,
  loadRoles,
  apiPut,
  showStatus,
  mountChannelPicker,
  mountRolePicker,
  guardForm,
} from "../config-helpers.js";
import { apiPost, esc } from "../api.js";

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
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const w = config.welcome;

    const trigger = w.welcome_trigger || "join";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Welcome & Leave</h2>
          <div class="subtitle">Greet new members, say goodbye to leavers, and point your greeter team at the door</div>
        </header>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Welcome Message</div>
            <div class="field">
              <label>Welcome Channel</label>
              <span data-picker="welcome_channel_id"></span>
              <div class="field-hint">Where the welcome embed is posted. "(disabled)" turns welcomes off.</div>
            </div>
            <div class="field">
              <label for="cw-welcome-trigger">Welcome Trigger</label>
              <select name="welcome_trigger" id="cw-welcome-trigger">
                <option value="join"${trigger === "join" ? " selected" : ""}>At join</option>
                <option value="verified"${trigger === "verified" ? " selected" : ""}>After verification (unverified role removed)</option>
              </select>
              <div class="field-hint">When to send the welcome message. "After verification" fires when the unverified role below is removed — e.g. once DoubleCounter finishes its scan and lifts the gate. No bio is required.</div>
            </div>
            <div class="field" data-verified-only style="${trigger === "verified" ? "" : "display:none"}">
              <label>Unverified Role</label>
              <span data-picker="unverified_role_id"></span>
              <div class="field-hint">Role that is removed by your verification process (e.g. DoubleCounter). The welcome fires the moment this role is stripped.</div>
            </div>
            <div class="field">
              <label for="cw-welcome-message">Welcome Message</label>
              <textarea name="welcome_message" id="cw-welcome-message" data-template>${esc(w.welcome_message || "")}</textarea>
              <div class="field-hint">Tap a placeholder below to insert it at the cursor:</div>
              ${chipsHtml("welcome_message")}
              ${legendHtml()}
            </div>
            <div class="field">
              <label>Welcome Ping Role</label>
              <span data-picker="welcome_ping_role_id"></span>
              <div class="field-hint">This role is mentioned above the welcome embed so its holders get notified of every new arrival. "(none)" pings nobody.</div>
            </div>
            <div class="field">
              <label><input type="checkbox" name="welcome_ping_member" ${w.welcome_ping_member ? "checked" : ""} /> Ping the new member</label>
              <div class="field-hint">Mention the joining member in the message so they get a notification. The <code>{member}</code> placeholder inside the message body looks like a mention but does not notify — only this option pings the member.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Leave Message</div>
            <div class="field">
              <label>Leave Channel</label>
              <span data-picker="leave_channel_id"></span>
              <div class="field-hint">Where the goodbye embed is posted when a member leaves. "(disabled)" turns leave messages off.</div>
            </div>
            <div class="field">
              <label for="cw-leave-message">Leave Message</label>
              <textarea name="leave_message" id="cw-leave-message" data-template>${esc(w.leave_message || "")}</textarea>
              <div class="field-hint">Tap a placeholder below to insert it at the cursor:</div>
              ${chipsHtml("leave_message")}
            </div>
          </div>

          <div class="card">
            <div class="section-label">Greeter Team</div>
            <div class="field">
              <label>Greeter Role</label>
              <span data-picker="greeter_role_id"></span>
              <div class="field-hint">Members with this role are your greeter team; the Greeter Response report scores how quickly they welcome newcomers.</div>
            </div>
            <div class="field">
              <label>Greeter Chat Channel</label>
              <span data-picker="greeter_chat_channel_id"></span>
              <div class="field-hint">The channel where greeters chat with newcomers — the Greeter Response report watches it for first replies.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Newcomer Guide & Logging</div>
            <div class="field">
              <label>Server Guide Channel</label>
              <span data-picker="server_guide_channel_id"></span>
              <div class="field-hint">Optional — the channel that introduces newcomers to your server. When set, the {server_guide} placeholder above resolves to a clickable #mention of this channel.</div>
            </div>
            <div class="field">
              <label>Join / Leave Log Channel</label>
              <span data-picker="join_leave_log_channel_id"></span>
              <div class="field-hint">Used by the Greeter Response report to time joins, greetings, and early departures.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
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

    // Searchable pickers replace the old plain <select>s. getValue() falls
    // back to "0", matching the legacy "(disabled)"/"(none)" contract.
    const pickers = {};
    const mountDefs = [
      ["welcome_channel_id", mountChannelPicker, channels, "Welcome Channel"],
      ["unverified_role_id", mountRolePicker, roles, "Unverified Role"],
      ["welcome_ping_role_id", mountRolePicker, roles, "Welcome Ping Role"],
      ["leave_channel_id", mountChannelPicker, channels, "Leave Channel"],
      ["greeter_role_id", mountRolePicker, roles, "Greeter Role"],
      ["greeter_chat_channel_id", mountChannelPicker, channels, "Greeter Chat Channel"],
      ["server_guide_channel_id", mountChannelPicker, channels, "Server Guide Channel"],
      ["join_leave_log_channel_id", mountChannelPicker, channels, "Join / Leave Log Channel"],
    ];
    for (const [name, mountFn, options, label] of mountDefs) {
      pickers[name] = mountFn(
        form.querySelector(`[data-picker="${name}"]`),
        options,
        String(w[name] || "0"),
        { label },
      );
    }

    guardForm(form);

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
        // POST the on-screen values so the preview shows what's typed right
        // now, saved or not (W-C3).
        const fd = new FormData(form);
        const data = await apiPost("/api/config/welcome/preview", {
          welcome_message: fd.get("welcome_message"),
          leave_message: fd.get("leave_message"),
          server_guide_channel_id: pickers.server_guide_channel_id.getValue() || "0",
        });
        previewWrap.innerHTML =
          `<div class="subtitle">Previewing the text in the form above (including unsaved edits). Sample member: ${esc(data.sample_user_name || "(you)")}</div>` +
          renderEmbed("Welcome embed", data.welcome) +
          renderEmbed("Leave embed", data.leave);
      } catch (err) {
        previewWrap.textContent = `Preview failed: ${err.message}`;
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/welcome", {
          welcome_channel_id: pickers.welcome_channel_id.getValue() || "0",
          welcome_message: fd.get("welcome_message"),
          welcome_ping_role_id: pickers.welcome_ping_role_id.getValue() || "0",
          welcome_ping_member: fd.get("welcome_ping_member") === "on",
          welcome_trigger: fd.get("welcome_trigger"),
          unverified_role_id: pickers.unverified_role_id.getValue() || "0",
          leave_channel_id: pickers.leave_channel_id.getValue() || "0",
          leave_message: fd.get("leave_message"),
          greeter_role_id: pickers.greeter_role_id.getValue() || "0",
          greeter_chat_channel_id: pickers.greeter_chat_channel_id.getValue() || "0",
          server_guide_channel_id: pickers.server_guide_channel_id.getValue() || "0",
          join_leave_log_channel_id: pickers.join_leave_log_channel_id.getValue() || "0",
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
