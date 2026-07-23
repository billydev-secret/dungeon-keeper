import { wGet, wPost, wPut, wDelete, esc } from "../wellness-helpers.js";
import { toast, confirmDialog } from "../ui.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading your blackout windows…")}</div>`;

  async function load() {
    let d;
    try { d = await wGet("/api/wellness/blackouts"); } catch (e) {
      container.querySelector(".panel").innerHTML =
        renderError(`Couldn’t load your blackout windows — try again. (${e.message})`);
      return;
    }

    const listHTML = d.blackouts.length
      ? d.blackouts.map(b => `
          <div class="w-row${b.enabled ? "" : " w-row-muted"}" data-bo-id="${b.id}">
            <div class="w-row-main">
              <strong>${esc(b.name)}</strong>
              <span class="chip">${esc(b.start_str)} &rarr; ${esc(b.end_str)}</span>
              <span class="chip chip-neutral">${esc(b.days_str)}</span>
            </div>
            <div class="w-row-actions">
              <label class="w-toggle">
                <input type="checkbox" data-toggle-bo="${b.id}" ${b.enabled ? "checked" : ""}
                       aria-label="Turn the ${esc(b.name)} blackout on or off" />
                <span class="w-toggle-track"></span>
              </label>
              <button class="btn btn-sm btn-danger" data-del-bo="${b.id}">Remove</button>
            </div>
          </div>
        `).join("")
      : renderEmpty("No blackout windows yet. Pick a quick template below, or build your own — Dungeon Keeper will nudge you to step away during the hours you choose.");

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

      <div class="section-label">Quick Templates</div>
      <div class="w-tpl-grid">${tplHTML}</div>

      <div class="section-label">Custom Blackout</div>
      <form data-add-form class="form">
          <div class="field">
            <label>Name
              <input type="text" name="name" required maxlength="40" placeholder="e.g. Focus Block" />
            </label>
          </div>
          <div class="field-row">
            <div class="field">
              <label>Starts At
                <input type="time" name="start" required value="22:00" />
              </label>
            </div>
            <div class="field">
              <label>Ends At
                <input type="time" name="end" required value="07:00" />
              </label>
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
          <div><button type="submit" class="btn btn-primary">Add Blackout</button><span data-add-status></span></div>
      </form>
    `;

    // Toggle
    container.querySelectorAll("[data-toggle-bo]").forEach(cb => {
      cb.addEventListener("change", async () => {
        try {
          await wPut(`/api/wellness/blackouts/${cb.dataset.toggleBo}/toggle`, { enabled: cb.checked });
          toast(cb.checked ? "Blackout enabled" : "Blackout disabled");
        }
        catch (e) { toast(`Couldn’t change that blackout — ${e.message}`, "error"); cb.checked = !cb.checked; }
      });
    });

    // Delete
    container.querySelectorAll("[data-del-bo]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const ok = await confirmDialog(
          "Remove this blackout window? Dungeon Keeper will stop nudging you during those hours.",
          { title: "Remove Blackout", danger: true, confirmLabel: "Remove" },
        );
        if (!ok) return;
        try { await wDelete(`/api/wellness/blackouts/${btn.dataset.delBo}`); load(); }
        catch (e) { toast(`Couldn’t remove that blackout — ${e.message}`, "error"); }
      });
    });

    // Templates
    container.querySelectorAll("[data-tpl]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try { await wPost("/api/wellness/blackouts", { template: btn.dataset.tpl }); toast("Blackout added"); load(); }
        catch (e) { toast(`Couldn’t add that blackout — ${e.message}`, "error"); }
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
        toast("Blackout added");
        load();
      } catch (err) { toast(`Couldn’t add that blackout — ${err.message}`, "error"); }
    });
  }

  load();
}
