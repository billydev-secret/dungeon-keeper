import { esc } from "./tile-helpers.js";
import { api, apiPost } from "../api.js";
// The assistant's name is per-guild branding (Config → Branding). The tile's
// payload carries the resolved name; this is the same fallback the nav and the
// Help panel use, for a payload that predates the field or a failed lookup.
import { DEFAULT_ASSISTANT_NAME } from "../panels/help-sections.js";

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
            <button type="button" class="sugg-dismiss" data-dismiss="${esc(s.slug)}"
              title="Not for this server — clear this suggestion"
              aria-label="Dismiss ${esc(s.label)}">&times;</button>
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
    <div class="sugg-foot">Ask ${esc(d?.assistant_name || DEFAULT_ASSISTANT_NAME)} to set any of these up for you &middot;
      dismissed rows come back from the <a href="#/config-advisor">AI Assistant</a> page.</div>
  `;

  // The whole tile is a click-through to the AI Assistant page (widget-grid
  // binds that on the card), so every dismiss click has to stop there or the
  // admin gets navigated away mid-action.
  el.querySelectorAll("[data-dismiss]").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      btn.disabled = true;
      try {
        await apiPost(`/api/help/suggestions/${encodeURIComponent(btn.dataset.dismiss)}/dismiss`);
      } catch (_) {
        btn.disabled = false;
        return;
      }
      // Re-fetch rather than splicing the row out: dismissing one suggestion
      // promotes the next one behind it, which is the point of dismissing.
      try {
        renderTile(el, await api("/api/help/suggestions", { limit: 3 }));
      } catch (_) {
        btn.closest(".sugg-row")?.remove();
      }
    });
  });
}
