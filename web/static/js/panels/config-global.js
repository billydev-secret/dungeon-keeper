import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const g = config.global;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Global Config</h2>
          <div class="subtitle">Timezone, mod channel, and bypass roles</div>
        </header>
        <form class="config-form" data-form>
          <div class="field">
            <label>Timezone Offset (hours from UTC)</label>
            <input type="number" step="0.5" name="tz_offset_hours" value="${g.tz_offset_hours}" />
            <div class="field-hint">e.g. -5 for EST, 1 for CET</div>
          </div>
          <div class="field">
            <label>Mod Channel</label>
            <select name="mod_channel_id">${channelSelect(channels, g.mod_channel_id)}</select>
          </div>
          <div class="field">
            <label>Bypass Role IDs</label>
            <input type="text" name="bypass_role_ids" value="${g.bypass_role_ids.join(", ")}" />
            <div class="field-hint">Roles that bypass spoiler guard and other restrictions (comma-separated IDs)</div>
          </div>
          <div class="field">
            <label>Booster Swatch Directory</label>
            <input type="text" name="booster_swatch_dir" value="${g.booster_swatch_dir || ""}" />
            <div class="field-hint">Folder with booster color swatch images</div>
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
        await apiPut("/api/config/global", {
          tz_offset_hours: parseFloat(fd.get("tz_offset_hours")) || 0,
          mod_channel_id: fd.get("mod_channel_id"),
          bypass_role_ids: fd.get("bypass_role_ids").split(",").map((s) => s.trim()).filter(Boolean),
          booster_swatch_dir: fd.get("booster_swatch_dir"),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
