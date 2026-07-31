import { wGet, wPost, esc, showStatus, enfLabel, notifLabel } from "../wellness-helpers.js";
import { guardForm } from "../config-helpers.js";
import { renderLoading, renderError } from "../states.js";

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading your wellness dashboard…")}</div>`;

  (async () => {
    let d;
    try { d = await wGet("/api/wellness/me"); } catch (e) {
      container.querySelector(".panel").innerHTML =
        renderError(`Couldn’t load your wellness dashboard — try again. (${e.message})`);
      return;
    }

    if (!d.opted_in) {
      container.querySelector(".panel").innerHTML = `
        <header><h2>Wellness</h2></header>
        <div class="w-notice">
          <p>You haven’t joined the wellness program yet. It tracks your own
          posting habits and nudges you when you ask it to — nobody else sees your data.</p>
          <p>Run <code>/wellness setup</code> in Discord to get started.</p>
        </div>`;
      return;
    }

    const pausedHTML = d.paused_until && d.paused_until > Date.now() / 1000
      ? `<div class="chip chip-warning" style="margin-top:8px;">Paused until ${new Date(d.paused_until * 1000).toLocaleTimeString()}</div>`
      : "";

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Wellness</h2>
        <div class="subtitle">Your wellness dashboard</div>
      </header>

      <div class="card-grid">
        <div class="card w-card-hero">
          <div class="w-streak-badge">${esc(d.streak.badge)}</div>
          <div class="w-streak-days">${d.streak.current_days} day streak</div>
          <div class="w-streak-sub">Personal best: ${d.streak.personal_best} &middot; Next: ${esc(d.next_milestone_text)}</div>
          ${pausedHTML}
        </div>

        <div class="card">
          <div class="stat-label">Caps</div>
          <div class="stat-value">${d.caps_count}</div>
          <a href="#/wellness-caps" class="w-card-link">Manage</a>
        </div>

        <div class="card">
          <div class="stat-label">Active Blackouts</div>
          <div class="stat-value">${d.blackouts_count}</div>
          <a href="#/wellness-blackouts" class="w-card-link">Manage</a>
        </div>

        <div class="card">
          <div class="stat-label">Partners</div>
          <div class="stat-value">${d.partners_count}${d.pending_partners_count ? ` <span class="w-pending">(${d.pending_partners_count} pending)</span>` : ""}</div>
          <a href="#/wellness-partners" class="w-card-link">Manage</a>
        </div>

        <div class="card">
          <div class="stat-label">Away</div>
          <div class="stat-value">${d.away_enabled ? "On" : "Off"}</div>
          <a href="#/wellness-away" class="w-card-link">Configure</a>
        </div>
      </div>

      <div class="section-label">Settings</div>
      <form data-settings-form class="form">
          <div class="field">
            <label>Timezone
              <input type="text" name="timezone" value="${esc(d.timezone)}" placeholder="e.g. America/New_York" />
            </label>
            <div class="field-hint">Caps and blackout windows follow this timezone.</div>
          </div>
          <div class="field">
            <label>Enforcement Level
              <select name="enforcement_level">
                ${d.enforcement_levels.map(e => `<option value="${e}"${e === d.enforcement_level ? " selected" : ""}>${enfLabel(e)}</option>`).join("")}
              </select>
            </label>
            <div class="field-hint">How firmly Dungeon Keeper holds you to your own limits.</div>
          </div>
          <div class="field">
            <label>Notifications
              <select name="notifications_pref">
                ${d.notification_prefs.map(e => `<option value="${e}"${e === d.notifications_pref ? " selected" : ""}>${notifLabel(e)}</option>`).join("")}
              </select>
            </label>
            <div class="field-hint">Where limit notices go. In-channel replies delete after ~30 seconds
            but are visible to the room while they last; DM keeps it private. This also decides whether
            your daily digest DM includes your wellness line.</div>
          </div>
          <div class="field">
            <label>Daily Reset Hour (0-23)
              <input type="number" name="daily_reset_hour" min="0" max="23" value="${d.daily_reset_hour}" />
            </label>
            <div class="field-hint">The hour your daily counters start over — pick the hour you usually wake up.</div>
          </div>
          <div class="field">
            <label><input type="checkbox" name="public_commitment" ${d.public_commitment ? "checked" : ""} /> Share My Streak Publicly</label>
            <div class="field-hint">When on, your name and streak appear on the pinned Active in Commitment list, and milestone celebrations may mention you in the wellness channel. Off (the default) keeps all of that private.</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
      </form>

      <div class="section-label">Quick Actions</div>
      <div class="w-actions">
        <form data-pause-form class="w-inline-form">
          <label style="display:inline-flex;align-items:center;gap:6px;">Pause for (minutes)
            <input type="number" name="minutes" min="1" max="10080" value="60" style="width:80px" />
          </label>
          <button type="submit" class="btn">Pause the Program</button>
          <span data-pause-status></span>
        </form>
        <button data-resume-btn class="btn">Resume</button>
      </div>
      <div class="field-hint">Pausing stops everything until it ends — enforcement, blackout windows,
      streak credit, and wellness DMs.</div>

      <div class="section-label">Leave</div>
      <div class="w-actions">
        <button data-optout-btn class="btn btn-danger">Leave the Program</button>
        <span data-optout-status></span>
      </div>
      <div class="field-hint">Stops all tracking, nudges and enforcement immediately and removes your wellness role.
      Your settings are kept for 30 days in case you come back, then deleted.</div>
    `;

    // Settings form
    const sForm = guardForm(container.querySelector("[data-settings-form]"));
    const sStatus = container.querySelector("[data-status]");
    sForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(sForm);
      try {
        await wPost("/api/wellness/settings", {
          timezone: fd.get("timezone"),
          enforcement_level: fd.get("enforcement_level"),
          notifications_pref: fd.get("notifications_pref"),
          daily_reset_hour: parseInt(fd.get("daily_reset_hour"), 10),
          public_commitment: sForm.querySelector("[name=public_commitment]").checked,
        });
        showStatus(sStatus, true);
      } catch (err) { showStatus(sStatus, false, `Couldn’t save — ${err.message}`); }
    });

    // Pause
    const pForm = container.querySelector("[data-pause-form]");
    const pStatus = container.querySelector("[data-pause-status]");
    pForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await wPost("/api/wellness/pause", { minutes: parseInt(new FormData(pForm).get("minutes"), 10) });
        showStatus(pStatus, true, "Program paused");
      } catch (err) { showStatus(pStatus, false, `Couldn’t pause — ${err.message}`); }
    });

    // Resume
    container.querySelector("[data-resume-btn]").addEventListener("click", async () => {
      try { await wPost("/api/wellness/resume", {}); showStatus(pStatus, true, "Program resumed"); }
      catch (err) { showStatus(pStatus, false, `Couldn’t resume — ${err.message}`); }
    });

    // Opt out
    const oStatus = container.querySelector("[data-optout-status]");
    container.querySelector("[data-optout-btn]").addEventListener("click", async () => {
      if (!window.confirm(
        "Leave the wellness program?\n\n" +
        "All tracking, nudges and enforcement stop immediately and your wellness role is removed. " +
        "Your caps, blackouts and settings are kept for 30 days in case you rejoin, then deleted."
      )) return;
      try {
        await wPost("/api/wellness/optout", {});
        mount(container); // re-render — shows the not-opted-in notice
      } catch (err) { showStatus(oStatus, false, `Couldn’t leave — ${err.message}`); }
    });
  })();
}
