import { wGet, wPost, wPut, wDelete, esc, showStatus } from "../wellness-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading caps...</div></div>`;

  async function load() {
    let d;
    try { d = await wGet("/api/wellness/caps"); } catch (e) {
      container.querySelector(".panel").innerHTML = `<div class="error">${e.message}</div>`;
      return;
    }

    const capsHTML = d.caps.length
      ? d.caps.map(c => `
          <div class="w-row" data-cap-id="${c.id}">
            <div class="w-row-main">
              <strong>${esc(c.label)}</strong>
              <span class="w-chip">${c.scope}</span>
              <span class="w-chip">${c.window}</span>
              <span class="w-chip">${c.limit} msgs</span>
              ${c.exclude_exempt ? '<span class="w-chip w-chip-dim">excl. exempt</span>' : ""}
            </div>
            <div class="w-row-actions">
              <input type="number" min="1" value="${c.limit}" style="width:70px" data-edit-limit />
              <button data-save-cap="${c.id}">Save</button>
              <button class="btn-danger" data-del-cap="${c.id}">Remove</button>
              <span data-cap-status="${c.id}"></span>
            </div>
          </div>
        `).join("")
      : '<div class="w-empty">No caps yet. Add one below.</div>';

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Message Caps</h2>
        <div class="subtitle">Set limits on how many messages you send per window</div>
      </header>
      <div class="w-list">${capsHTML}</div>

      <section class="w-section">
        <h3>Add Cap</h3>
        <form data-add-form class="w-form">
          <div class="field">
            <label>Label</label>
            <input type="text" name="label" required maxlength="40" placeholder="e.g. daily limit" />
          </div>
          <div class="w-form-row">
            <div class="field">
              <label>Scope</label>
              <select name="scope">${d.scopes.map(s => `<option value="${s}">${s}</option>`).join("")}</select>
            </div>
            <div class="field">
              <label>Window</label>
              <select name="window">${d.windows.map(w => `<option value="${w}">${w}</option>`).join("")}</select>
            </div>
            <div class="field">
              <label>Limit</label>
              <input type="number" name="limit" min="1" value="50" required />
            </div>
          </div>
          <div class="field">
            <label><input type="checkbox" name="exclude_exempt" checked /> Exclude exempt channels</label>
          </div>
          <div><button type="submit">Add Cap</button><span data-add-status></span></div>
        </form>
      </section>
    `;

    // Save edits
    container.querySelectorAll("[data-save-cap]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.saveCap;
        const row = container.querySelector(`[data-cap-id="${id}"]`);
        const limit = parseInt(row.querySelector("[data-edit-limit]").value, 10);
        const st = container.querySelector(`[data-cap-status="${id}"]`);
        try { await wPut(`/api/wellness/caps/${id}`, { limit }); showStatus(st, true); }
        catch (e) { showStatus(st, false, e.message); }
      });
    });

    // Delete
    container.querySelectorAll("[data-del-cap]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!confirm("Remove this cap?")) return;
        try { await wDelete(`/api/wellness/caps/${btn.dataset.delCap}`); load(); }
        catch (e) { alert(e.message); }
      });
    });

    // Add form
    const form = container.querySelector("[data-add-form]");
    const addSt = container.querySelector("[data-add-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await wPost("/api/wellness/caps", {
          label: fd.get("label"),
          scope: fd.get("scope"),
          window: fd.get("window"),
          limit: parseInt(fd.get("limit"), 10),
          exclude_exempt: form.querySelector("[name=exclude_exempt]").checked,
        });
        load();
      } catch (err) { showStatus(addSt, false, err.message); }
    });
  }

  load();
}
