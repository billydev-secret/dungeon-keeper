import { wGet, wPost, wDelete, esc, showStatus } from "../wellness-helpers.js";
import { toast } from "../ui.js";
import { guardForm } from "../config-helpers.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading wellness settings…")}</div>`;

  (async () => {
    let dash, defaults, users, exempt;
    try {
      [dash, defaults, users, exempt] = await Promise.all([
        wGet("/api/wellness/admin/dashboard"),
        wGet("/api/wellness/admin/defaults"),
        wGet("/api/wellness/admin/users"),
        wGet("/api/wellness/admin/exempt"),
      ]);
    } catch (e) {
      container.querySelector(".panel").innerHTML =
        renderError(`Couldn’t load the wellness admin panel — try again. (${e.message})`);
      return;
    }

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
              ${defaults.enforcement_levels.map(e => `<option value="${e}"${e === cfg.default_enforcement ? " selected" : ""}>${e}</option>`).join("")}
            </select>
          </label>
          <div class="field-hint">The starting level for members who opt in. Each member can change their own afterwards.</div>
        </div>
        <div class="field">
          <label>Crisis Resource URL
            <input type="url" name="crisis_resource_url" value="${esc(cfg.crisis_resource_url || "")}" placeholder="https://findahelpline.com/" />
          </label>
          <div class="field-hint">Linked from every member’s wellness page. Leave blank to hide the link.</div>
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
              <button class="btn btn-sm btn-danger" data-unexempt="${ch.id}">Remove</button>
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
          crisis_resource_url: fd.get("crisis_resource_url"),
        });
        showStatus(dStatus, true);
      } catch (err) { showStatus(dStatus, false, `Couldn’t save — ${err.message}`); }
    });

    // Pause/Resume handlers
    container.querySelectorAll("[data-pause-uid]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await wPost(`/api/wellness/admin/users/${btn.dataset.pauseUid}/pause`, { minutes: 60 });
          btn.closest("tr").querySelector("td:nth-child(4)").innerHTML = '<span class="chip chip-warning">Paused</span>';
          btn.textContent = "Paused";
          btn.disabled = true;
        } catch (e) { toast(`Couldn’t pause that member — ${e.message}`, "error"); }
      });
    });
    container.querySelectorAll("[data-resume-uid]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await wPost(`/api/wellness/admin/users/${btn.dataset.resumeUid}/resume`, {});
          btn.closest("tr").querySelector("td:nth-child(4)").innerHTML = '<span class="chip chip-success">Active</span>';
          btn.textContent = "Resumed";
          btn.disabled = true;
        } catch (e) { toast(`Couldn’t resume that member — ${e.message}`, "error"); }
      });
    });

    // Exempt remove
    container.querySelectorAll("[data-unexempt]").forEach(btn => {
      btn.addEventListener("click", async () => {
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
  })();
}
