import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const xp = config.xp;

    container.innerHTML = `
      <div class="panel" style="overflow-y:auto;">
        <header>
          <h2>XP Logging</h2>
          <div class="subtitle">XP role grants, log channels, and excluded channels</div>
        </header>
        <form class="config-form" data-form>
          <div class="field">
            <label>Level 5 Role</label>
            <select name="level_5_role_id">${roleSelect(roles, xp.level_5_role_id)}</select>
          </div>
          <div class="field">
            <label>Level 5 Log Channel</label>
            <select name="level_5_log_channel_id">${channelSelect(channels, xp.level_5_log_channel_id)}</select>
          </div>
          <div class="field">
            <label>Level-Up Log Channel</label>
            <select name="level_up_log_channel_id">${channelSelect(channels, xp.level_up_log_channel_id)}</select>
          </div>
          <div class="field">
            <label>XP Grant Allowed User IDs</label>
            <input type="text" name="xp_grant_allowed_user_ids" value="${xp.xp_grant_allowed_user_ids.join(", ")}" />
            <div class="field-hint">Comma-separated user IDs allowed to use /xp grant</div>
          </div>
          <div class="field">
            <label>XP Excluded Channel IDs</label>
            <input type="text" name="xp_excluded_channel_ids" value="${xp.xp_excluded_channel_ids.join(", ")}" />
            <div class="field-hint">Comma-separated channel IDs where XP is not earned</div>
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
        await apiPut("/api/config/xp", {
          level_5_role_id: fd.get("level_5_role_id"),
          level_5_log_channel_id: fd.get("level_5_log_channel_id"),
          level_up_log_channel_id: fd.get("level_up_log_channel_id"),
          xp_grant_allowed_user_ids: fd.get("xp_grant_allowed_user_ids").split(",").map((s) => s.trim()).filter(Boolean),
          xp_excluded_channel_ids: fd.get("xp_excluded_channel_ids").split(",").map((s) => s.trim()).filter(Boolean),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
