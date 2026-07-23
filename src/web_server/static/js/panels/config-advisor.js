import { api, esc } from "../api.js";
import { apiPut, showStatus, guardForm } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    let cfg;
    try {
      cfg = await api("/api/config/advisor");
    } catch (err) {
      container.innerHTML = `<div class="panel"><div class="error">Assistant settings failed to load: ${esc(err.message)}</div></div>`;
      return;
    }

    // The name itself is per-guild branding, edited on the Branding panel.
    const name = esc(cfg.assistant_name || "Billy-bot");

    const optionsFor = (selected) =>
      (cfg.models || [])
        .map(
          (m) =>
            `<option value="${esc(m.id)}" ${m.id === selected ? "selected" : ""}>${esc(m.label)}</option>`,
        )
        .join("");

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>${name}</h2>
          <div class="subtitle">The AI helper behind <code>/ask</code> and the ask box in the Help panel. Rename it under <strong>Branding</strong>.</div>
        </header>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Models</div>
            <div class="field">
              <label for="ad-model">Model — Members</label>
              <select name="model" id="ad-model">${optionsFor(cfg.model)}</select>
              <div class="field-hint">Which Claude model answers regular members. Haiku is
                the default — fastest and cheapest, and plenty for grounded help. Every
                answer is billed to whoever hosts the bot.</div>
            </div>
            <div class="field">
              <label for="ad-staff-model">Model — Mods &amp; Admins</label>
              <select name="staff_model" id="ad-staff-model">${optionsFor(cfg.staff_model)}</select>
              <div class="field-hint">Which model answers anyone with a moderator or admin
                permission. Defaults to Sonnet 5: staff asks are the ones that look up and
                change settings, where a stronger model pays for itself. Set it to Haiku to
                treat everyone the same.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">What ${name} Can Read</div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="server_context" ${cfg.server_context ? "checked" : ""} />
                Let ${name} read this server as well as the manual
              </label>
              <div class="field-hint">
                <strong>Off by default.</strong> When checked, ${name} may also draw on
                this server's channel topics, pinned messages, recent announcements, and
                dashboard docs — on top of the Dungeon Keeper manual. It is always limited
                to what the person asking can already <em>see</em> (their visible,
                non-age-restricted channels) and shaped by what they can <em>do</em>
                (their roles), so <code>/ask</code> cannot leak anything out of a channel a
                member has no access to. Unchecked, answers come from the manual only.
              </div>
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

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiPut("/api/config/advisor", {
          model: form.querySelector('select[name="model"]').value,
          staff_model: form.querySelector('select[name="staff_model"]').value,
          server_context: form.querySelector('input[name="server_context"]').checked,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
