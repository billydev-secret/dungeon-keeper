import { esc } from "./tile-helpers.js";

// Cheapest win first — mirrors advisor_gaps.STATUS_ORDER.
// Keep labels short — they sit beside the feature name in a flex row that has
// to survive a 390px phone.
const STATUS = {
  ready_but_off: { label: "Just switch on", cls: "sugg-ready" },
  partial: { label: "Half set up", cls: "sugg-partial" },
  unconfigured: { label: "Not set up", cls: "sugg-unset" },
};

export function renderTile(el, d) {
  const items = d?.suggestions || [];
  if (!items.length) {
    el.innerHTML = `
      <div class="home-card-label">Suggested setup</div>
      <div class="home-dim">Everything I track is already set up. Nice.</div>
    `;
    return;
  }

  const rows = items
    .map((s) => {
      const st = STATUS[s.status] || STATUS.unconfigured;
      const needs = (s.missing || []).map((m) => m.label);
      const needsLine = needs.length
        ? `<div class="sugg-needs">Still needs: ${esc(needs.join(", "))}</div>`
        : "";
      return `
        <div class="sugg-row">
          <div class="sugg-head">
            <span class="sugg-name">${esc(s.label)}</span>
            <span class="sugg-badge ${st.cls}">${esc(st.label)}</span>
          </div>
          <div class="sugg-blurb">${esc(s.blurb)}</div>
          ${needsLine}
          <div class="sugg-panel">${esc(s.panel)}</div>
        </div>
      `;
    })
    .join("");

  el.innerHTML = `
    <div class="home-card-label">Suggested setup</div>
    ${rows}
    <div class="sugg-foot">Ask Billy-bot to set any of these up for you.</div>
  `;
}
