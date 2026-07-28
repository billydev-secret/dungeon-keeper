import { esc } from "./tile-helpers.js";

// Counterpart to setup-suggestions: that tile lists what isn't set up, this
// one lists what *is* set up and silently doesn't work. Discord gives no
// signal for the second kind — deleting a role takes its channel permission
// overwrites with it, so a channel can go dark with no config change and
// nothing in the audit log naming it.
const CODE_LABEL = {
  missing: "Channel gone",
  wrong_type: "Wrong kind of channel",
  bot_cannot_post: "I can't post there",
  nobody_can_view: "Nobody can see it",
};

export function renderTile(el, data) {
  if (!data || data.available === false) {
    el.innerHTML = `
      <div class="home-card-label">Configuration problems</div>
      <div class="home-dim">Can't check right now — the bot isn't connected.</div>
    `;
    return;
  }

  const issues = data.issues || [];
  if (!issues.length) {
    const n = data.checked || 0;
    el.innerHTML = `
      <div class="home-card-label">Configuration problems</div>
      <div class="home-dim">
        All ${n} configured channel${n === 1 ? "" : "s"} are working.
      </div>
    `;
    return;
  }

  const rows = issues
    .map((i) => {
      const where = i.channel_name ? `#${esc(i.channel_name)}` : `#${esc(i.channel_id)}`;
      const label = CODE_LABEL[i.code] || "Problem";
      // Several settings can point at one channel; one fix clears them all.
      const settings = (i.settings || []).map((s) => s.label).join(", ");
      const panels = [...new Set((i.settings || []).map((s) => s.panel))].join(", ");
      return `
        <div class="sugg-row">
          <div class="sugg-head">
            <span class="sugg-name">${where}</span>
            <span class="sugg-badge sugg-unset">${esc(label)}</span>
          </div>
          <div class="sugg-blurb">${esc(i.message)}</div>
          <div class="sugg-needs">Used for: ${esc(settings)}</div>
          <div class="sugg-panel">${esc(panels)}</div>
        </div>
      `;
    })
    .join("");

  el.innerHTML = `
    <div class="home-card-label">Configuration problems</div>
    ${rows}
  `;
}
