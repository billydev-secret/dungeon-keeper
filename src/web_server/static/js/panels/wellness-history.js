import { wGet, esc } from "../wellness-helpers.js";
import { renderLoading, renderEmpty } from "../states.js";
import { mountAsync } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading your weekly reports…")}</div>`;

  return mountAsync(container, async () => {
    // Let the rejection reach mountAsync: it draws the error state *and* a
    // working Try again button. Catching it here rendered a dead-end error
    // and made this panel's own errorMsg unreachable. wellness-caps.js
    // documents the same reasoning where it rethrows on first load.
    const d = await wGet("/api/wellness/history");

    if (!d.reports.length) {
      container.querySelector(".panel").innerHTML = `
        <header><h2>Weekly Reports</h2></header>
        ${renderEmpty("No weekly reports yet. Dungeon Keeper writes one every Sunday once you have a full week of wellness history.")}`;
      return;
    }

    const reportsHTML = d.reports.map(r => {
      const s = r.summary;
      // compliance_pct is stored 0-100 (never re-scale it — a 57 rendered as
      // 5700% once), and the stats gate matches fields the summary really has.
      const statsHTML = s.clean_days !== undefined
        ? `<div class="w-report-stats">
            <span>${s.clean_days || 0}/${s.tracked_days ?? 7} clean days</span>
            <span>${Math.round(s.compliance_pct || 0)}% compliance</span>
            ${s.violation_days ? `<span>${s.violation_days} slip day${s.violation_days === 1 ? "" : "s"}</span>` : ""}
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
  }, { errorMsg: "Couldn’t load your weekly reports." });
}
