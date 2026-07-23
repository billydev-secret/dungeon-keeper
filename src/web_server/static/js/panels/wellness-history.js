import { wGet, esc } from "../wellness-helpers.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading your weekly reports…")}</div>`;

  (async () => {
    let d;
    try { d = await wGet("/api/wellness/history"); } catch (e) {
      container.querySelector(".panel").innerHTML =
        renderError(`Couldn’t load your weekly reports — try again. (${e.message})`);
      return;
    }

    if (!d.reports.length) {
      container.querySelector(".panel").innerHTML = `
        <header><h2>Weekly Reports</h2></header>
        ${renderEmpty("No weekly reports yet. Dungeon Keeper writes one every Sunday once you have a full week of wellness history.")}`;
      return;
    }

    const reportsHTML = d.reports.map(r => {
      const s = r.summary;
      const statsHTML = s.total_messages !== undefined
        ? `<div class="w-report-stats">
            <span>${s.total_messages || 0} messages</span>
            <span>${s.caps_hit || 0} caps hit</span>
            <span>${s.blackout_violations || 0} blackout violations</span>
            <span>${Math.round((s.compliance_pct || 0) * 100)}% compliance</span>
          </div>`
        : "";
      return `
        <div class="w-report">
          <div class="w-report-header">
            <strong>Week ${r.iso_week}, ${r.iso_year}</strong>
            <span class="chip chip-neutral">${esc(r.week_start)}</span>
          </div>
          ${statsHTML}
          ${r.ai_text ? `<div class="w-report-ai">${esc(r.ai_text)}</div>` : ""}
        </div>
      `;
    }).join("");

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Weekly Reports</h2>
        <div class="subtitle">Your wellness history, one week at a time</div>
      </header>
      <div class="w-list">${reportsHTML}</div>
    `;
  })();
}
