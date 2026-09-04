import { mountGamePanel } from "./games-panel-shared.js";
import { mountRoleDialStates } from "../role-dial-state.js";
import {
  loadConfig,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountRolePicker,
  mountAsync,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [config, roles] = await Promise.all([loadConfig(), loadRoles()]);
    const r = config.risky || {};
    // Stored in seconds, edited in minutes — the unit is in the field label.
    const minMinutes = Math.round((r.min_game_seconds || 0) / 60);
    const chaseHours = r.chase_hours || 0;
    const fallbackHours = r.fallback_hours || 0;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Risky Rolls</h2>
          <div class="subtitle">A dice game members start with <code>/risky start</code></div>
        </header>
        ${renderMetaWarning()}
        <div class="card" data-region="status"></div>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Announcements</div>
            <div class="field">
              <label>Ping Role</label>
              <span data-picker="ping_role_id"></span>
              <div class="field-hint">This role is mentioned whenever a new round
                opens, so its holders get a notification. "(none)" starts rounds
                quietly, and I won't make one.</div>
              <div data-role-state="risky_ping_role_id"></div>
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

          <div class="card">
            <div class="section-label">The Payoff</div>
            <div class="field">
              <label for="rr-chase">Chase the winner's question after N hours</label>
              <input type="number" name="chase_hours" id="rr-chase" required
                min="0" max="168" step="1" value="${chaseHours}" style="max-width:140px;" />
              <div class="field-hint">A round's payoff is the winner's question, and most
                winners walk off without asking it. After this many hours the winner gets
                one reminder to ask; once a question is posted, the person who owes the
                reply gets one reminder too. 0 sends no reminders.</div>
            </div>
            <div class="field">
              <label for="rr-fallback">Fall back to a bank question after N hours</label>
              <input type="number" name="fallback_hours" id="rr-fallback" required
                min="0" max="168" step="1" value="${fallbackHours}" style="max-width:140px;" />
              <div class="field-hint">If the winner still hasn't asked after this many hours,
                the bot draws a Truth from the Truth or Dare question bank and posts it as
                the winner's question, so the loser still answers. Spicy questions only
                appear in age-restricted channels. 0 leaves an unasked round unasked.
                Set this longer than the reminder above, or the reminder never gets its turn.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    // The on/off switch lives in games_game_config, not the risky config row,
    // so it rides the shared game-panel status section like every other game.
    // The label matches the Global Config list this game appears in, and the
    // hint names both readers — /risky start and the scheduler.
    mountGamePanel(container.querySelector('[data-region="status"]'), {
      gameType: "risky_roll", gameName: "Risky Rolls", gameIcon: "🎰",
      hasBank: false, bare: true,
      statusLabel: "Available on This Server",
      statusHint: "When off, /risky start refuses to open a round and a scheduled"
        + " Risky Rolls round is skipped when its time comes round. Rounds already"
        + " running finish normally.",
    });

    const form = container.querySelector("[data-form]");
    const status = container.querySelector('[data-form] [data-status]');

    const rolePicker = mountRolePicker(
      form.querySelector('[data-picker="ping_role_id"]'),
      roles, String(r.ping_role_id || "0"), { label: "Ping Role" },
    );

    guardForm(form);
    mountRoleDialStates(container);

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
      const hourFields = [
        ["chase_hours", "Chase the winner's question"],
        ["fallback_hours", "Fall back to a bank question"],
      ];
      const hours = {};
      for (const [name, label] of hourFields) {
        const value = parseInt(fd.get(name), 10);
        if (!Number.isFinite(value) || value < 0 || value > 168) {
          showStatus(status, false, `${label} must be a number of hours from 0 to 168`);
          form.querySelector(`[name=${name}]`).focus();
          return;
        }
        hours[name] = value;
      }
      try {
        await apiPut("/api/config/risky", {
          // Role id stays a string; minutes are still converted back to the
          // seconds the API stores.
          ping_role_id: rolePicker.getValue() || "0",
          min_game_seconds: mins * 60,
          max_games_per_channel: maxGames,
          chase_hours: hours.chase_hours,
          fallback_hours: hours.fallback_hours,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the Risky Rolls settings." });
}
