import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const grants = config.roles;
    const names = Object.keys(grants);

    if (!names.length) {
      container.innerHTML = `
        <div class="panel">
          <header><h2>Role Grants</h2><div class="subtitle">No grant roles configured.</div></header>
        </div>`;
      return;
    }

    function renderRole(name) {
      const g = grants[name];
      return `
        <form class="config-form" style="margin-bottom:24px; padding:16px; background:var(--bg-alt); border-radius:6px;" data-grant="${name}">
          <h3 style="margin:0 0 8px; font-size:15px;">${g.label || name}</h3>
          <div class="field">
            <label>Role</label>
            <select name="role_id">${roleSelect(roles, g.role_id)}</select>
          </div>
          <div class="field">
            <label>Log Channel</label>
            <select name="log_channel_id">${channelSelect(channels, g.log_channel_id)}</select>
          </div>
          <div class="field">
            <label>Announce Channel</label>
            <select name="announce_channel_id">${channelSelect(channels, g.announce_channel_id)}</select>
          </div>
          <div class="field">
            <label>Grant Message</label>
            <textarea name="grant_message">${g.grant_message}</textarea>
          </div>
          <div><button type="submit">Save</button><span data-status></span></div>
        </form>
      `;
    }

    container.innerHTML = `
      <div class="panel" style="overflow-y:auto;">
        <header>
          <h2>Role Grants</h2>
          <div class="subtitle">Configure grant roles (denizen, nsfw, veteran, etc.)</div>
        </header>
        <div data-grants>${names.map(renderRole).join("")}</div>
      </div>
    `;

    for (const name of names) {
      const form = container.querySelector(`[data-grant="${name}"]`);
      const status = form.querySelector("[data-status]");
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        try {
          await apiPut(`/api/config/roles/${name}`, {
            role_id: fd.get("role_id"),
            log_channel_id: fd.get("log_channel_id"),
            announce_channel_id: fd.get("announce_channel_id"),
            grant_message: fd.get("grant_message"),
          });
          showStatus(status, true);
        } catch (err) {
          showStatus(status, false, err.message);
        }
      });
    }
  })();
}
