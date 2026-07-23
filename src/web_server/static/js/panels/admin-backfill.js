import { apiPost, esc } from "../api.js";
import { confirmDialog } from "../ui.js";

export function mount(container) {
  const html = `
    <div class="panel">
      <header>
        <h2>Admin Backfill Jobs</h2>
        <div class="subtitle">One-shot jobs that fill in history from before Dungeon Keeper started tracking. The XP and interaction jobs run in the background — watch the bot logs for progress.</div>
      </header>

      <div class="form">
        <h3>Role Events</h3>
        <p class="field-hint">Compare the role history log against who actually holds each role right now, and add the grant or remove events that are missing. Safe to run as often as you like, and it finishes in seconds.</p>
        <button class="btn btn-primary" data-action="roles">Run Role Backfill</button>
        <div data-status="roles" style="margin-top:8px;"></div>
      </div>

      <hr style="margin:20px 0;">

      <div class="form">
        <h3>XP History</h3>
        <p class="field-hint">Read back through past messages and award the XP that was never recorded. Messages already counted are skipped, so re-running is safe.</p>
        <label>Days to Scan (0 scans everything available)
          <input type="number" data-control="xp-days" min="0" max="3650" value="30" />
        </label>
        <button class="btn btn-primary" data-action="xp">Start XP Backfill</button>
        <div data-status="xp" style="margin-top:8px;"></div>
      </div>

      <hr style="margin:20px 0;">

      <div class="form">
        <h3>Interaction Graph</h3>
        <p class="field-hint">Read back through replies and mentions so the Connection Web and Interaction Heatmap have history. Turn on Reset first if the counts look inflated from an earlier run.</p>
        <label>Days to Scan (0 scans everything available)
          <input type="number" data-control="int-days" min="0" max="3650" value="0" />
        </label>
        <label>Channel ID (Optional)
          <input type="text" data-control="int-channel" placeholder="Leave blank to scan every channel Dungeon Keeper can read" />
        </label>
        <label><input type="checkbox" data-control="int-reset" /> Delete existing interaction data first</label>
        <div class="field-hint">Wipes the current interaction graph before rebuilding it. Leave off to add to what’s already there.</div>
        <button class="btn btn-primary" data-action="interactions">Start Interaction Backfill</button>
        <div data-status="interactions" style="margin-top:8px;"></div>
      </div>
    </div>
  `;
  container.innerHTML = html;

  function statusEl(name) { return container.querySelector(`[data-status="${name}"]`); }
  function actionBtn(name) { return container.querySelector(`[data-action="${name}"]`); }

  async function runRoles() {
    const s = statusEl("roles");
    const btn = actionBtn("roles");
    btn.disabled = true;
    s.textContent = "Running…";
    try {
      const r = await apiPost("/api/admin/backfill-roles", {});
      s.textContent = r.message || "Done.";
    } catch (err) {
      s.textContent = `Error: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  }

  async function runXp() {
    const s = statusEl("xp");
    const btn = actionBtn("xp");
    const days = parseInt(container.querySelector('[data-control="xp-days"]').value) || 0;
    btn.disabled = true;
    s.textContent = "Starting…";
    try {
      const r = await apiPost("/api/admin/backfill-xp", { days });
      s.textContent = r.message || "Started.";
    } catch (err) {
      s.textContent = `Error: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  }

  async function runInteractions() {
    const s = statusEl("interactions");
    const btn = actionBtn("interactions");
    const days = parseInt(container.querySelector('[data-control="int-days"]').value) || 0;
    const reset = container.querySelector('[data-control="int-reset"]').checked;
    const channelId = container.querySelector('[data-control="int-channel"]').value.trim();
    if (reset && !(await confirmDialog("Delete the current interaction graph before backfilling? Everything the Connection Web and Interaction Heatmap show today will be rebuilt from scratch.", { title: "Reset Interaction Data", danger: true, confirmLabel: "Reset and Run" }))) return;
    let qs = `reset=${reset ? "true" : "false"}`;
    if (channelId) qs += `&channel_id=${encodeURIComponent(channelId)}`;
    btn.disabled = true;
    s.textContent = "Starting…";
    try {
      const r = await apiPost(`/api/admin/backfill-interactions?${qs}`, { days });
      s.textContent = r.message || "Started.";
    } catch (err) {
      s.textContent = `Error: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  }

  container.querySelector('[data-action="roles"]').addEventListener("click", runRoles);
  container.querySelector('[data-action="xp"]').addEventListener("click", runXp);
  container.querySelector('[data-action="interactions"]').addEventListener("click", runInteractions);

  return { unmount() {} };
}
