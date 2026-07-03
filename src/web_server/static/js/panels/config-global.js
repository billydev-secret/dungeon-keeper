import { api } from "../api.js";
import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelectMulti, apiPut, showStatus } from "../config-helpers.js";

function _esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config...</div></div>`;

  (async () => {
    const [config, channels, roles, supportResp] = await Promise.all([
      loadConfig(), loadChannels(), loadRoles(),
      api("/api/config/support-access").catch(() => ({ enabled: false })),
    ]);
    const g = config.global;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Global Config</h2>
          <div class="subtitle">Timezone, mod channel, bypass roles, and recorded bots</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Timezone Offset (hours from UTC)</label>
            <input type="number" step="0.5" name="tz_offset_hours" value="${_esc(g.tz_offset_hours)}" />
            <div class="field-hint">e.g. -5 for EST, 1 for CET</div>
          </div>
          <div class="field">
            <label>Mod Channel</label>
            <select name="mod_channel_id">${channelSelect(channels, g.mod_channel_id)}</select>
          </div>
          <div class="field">
            <label>Bypass Roles</label>
            <select name="bypass_role_ids" multiple size="6">${roleSelectMulti(roles, g.bypass_role_ids)}</select>
            <div class="field-hint">Roles that bypass spoiler guard and other restrictions (Ctrl/Cmd-click to select multiple)</div>
          </div>
          <div class="field">
            <label>Recorded Bot User IDs</label>
            <input type="text" name="recorded_bot_user_ids" value="${_esc((g.recorded_bot_user_ids || []).join(", "))}" />
            <div class="field-hint">Bot accounts whose messages should be stored (e.g. Risky Roller). Comma-separated user IDs. These bots still don't earn XP or trigger wellness/moderation.</div>
          </div>
          <div class="field">
            <label>Booster Swatch Directory</label>
            <input type="text" name="booster_swatch_dir" value="${_esc(g.booster_swatch_dir || "")}" />
            <div class="field-hint">Folder with booster color swatch images</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>

        <section class="form" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--border,#333)">
          <h3 style="margin:0 0 0.25rem">Support Access</h3>
          <div class="field-hint" style="margin-bottom:1rem">Allow the Dungeon Keeper developer to access this server's dashboard to help troubleshoot issues or assist with configuration. You can revoke access at any time.</div>
          <div style="display:flex;align-items:center;gap:12px;">
            <label style="margin:0;cursor:pointer;display:flex;align-items:center;gap:8px;">
              <input type="checkbox" data-support-toggle ${supportResp.enabled ? "checked" : ""} />
              Enable developer support access
            </label>
            <span data-support-status></span>
          </div>
        </section>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/global", {
          tz_offset_hours: parseFloat(fd.get("tz_offset_hours")) || 0,
          mod_channel_id: fd.get("mod_channel_id"),
          bypass_role_ids: Array.from(form.querySelector('select[name="bypass_role_ids"]').selectedOptions).map((o) => o.value),
          recorded_bot_user_ids: fd.get("recorded_bot_user_ids").split(",").map((s) => s.trim()).filter(Boolean),
          booster_swatch_dir: fd.get("booster_swatch_dir"),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    const supportToggle = container.querySelector("[data-support-toggle]");
    const supportStatus = container.querySelector("[data-support-status]");
    supportToggle.addEventListener("change", async () => {
      try {
        await apiPut("/api/config/support-access", { enabled: supportToggle.checked });
        showStatus(supportStatus, true, supportToggle.checked ? "Access granted" : "Access revoked");
      } catch (err) {
        supportToggle.checked = !supportToggle.checked;
        showStatus(supportStatus, false, err.message);
      }
    });
  })();
}
