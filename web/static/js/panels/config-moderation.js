import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const m = config.moderation;

    // Build category options from channels (Discord categories are type 4, but
    // the meta endpoint returns text channels — we accept channel IDs here)
    container.innerHTML = `
      <div class="panel" style="overflow-y:auto;">
        <header>
          <h2>Moderation</h2>
          <div class="subtitle">Jail, ticket, warning, and logging settings</div>
        </header>
        <form class="config-form" data-form>
          <div class="field">
            <label>Jailed Role</label>
            <select name="jailed_role_id">${roleSelect(roles, m.jailed_role_id)}</select>
            <div class="field-hint">Role assigned to jailed members</div>
          </div>
          <div class="field">
            <label>Jail Category ID</label>
            <input type="text" name="jail_category_id" value="${m.jail_category_id !== "0" ? m.jail_category_id : ""}" placeholder="Category ID" />
            <div class="field-hint">Discord category where jail channels are created</div>
          </div>
          <div class="field">
            <label>Ticket Category ID</label>
            <input type="text" name="ticket_category_id" value="${m.ticket_category_id !== "0" ? m.ticket_category_id : ""}" placeholder="Category ID" />
            <div class="field-hint">Discord category where ticket channels are created</div>
          </div>
          <div class="field">
            <label>Log Channel</label>
            <select name="log_channel_id">${channelSelect(channels, m.log_channel_id)}</select>
            <div class="field-hint">Channel for moderation log messages</div>
          </div>
          <div class="field">
            <label>Transcript Channel</label>
            <select name="transcript_channel_id">${channelSelect(channels, m.transcript_channel_id)}</select>
            <div class="field-hint">Where transcripts are posted (falls back to log channel if empty)</div>
          </div>
          <div class="field">
            <label>Mod Role IDs</label>
            <input type="text" name="mod_role_ids" value="${m.mod_role_ids}" />
            <div class="field-hint">Comma-separated role IDs for moderators</div>
          </div>
          <div class="field">
            <label>Admin Role IDs</label>
            <input type="text" name="admin_role_ids" value="${m.admin_role_ids}" />
            <div class="field-hint">Comma-separated role IDs for admins (can escalate tickets)</div>
          </div>
          <div class="field">
            <label>Notify on Ticket Create</label>
            <select name="ticket_notify_on_create">
              <option value="1" ${m.ticket_notify_on_create === "1" ? "selected" : ""}>Yes</option>
              <option value="0" ${m.ticket_notify_on_create === "0" ? "selected" : ""}>No</option>
            </select>
          </div>
          <div class="field">
            <label>Warning Threshold</label>
            <input type="number" name="warning_threshold" min="1" max="99" value="${m.warning_threshold}" />
            <div class="field-hint">Number of active warnings before auto-action</div>
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
        await apiPut("/api/config/moderation", {
          jailed_role_id: fd.get("jailed_role_id"),
          jail_category_id: fd.get("jail_category_id") || "0",
          ticket_category_id: fd.get("ticket_category_id") || "0",
          log_channel_id: fd.get("log_channel_id"),
          transcript_channel_id: fd.get("transcript_channel_id"),
          mod_role_ids: fd.get("mod_role_ids"),
          admin_role_ids: fd.get("admin_role_ids"),
          ticket_notify_on_create: fd.get("ticket_notify_on_create"),
          warning_threshold: parseInt(fd.get("warning_threshold")) || 3,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
