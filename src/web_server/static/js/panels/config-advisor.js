import { api, esc } from "../api.js";
import { apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Billy-bot config…</div></div>`;

  (async () => {
    let cfg;
    try {
      cfg = await api("/api/config/advisor");
    } catch (err) {
      container.innerHTML = `<div class="panel"><div class="error">Failed to load Billy-bot config: ${esc(err.message)}</div></div>`;
      return;
    }

    const options = (cfg.models || [])
      .map(
        (m) =>
          `<option value="${esc(m.id)}" ${m.id === cfg.model ? "selected" : ""}>${esc(m.label)}</option>`,
      )
      .join("");

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Billy-bot</h2>
          <div class="subtitle">The AI helper behind <code>/ask</code> and the Help panel's "Ask Billy-bot" box</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Model</label>
            <select name="model">${options}</select>
            <div class="field-hint">Which Claude model answers questions. Haiku is the default — fastest and cheapest, and plenty for grounded help. Bump to Sonnet 5 for higher quality, or Opus for the best (and priciest).</div>
          </div>
          <div class="field">
            <label><input type="checkbox" name="server_context" ${cfg.server_context ? "checked" : ""} /> Use live server context</label>
            <div class="field-hint">
              <strong>Off by default.</strong> When on, Billy-bot can also use this server's
              channel topics, pinned messages, recent announcements, and dashboard docs to answer —
              in addition to the Dungeon Keeper manual. It is always scoped to what the person
              asking can <em>see</em> (their visible, non-NSFW channels) and tailored to what they
              can <em>do</em> (their roles/permissions), so <code>/ask</code> can't leak content
              from channels a member can't access. Leave off to answer from the manual only.
            </div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiPut("/api/config/advisor", {
          model: form.querySelector('select[name="model"]').value,
          server_context: form.querySelector('input[name="server_context"]').checked,
        });
        showStatus(status, true, "Saved");
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
