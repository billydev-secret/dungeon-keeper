import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const w = config.welcome;

    container.innerHTML = `
      <div class="panel" style="overflow-y:auto;">
        <header>
          <h2>Welcome & Leave</h2>
          <div class="subtitle">Welcome/leave channels, messages, greeter settings</div>
        </header>
        <form class="config-form" data-form>
          <div class="field">
            <label>Welcome Channel</label>
            <select name="welcome_channel_id">${channelSelect(channels, w.welcome_channel_id)}</select>
          </div>
          <div class="field">
            <label>Welcome Message</label>
            <textarea name="welcome_message">${w.welcome_message}</textarea>
            <div class="field-hint">Use {user} for mention, {server} for server name</div>
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
          <div><button type="submit">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
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
