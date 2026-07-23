import { loadConfig, apiPut, showStatus, guardForm } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const config = await loadConfig();
    const p = config.policy || {};
    const currentHours = Number.isInteger(p.vote_timeout_hours) ? p.vote_timeout_hours : 72;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Policy Ticket Settings</h2>
          <div class="subtitle">How long the moderator team has to vote on a policy proposal</div>
        </header>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Voting</div>
            <div class="field">
              <label for="pt-timeout">Voting Deadline (hours)</label>
              <input type="number" name="vote_timeout_hours" id="pt-timeout" required
                min="1" max="720" step="1" value="${currentHours}" style="max-width:140px;" />
              <div class="field-hint">Once a proposal has been open this long, any
                moderator who has not voted counts as absent and is left out of the
                tally. A single "no" still rejects the proposal, and a proposal nobody
                voted on fails. Longer deadlines give a scattered team time to weigh in;
                shorter ones keep decisions moving.</div>
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
    const input = form.querySelector("[name=vote_timeout_hours]");

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const hours = parseInt(input.value, 10);
      if (!Number.isFinite(hours) || hours < 1 || hours > 720) {
        showStatus(status, false, "Voting Deadline must be a number of hours from 1 to 720");
        input.focus();
        return;
      }
      try {
        await apiPut("/api/config/policy", { vote_timeout_hours: hours });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
