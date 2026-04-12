import { wGet, wPost, wPut, wDelete, esc } from "../wellness-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading blackouts...</div></div>`;

  async function load() {
    let d;
    try { d = await wGet("/api/wellness/blackouts"); } catch (e) {
      container.querySelector(".panel").innerHTML = `<div class="error">${e.message}</div>`;
      return;
    }

    const listHTML = d.blackouts.length
      ? d.blackouts.map(b => `
          <div class="w-row${b.enabled ? "" : " w-row-muted"}" data-bo-id="${b.id}">
            <div class="w-row-main">
              <strong>${esc(b.name)}</strong>
              <span class="w-chip">${esc(b.start_str)} &rarr; ${esc(b.end_str)}</span>
              <span class="w-chip w-chip-dim">${esc(b.days_str)}</span>
            </div>
            <div class="w-row-actions">
              <label class="w-toggle">
                <input type="checkbox" data-toggle-bo="${b.id}" ${b.enabled ? "checked" : ""} />
                <span class="w-toggle-track"></span>
              </label>
              <button class="btn-danger" data-del-bo="${b.id}">Remove</button>
            </div>
          </div>
        `).join("")
      : '<div class="w-empty">No blackouts yet. Pick a template or build one below.</div>';

    const tplHTML = d.templates.map(t => {
      const labels = {
        night_owl: "Night Owl<br><small>23:00-07:00 daily</small>",
        work_hours: "Work Hours<br><small>09:00-17:00 weekdays</small>",
        school_hours: "School Hours<br><small>08:00-15:00 weekdays</small>",
        weekend_detox: "Weekend Detox<br><small>All weekend</small>",
      };
      return `<button class="w-tpl-btn" data-tpl="${t}">${labels[t] || t}</button>`;
    }).join("");

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Blackout Windows</h2>
        <div class="subtitle">Set times when the bot will encourage you to step away</div>
      </header>
      <div class="w-list">${listHTML}</div>

      <section class="w-section">
        <h3>Quick Templates</h3>
        <div class="w-tpl-grid">${tplHTML}</div>
      </section>

      <section class="w-section">
        <h3>Custom Blackout</h3>
        <form data-add-form class="w-form">
          <div class="field">
            <label>Name</label>
            <input type="text" name="name" required maxlength="40" placeholder="e.g. focus block" />
          </div>
          <div class="w-form-row">
            <div class="field">
              <label>Start</label>
              <input type="time" name="start" required value="22:00" />
            </div>
            <div class="field">
              <label>End</label>
              <input type="time" name="end" required value="07:00" />
            </div>
          </div>
          <fieldset class="w-day-picker">
            <legend>Days</legend>
            <label><input type="checkbox" name="days" value="mon" checked />Mon</label>
            <label><input type="checkbox" name="days" value="tue" checked />Tue</label>
            <label><input type="checkbox" name="days" value="wed" checked />Wed</label>
            <label><input type="checkbox" name="days" value="thu" checked />Thu</label>
            <label><input type="checkbox" name="days" value="fri" checked />Fri</label>
            <label><input type="checkbox" name="days" value="sat" checked />Sat</label>
            <label><input type="checkbox" name="days" value="sun" checked />Sun</label>
          </fieldset>
          <div><button type="submit">Add Blackout</button><span data-add-status></span></div>
        </form>
      </section>
    `;

    // Toggle
    container.querySelectorAll("[data-toggle-bo]").forEach(cb => {
      cb.addEventListener("change", async () => {
        try { await wPut(`/api/wellness/blackouts/${cb.dataset.toggleBo}/toggle`, { enabled: cb.checked }); }
        catch (e) { alert(e.message); cb.checked = !cb.checked; }
      });
    });

    // Delete
    container.querySelectorAll("[data-del-bo]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!confirm("Remove this blackout?")) return;
        try { await wDelete(`/api/wellness/blackouts/${btn.dataset.delBo}`); load(); }
        catch (e) { alert(e.message); }
      });
    });

    // Templates
    container.querySelectorAll("[data-tpl]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try { await wPost("/api/wellness/blackouts", { template: btn.dataset.tpl }); load(); }
        catch (e) { alert(e.message); }
      });
    });

    // Custom add
    const form = container.querySelector("[data-add-form]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const days = Array.from(form.querySelectorAll("[name=days]:checked")).map(c => c.value);
      try {
        await wPost("/api/wellness/blackouts", {
          name: fd.get("name"), start: fd.get("start"), end: fd.get("end"), days,
        });
        load();
      } catch (err) { alert(err.message); }
    });
  }

  load();
}
