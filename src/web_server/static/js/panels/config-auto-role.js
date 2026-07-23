import {
  loadConfig,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountRoleMultiPicker,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, roles] = await Promise.all([loadConfig(), loadRoles()]);
    const active = config.auto_role?.auto_role_ids ?? [];

    // Only offer roles the bot can actually assign — Discord refuses to hand
    // out managed roles (bot integrations, the booster role).
    const assignable = roles.filter((r) => !r.managed);

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Auto-Role on Join</h2>
          <div class="subtitle">Roles handed to every new member the moment they join</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Roles on Join</div>
            <div class="field">
              <label>Roles to Assign on Join</label>
              ${assignable.length === 0
                ? `<div class="empty">This server has no roles Dungeon Keeper can assign. Create a role in Discord (one that is not managed by a bot or by boosting), then reload this page.</div>`
                : `<span data-picker="auto_role_ids"></span>`}
              <div class="field-hint">Every member who joins receives all of these
                roles right away. Leave the list empty to hand out nothing. Managed
                roles — bot integrations and the booster role — are not listed because
                Discord does not allow anyone to assign them. Any role sitting above
                Dungeon Keeper's own highest role is skipped when the member joins and
                the skip is written to the log.</div>
            </div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const slot = form.querySelector('[data-picker="auto_role_ids"]');
    const picker = slot
      ? mountRoleMultiPicker(slot, assignable, active, { label: "Roles to Assign on Join" })
      : null;

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        // Role ids stay strings — the payload shape (a list of id strings) is
        // exactly what the checkbox wall used to post.
        await apiPut("/api/config/auto-role", {
          auto_role_ids: picker ? picker.getValues() : [],
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
