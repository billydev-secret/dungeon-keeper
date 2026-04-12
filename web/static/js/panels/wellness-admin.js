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
      <div class="w-grid">
        <div class="w-card">
          <div class="w-card-label">Active Users</div>
          <div class="w-card-big">${dash.active_count}</div>
        </div>
        <div class="w-card">
          <div class="w-card-label">Exempt Channels</div>
          <div class="w-card-big">${dash.exempt_channels.length}</div>
        </div>
        <div class="w-card">
          <div class="w-card-label">Default Enforcement</div>
          <div class="w-card-big">${esc(dash.config?.default_enforcement || "—")}</div>
        </div>
      </div>`;

    // Defaults form
    const cfg = defaults.config || {};
    const defaultsHTML = `
      <section class="w-section">
        <h3>Server Defaults</h3>
        <form data-defaults-form class="w-form">
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
          <div><button type="submit">Save</button><span data-defaults-status></span></div>
        </form>
      </section>`;

    // Users table
    const usersHTML = users.users.length ? `
      <section class="w-section">
        <h3>Active Users</h3>
        <table class="w-table">
          <thead><tr><th>User</th><th>Timezone</th><th>Enforcement</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody>
            ${users.users.map(u => `
              <tr data-uid="${u.user_id}">
                <td>${esc(u.name)}</td>
                <td>${esc(u.timezone)}</td>
                <td>${esc(u.enforcement_level)}</td>
                <td>${u.is_paused ? '<span class="w-badge w-badge-warn">Paused</span>' : '<span class="w-badge w-badge-ok">Active</span>'}</td>
                <td>
                  ${u.is_paused
                    ? `<button data-resume-uid="${u.user_id}">Resume</button>`
                    : `<button data-pause-uid="${u.user_id}">Pause 60m</button>`}
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </section>` : "";

    // Exempt channels
    const exemptListHTML = exempt.exempt.length
      ? exempt.exempt.map(ch => `
          <div class="w-row">
            <div class="w-row-main">#${esc(ch.name)}</div>
            <div class="w-row-actions">
              <button class="btn-danger" data-unexempt="${ch.id}">Remove</button>
            </div>
          </div>
        `).join("")
      : '<div class="w-empty">No exempt channels.</div>';

    const channelOptsHTML = exempt.channel_options.length
      ? exempt.channel_options.map(c => `<option value="${c.id}">#${esc(c.name)}</option>`).join("")
      : '<option value="">No channels available</option>';

    const exemptHTML = `
      <section class="w-section">
        <h3>Exempt Channels</h3>
        <div class="w-list">${exemptListHTML}</div>
        <form data-exempt-form class="w-form w-inline-form" style="margin-top:12px;">
          <select name="channel_id">${channelOptsHTML}</select>
          <button type="submit">Add</button>
        </form>
      </section>`;

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
          btn.closest("tr").querySelector("td:nth-child(4)").innerHTML = '<span class="w-badge w-badge-warn">Paused</span>';
          btn.textContent = "Done";
          btn.disabled = true;
        } catch (e) { alert(e.message); }
      });
    });
    container.querySelectorAll("[data-resume-uid]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await wPost(`/api/wellness/admin/users/${btn.dataset.resumeUid}/resume`, {});
          btn.closest("tr").querySelector("td:nth-child(4)").innerHTML = '<span class="w-badge w-badge-ok">Active</span>';
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
