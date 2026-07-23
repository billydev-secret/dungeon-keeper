import {
  loadConfig,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountRolePicker,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, roles] = await Promise.all([loadConfig(), loadRoles()]);
    const r = config.risky || {};
    // Stored in seconds, edited in minutes — the unit is in the field label.
    const minMinutes = Math.round((r.min_game_seconds || 0) / 60);

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Risky Roller</h2>
          <div class="subtitle">A dice game members start with <code>/risky start</code></div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Announcements</div>
            <div class="field">
              <label>Ping Role</label>
              <span data-picker="ping_role_id"></span>
              <div class="field-hint">This role is mentioned whenever a new round
                opens, so its holders get a notification. "(none)" starts rounds
                quietly.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Round Rules</div>
            <div class="field">
              <label for="rr-min">Minimum Round Length (minutes)</label>
              <input type="number" name="min_game_minutes" id="rr-min" required
                min="0" max="1440" step="1" value="${minMinutes}" style="max-width:140px;" />
              <div class="field-hint">A round must stay open at least this long before
                anyone can close it, so latecomers still get a chance to join. 0 lets
                the host close a round the moment it opens.</div>
            </div>
            <div class="field">
              <label for="rr-max">Rounds Running at Once, Per Channel</label>
              <input type="number" name="max_games_per_channel" id="rr-max" required
                min="1" max="100" step="1" value="${r.max_games_per_channel || 10}" style="max-width:140px;" />
              <div class="field-hint">Once a channel has this many open rounds,
                <code>/risky start</code> is refused there until one finishes. Keeps a
                busy channel from filling with half-played games.</div>
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

    const rolePicker = mountRolePicker(
      form.querySelector('[data-picker="ping_role_id"]'),
      roles, String(r.ping_role_id || "0"), { label: "Ping Role" },
    );

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const mins = parseInt(fd.get("min_game_minutes"), 10);
      if (!Number.isFinite(mins) || mins < 0 || mins > 1440) {
        showStatus(status, false, "Minimum Round Length must be a number of minutes from 0 to 1440");
        form.querySelector("[name=min_game_minutes]").focus();
        return;
      }
      const maxGames = parseInt(fd.get("max_games_per_channel"), 10);
      if (!Number.isFinite(maxGames) || maxGames < 1 || maxGames > 100) {
        showStatus(status, false, "Rounds Running at Once must be a number from 1 to 100");
        form.querySelector("[name=max_games_per_channel]").focus();
        return;
      }
      try {
        await apiPut("/api/config/risky", {
          // Role id stays a string; minutes are still converted back to the
          // seconds the API stores.
          ping_role_id: rolePicker.getValue() || "0",
          min_game_seconds: mins * 60,
          max_games_per_channel: maxGames,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
