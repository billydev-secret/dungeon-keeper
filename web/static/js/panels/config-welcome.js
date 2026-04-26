import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, showStatus } from "../config-helpers.js";
import { api, esc } from "../api.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const w = config.welcome;

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
            <label>Welcome Message</label>
            <textarea name="welcome_message">${w.welcome_message}</textarea>
            <div class="field-hint">Use {member} for mention, {member_name} for display name, {server} for server name, {member_count} for member count</div>
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
            <textarea name="leave_message">${w.leave_message}</textarea>
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
          leave_channel_id: fd.get("leave_channel_id"),
          leave_message: fd.get("leave_message"),
          greeter_role_id: fd.get("greeter_role_id"),
          greeter_chat_channel_id: fd.get("greeter_chat_channel_id"),
          join_leave_log_channel_id: fd.get("join_leave_log_channel_id"),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
