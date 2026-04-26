import { wGet, wPost, wDelete, esc, showStatus } from "../wellness-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading wellness admin...</div></div>`;

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
      container.querySelector(".panel").innerHTML = `<div class="error">${e.message}</div>`;
      return;
    }

    // Overview cards
    const overviewHTML = `
      <div class="card-grid">
        <div class="card">
          <div class="stat-label">Active Users</div>
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
          <label>Default Enforcement</label>
          <select name="default_enforcement">
            ${defaults.enforcement_levels.map(e => `<option value="${e}"${e === cfg.default_enforcement ? " selected" : ""}>${e}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <label>Crisis Resource URL</label>
          <input type="url" name="crisis_resource_url" value="${esc(cfg.crisis_resource_url || "")}" placeholder="https://findahelpline.com/" />
        </div>
        <div><button type="submit" class="btn btn-primary">Save</button><span data-defaults-status></span></div>
      </form>`;

    // Users table
    const usersHTML = users.users.length ? `
      <div class="section-label">Active Users</div>
      <table class="w-table">
        <thead><tr><th>User</th><th>Timezone</th><th>Enforcement</th><th>Status</th><th>Actions</th></tr></thead>
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
                  : `<button class="btn btn-sm" data-pause-uid="${u.user_id}">Pause 60m</button>`}
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>` : "";

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
      : '<div class="empty">No exempt channels.</div>';

    const channelOptsHTML = exempt.channel_options.length
      ? exempt.channel_options.map(c => `<option value="${c.id}">#${esc(c.name)}</option>`).join("")
      : '<option value="">No channels available</option>';

    const exemptHTML = `
      <div class="section-label">Exempt Channels</div>
      <div class="w-list">${exemptListHTML}</div>
      <form data-exempt-form class="form w-inline-form" style="margin-top:12px;">
        <select name="channel_id">${channelOptsHTML}</select>
        <button type="submit" class="btn btn-primary">Add</button>
      </form>`;

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Wellness Admin</h2>
        <div class="subtitle">Server-wide wellness configuration</div>
      </header>
      ${overviewHTML}
      ${defaultsHTML}
      ${usersHTML}
      ${exemptHTML}
    `;

    // Defaults form handler
    const dForm = container.querySelector("[data-defaults-form]");
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
      } catch (err) { showStatus(dStatus, false, err.message); }
    });

    // Pause/Resume handlers
    container.querySelectorAll("[data-pause-uid]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await wPost(`/api/wellness/admin/users/${btn.dataset.pauseUid}/pause`, { minutes: 60 });
          btn.closest("tr").querySelector("td:nth-child(4)").innerHTML = '<span class="chip chip-warning">Paused</span>';
          btn.textContent = "Done";
          btn.disabled = true;
        } catch (e) { alert(e.message); }
      });
    });
    container.querySelectorAll("[data-resume-uid]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await wPost(`/api/wellness/admin/users/${btn.dataset.resumeUid}/resume`, {});
          btn.closest("tr").querySelector("td:nth-child(4)").innerHTML = '<span class="chip chip-success">Active</span>';
          btn.textContent = "Done";
          btn.disabled = true;
        } catch (e) { alert(e.message); }
      });
    });

    // Exempt remove
    container.querySelectorAll("[data-unexempt]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await wDelete(`/api/wellness/admin/exempt/${btn.dataset.unexempt}`);
          btn.closest(".w-row").remove();
        } catch (e) { alert(e.message); }
      });
    });

    // Exempt add
    const exForm = container.querySelector("[data-exempt-form]");
    if (exForm) {
      exForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const cid = new FormData(exForm).get("channel_id");
        if (!cid) return;
        try { await wPost("/api/wellness/admin/exempt", { channel_id: cid }); location.reload(); }
        catch (err) { alert(err.message); }
      });
    }
  })();
}
