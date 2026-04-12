import { wGet, esc } from "../wellness-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading history...</div></div>`;

  (async () => {
    let d;
    try { d = await wGet("/api/wellness/history"); } catch (e) {
      container.querySelector(".panel").innerHTML = `<div class="error">${e.message}</div>`;
      return;
    }

    if (!d.reports.length) {
      container.querySelector(".panel").innerHTML = `
        <header><h2>Weekly Reports</h2></header>
        <div class="w-empty">No weekly reports yet. Reports are generated each Sunday.</div>`;
      return;
    }

    const reportsHTML = d.reports.map(r => {
      const s = r.summary;
      const statsHTML = s.total_messages !== undefined
        ? `<div class="w-report-stats">
            <span>${s.total_messages || 0} msgs</span>
            <span>${s.caps_hit || 0} cap hits</span>
            <span>${s.blackout_violations || 0} blackout violations</span>
            <span>${Math.round((s.compliance_pct || 0) * 100)}% compliance</span>
          </div>`
        : "";
      return `
        <div class="w-report">
          <div class="w-report-header">
            <strong>Week ${r.iso_week}, ${r.iso_year}</strong>
            <span class="w-chip w-chip-dim">${esc(r.week_start)}</span>
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
