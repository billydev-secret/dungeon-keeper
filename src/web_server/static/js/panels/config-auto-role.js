import { loadConfig, loadRoles, apiPut, showStatus } from "../config-helpers.js";
import { esc } from "../api.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, roles] = await Promise.all([loadConfig(), loadRoles()]);
    const activeSet = new Set(config.auto_role?.auto_role_ids ?? []);

    // Only show roles the bot can actually assign (not managed — bots, boosters, integrations).
    const assignable = roles.filter((r) => !r.managed);

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Auto-Role on Join</h2>
          <div class="subtitle">Roles automatically applied to every new member when they join</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Roles to assign on join</label>
            ${assignable.length === 0
              ? `<div class="empty">No assignable roles found.</div>`
              : `<div data-checkboxes class="checkbox-list">
                  ${assignable.map((r) => `
                    <label>
                      <input type="checkbox" name="roles" value="${esc(r.id)}" ${activeSet.has(r.id) ? "checked" : ""} />
                      <span style="color:${esc(r.color)}">@${esc(r.name)}</span>
                    </label>
                  `).join("")}
                </div>`
            }
            <div class="field-hint">Managed roles (booster, bot integration) are excluded — Discord does not allow them to be assigned manually. Roles above the bot's own highest role are filtered at apply time and logged.</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const checked = [...form.querySelectorAll('input[name="roles"]:checked')].map((el) => el.value);
      try {
        await apiPut("/api/config/auto-role", { auto_role_ids: checked });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
