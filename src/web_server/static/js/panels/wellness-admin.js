import { wGet, wPost, wDelete, esc, showStatus, enfLabel } from "../wellness-helpers.js";
import { confirmDialog, toast } from "../ui.js";
import { guardForm, mountAsync } from "../config-helpers.js";
import { renderLoading, renderEmpty } from "../states.js";

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading wellness settings…")}</div>`;

  return mountAsync(container, async () => {
    // Let the rejection reach mountAsync: it draws the error state *and* a
    // working Try again button. Catching it here rendered a dead-end error
    // and made this panel's own errorMsg unreachable. wellness-caps.js
    // documents the same reasoning where it rethrows on first load.
    const [dash, defaults, users, exempt] = await Promise.all([
      wGet("/api/wellness/admin/dashboard"),
      wGet("/api/wellness/admin/defaults"),
      wGet("/api/wellness/admin/users"),
      wGet("/api/wellness/admin/exempt"),
    ]);

    // Overview cards
    const overviewHTML = `
      <div class="card-grid">
        <div class="card">
          <div class="stat-label">Members Opted In</div>
          <div class="stat-value">${dash.active_count}</div>
        </div>
        <div class="card">
          <div class="stat-label">Exempt Channels</div>
          <div class="stat-value">${dash.exempt_channels.length}</div>
        </div>
        <div class="card">
          <div class="stat-label">Default Enforcement</div>
          <div class="stat-value">${esc(dash.config?.default_enforcement || "—")}</div>
        </div>
      </div>`;

    // Defaults form
    const cfg = defaults.config || {};
    const defaultsHTML = `
      <div class="section-label">Server Defaults</div>
      <form data-defaults-form class="form">
        <div class="field">
          <label>Default Enforcement
            <select name="default_enforcement">
              ${defaults.enforcement_levels.map(e => `<option value="${e}"${e === cfg.default_enforcement ? " selected" : ""}>${enfLabel(e)}</option>`).join("")}
            </select>
          </label>
          <div class="field-hint">The starting level for members who opt in. Each member can change their own afterwards.</div>
        </div>
        <div><button type="submit" class="btn btn-primary">Save</button><span data-defaults-status></span></div>
      </form>`;

    // Users table
    const usersHTML = users.users.length ? `
      <div class="section-label">Members Opted In</div>
      <table class="w-table">
        <thead><tr><th>Member</th><th>Timezone</th><th>Enforcement</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          ${users.users.map(u => `
            <tr data-uid="${u.user_id}">
              <td>${esc(u.name)}</td>
              <td>${esc(u.timezone)}</td>
              <td>${esc(u.enforcement_level)}</td>
              <td>${u.is_paused ? '<span class="chip chip-warning">Paused</span>' : '<span class="chip chip-success">Active</span>'}</td>
              <td>
                ${u.is_paused
                  ? `<button class="btn btn-sm" data-resume-uid="${u.user_id}">Resume</button>`
                  : `<button class="btn btn-sm" data-pause-uid="${u.user_id}">Pause 60 Minutes</button>`}
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>` : `
      <div class="section-label">Members Opted In</div>
      ${renderEmpty("Nobody has opted in yet. Members join by running /wellness setup in Discord.")}`;

    // Exempt channels
    const exemptListHTML = exempt.exempt.length
      ? exempt.exempt.map(ch => `
          <div class="w-row">
            <div class="w-row-main">#${esc(ch.name)}</div>
            <div class="w-row-actions">
              <button class="btn btn-sm btn-danger" data-unexempt="${ch.id}" data-unexempt-name="${esc(ch.name)}">Remove</button>
            </div>
          </div>
        `).join("")
      : renderEmpty("No exempt channels. Messages in every channel count toward members’ wellness caps — add a channel below to leave it out.");

    const channelOptsHTML = exempt.channel_options.length
      ? exempt.channel_options.map(c => `<option value="${c.id}">#${esc(c.name)}</option>`).join("")
      : '<option value="">No channels available</option>';

    const exemptHTML = `
      <div class="section-label">Exempt Channels</div>
      <div class="w-list">${exemptListHTML}</div>
      <form data-exempt-form class="form w-inline-form" style="margin-top:12px;">
        <label style="display:inline-flex;align-items:center;gap:6px;">Channel to exempt
          <select name="channel_id">${channelOptsHTML}</select>
        </label>
        <button type="submit" class="btn btn-primary">Add Exempt Channel</button>
      </form>`;

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Wellness Admin</h2>
        <div class="subtitle">Server-wide defaults for the wellness program. Members still control their own caps and blackouts.</div>
      </header>
      ${overviewHTML}
      ${defaultsHTML}
      ${usersHTML}
      ${exemptHTML}
    `;

    // Defaults form handler
    const dForm = guardForm(container.querySelector("[data-defaults-form]"));
    const dStatus = container.querySelector("[data-defaults-status]");
    dForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(dForm);
      try {
        await wPost("/api/wellness/admin/defaults", {
          default_enforcement: fd.get("default_enforcement"),
        });
        showStatus(dStatus, true);
      } catch (err) { showStatus(dStatus, false, `Couldn’t save — ${err.message}`); }
    });

    // Pause/Resume. Delegated, because a button flips to the opposite action
    // after it fires: a listener bound to the element would keep running the
    // action the button no longer offers. Before this, acting on a row left a
    // disabled "Paused"/"Resumed" label and no way to undo without a reload.
    container.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-pause-uid], [data-resume-uid]");
      if (!btn || btn.disabled) return;
      const pausing = "pauseUid" in btn.dataset;
      const uid = pausing ? btn.dataset.pauseUid : btn.dataset.resumeUid;
      const statusCell = btn.closest("tr").querySelector("td:nth-child(4)");
      btn.disabled = true;
      try {
        if (pausing) {
          await wPost(`/api/wellness/admin/users/${uid}/pause`, { minutes: 60 });
          statusCell.innerHTML = '<span class="chip chip-warning">Paused</span>';
          delete btn.dataset.pauseUid;
          btn.dataset.resumeUid = uid;
          btn.textContent = "Resume";
        } else {
          await wPost(`/api/wellness/admin/users/${uid}/resume`, {});
          statusCell.innerHTML = '<span class="chip chip-success">Active</span>';
          delete btn.dataset.resumeUid;
          btn.dataset.pauseUid = uid;
          btn.textContent = "Pause 60 Minutes";
        }
      } catch (err) {
        toast(`Couldn’t ${pausing ? "pause" : "resume"} that member — ${err.message}`, "error");
      } finally {
        btn.disabled = false;
      }
    });

    // Exempt remove
    container.querySelectorAll("[data-unexempt]").forEach(btn => {
      btn.addEventListener("click", async () => {
        // Every other destructive action on this page confirms first; this one
        // used to fire on the click and immediately start counting messages in
        // that channel against members' caps.
        const name = btn.dataset.unexemptName || "this channel";
        const ok = await confirmDialog(
          `Messages in #${name} will start counting toward members’ wellness caps again.`,
          { title: "Stop exempting this channel?", danger: true, confirmLabel: "Remove" },
        );
        if (!ok) return;
        try {
          await wDelete(`/api/wellness/admin/exempt/${btn.dataset.unexempt}`);
          btn.closest(".w-row").remove();
        } catch (e) { toast(`Couldn’t remove that exempt channel — ${e.message}`, "error"); }
      });
    });

    // Exempt add
    const exForm = container.querySelector("[data-exempt-form]");
    if (exForm) {
      exForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const cid = new FormData(exForm).get("channel_id");
        if (!cid) return;
        try {
          await wPost("/api/wellness/admin/exempt", { channel_id: cid });
          toast("Exempt channel added.");
          mount(container);
        }
        catch (err) { toast(`Couldn’t add that exempt channel — ${err.message}`, "error"); }
      });
    }
  }, { errorMsg: "Couldn’t load the wellness admin settings." });
}
