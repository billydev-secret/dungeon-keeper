import { apiPost, esc } from "../api.js";

export function mount(container) {
  const html = `
    <div class="panel">
      <header>
        <h2>Admin Backfill Jobs</h2>
        <div class="subtitle">One-shot data backfills. XP & interactions backfills run in the background — progress in bot logs.</div>
      </header>

      <div class="form">
        <h3>Role events</h3>
        <p class="field-hint">Sync the role_events log with current server state. Adds missing grant/remove events. Idempotent and fast.</p>
        <button class="btn btn-primary" data-action="roles">Run role backfill</button>
        <div data-status="roles" style="margin-top:8px;"></div>
      </div>

      <hr style="margin:20px 0;">

      <div class="form">
        <h3>XP history</h3>
        <p class="field-hint">Scan past messages and award any XP that wasn't recorded. Already-processed messages are skipped (re-runnable).</p>
        <label>Days to scan (0 = all available)
          <input type="number" data-control="xp-days" min="0" max="3650" value="30" />
        </label>
        <button class="btn btn-primary" data-action="xp">Start XP backfill</button>
        <div data-status="xp" style="margin-top:8px;"></div>
      </div>

      <hr style="margin:20px 0;">

      <div class="form">
        <h3>Interaction graph</h3>
        <p class="field-hint">Backfill replies + mentions for the connection web / interaction heatmap. Use <em>reset</em> if counts look inflated.</p>
        <label>Days (0 = all available)
          <input type="number" data-control="int-days" min="0" max="3650" value="0" />
        </label>
        <label>Channel ID (optional)
          <input type="text" data-control="int-channel" placeholder="leave blank for all readable channels" />
        </label>
        <label><input type="checkbox" data-control="int-reset" /> Reset existing data first</label>
        <button class="btn btn-primary" data-action="interactions">Start interaction backfill</button>
        <div data-status="interactions" style="margin-top:8px;"></div>
      </div>
    </div>
  `;
  container.innerHTML = html;

  function statusEl(name) { return container.querySelector(`[data-status="${name}"]`); }

  async function runRoles() {
    const s = statusEl("roles");
    s.textContent = "Running…";
    try {
      const r = await apiPost("/api/admin/backfill-roles", {});
      s.textContent = r.message || "Done.";
    } catch (err) {
      s.textContent = `Error: ${err.message}`;
    }
  }

  async function runXp() {
    const s = statusEl("xp");
    const days = parseInt(container.querySelector('[data-control="xp-days"]').value) || 0;
    s.textContent = "Starting…";
    try {
      const r = await apiPost("/api/admin/backfill-xp", { days });
      s.textContent = r.message || "Started.";
    } catch (err) {
      s.textContent = `Error: ${err.message}`;
    }
  }

  async function runInteractions() {
    const s = statusEl("interactions");
    const days = parseInt(container.querySelector('[data-control="int-days"]').value) || 0;
    const reset = container.querySelector('[data-control="int-reset"]').checked;
    const channelId = container.querySelector('[data-control="int-channel"]').value.trim();
    let qs = `reset=${reset ? "true" : "false"}`;
    if (channelId) qs += `&channel_id=${encodeURIComponent(channelId)}`;
    s.textContent = "Starting…";
    try {
      const r = await apiPost(`/api/admin/backfill-interactions?${qs}`, { days });
      s.textContent = r.message || "Started.";
    } catch (err) {
      s.textContent = `Error: ${err.message}`;
    }
  }

  container.querySelector('[data-action="roles"]').addEventListener("click", runRoles);
  container.querySelector('[data-action="xp"]').addEventListener("click", runXp);
  container.querySelector('[data-action="interactions"]').addEventListener("click", runInteractions);

  return { unmount() {} };
}
