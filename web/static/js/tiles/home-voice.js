import { esc } from "./tile-helpers.js";

export function renderTile(el, d) {
  const voiceHTML = d.voice_channels.length
    ? d.voice_channels.map((vc) => `
        <div class="home-voice-ch">
          <div class="home-voice-ch-name">${esc(vc.channel_name)}</div>
          <div class="home-voice-ch-members">${vc.members.map((m) => esc(m.user_name)).join(", ")}</div>
        </div>
      `).join("")
    : '<div class="home-dim">No one in voice</div>';

  el.innerHTML = `
    <div class="home-card-label">In Voice Now</div>
    ${voiceHTML}
  `;
}
