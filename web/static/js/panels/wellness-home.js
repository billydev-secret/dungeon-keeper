import { wGet, wPost, esc, showStatus } from "../wellness-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading wellness...</div></div>`;

  (async () => {
    let d;
    try { d = await wGet("/api/wellness/me"); } catch (e) {
      container.querySelector(".panel").innerHTML = `<div class="error">${e.message}</div>`;
      return;
    }

    if (!d.opted_in) {
      container.querySelector(".panel").innerHTML = `
        <header><h2>Wellness</h2></header>
        <div class="w-notice">
          <p>You haven't opted in to the wellness programme yet.</p>
          <p>Use <code>/wellness setup</code> in Discord to get started.</p>
        </div>`;
      return;
    }

    const pausedHTML = d.paused_until && d.paused_until > Date.now() / 1000
      ? `<div class="w-badge w-badge-warn">Paused until ${new Date(d.paused_until * 1000).toLocaleTimeString()}</div>`
      : "";

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Wellness</h2>
        <div class="subtitle">Your wellness dashboard</div>
      </header>

      <div class="w-grid">
        <div class="w-card w-card-hero">
          <div class="w-streak-badge">${esc(d.streak.badge)}</div>
          <div class="w-streak-days">${d.streak.current_days} day streak</div>
          <div class="w-streak-sub">Personal best: ${d.streak.personal_best} &middot; Next: ${esc(d.next_milestone_text)}</div>
          ${pausedHTML}
        </div>

        <div class="w-card">
          <div class="w-card-label">Caps</div>
          <div class="w-card-big">${d.caps_count}</div>
          <a href="#/wellness-caps" class="w-card-link">Manage</a>
        </div>

        <div class="w-card">
          <div class="w-card-label">Active Blackouts</div>
          <div class="w-card-big">${d.blackouts_count}</div>
          <a href="#/wellness-blackouts" class="w-card-link">Manage</a>
        </div>

        <div class="w-card">
          <div class="w-card-label">Partners</div>
          <div class="w-card-big">${d.partners_count}${d.pending_partners_count ? ` <span class="w-pending">(${d.pending_partners_count} pending)</span>` : ""}</div>
          <a href="#/wellness-partners" class="w-card-link">Manage</a>
        </div>

        <div class="w-card">
          <div class="w-card-label">Away</div>
          <div class="w-card-big">${d.away_enabled ? "On" : "Off"}</div>
          <a href="#/wellness-away" class="w-card-link">Configure</a>
        </div>
      </div>

      <section class="w-section">
        <h3>Settings</h3>
        <form data-settings-form class="w-form">
          <div class="field">
            <label>Timezone</label>
            <input type="text" name="timezone" value="${esc(d.timezone)}" />
          </div>
          <div class="field">
            <label>Enforcement Level</label>
            <select name="enforcement_level">
              ${d.enforcement_levels.map(e => `<option value="${e}"${e === d.enforcement_level ? " selected" : ""}>${e}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label>Notifications</label>
            <select name="notifications_pref">
              ${d.notification_prefs.map(e => `<option value="${e}"${e === d.notifications_pref ? " selected" : ""}>${e}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label>Daily Reset Hour (0-23)</label>
            <input type="number" name="daily_reset_hour" min="0" max="23" value="${d.daily_reset_hour}" />
          </div>
          <div class="field">
            <label><input type="checkbox" name="public_commitment" ${d.public_commitment ? "checked" : ""} /> Public commitment</label>
          </div>
          <div><button type="submit">Save</button><span data-status></span></div>
        </form>
      </section>

      <section class="w-section">
        <h3>Quick Actions</h3>
        <div class="w-actions">
          <form data-pause-form class="w-inline-form">
            <input type="number" name="minutes" min="1" max="10080" value="60" style="width:80px" />
            <button type="submit">Pause (minutes)</button>
            <span data-pause-status></span>
          </form>
          <button data-resume-btn>Resume</button>
        </div>
      </section>

      ${d.crisis_resource_url ? `<div class="w-crisis"><a href="${esc(d.crisis_resource_url)}" target="_blank" rel="noopener">Crisis resources</a></div>` : ""}
    `;

    // Settings form
    const sForm = container.querySelector("[data-settings-form]");
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
      } catch (err) { showStatus(sStatus, false, err.message); }
    });

    // Pause
    const pForm = container.querySelector("[data-pause-form]");
    const pStatus = container.querySelector("[data-pause-status]");
    pForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await wPost("/api/wellness/pause", { minutes: parseInt(new FormData(pForm).get("minutes"), 10) });
        showStatus(pStatus, true, "Paused");
      } catch (err) { showStatus(pStatus, false, err.message); }
    });

    // Resume
    container.querySelector("[data-resume-btn]").addEventListener("click", async () => {
      try { await wPost("/api/wellness/resume", {}); showStatus(pStatus, true, "Resumed"); }
      catch (err) { showStatus(pStatus, false, err.message); }
    });
  })();
}
