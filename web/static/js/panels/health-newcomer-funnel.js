import { api } from "../api.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading newcomer data...</div></div>';

  async function load() {
    const d = await api("/api/health/newcomer-funnel");
    const panel = container.querySelector(".panel");

    const f = d.funnel || {};
    const stages = [
      { label: "Joined", count: f.joined, desc: "New members (90d)" },
      { label: "First Message", count: f.first_message, desc: "Sent at least one message" },
      { label: "Got a Reply", count: f.first_reply, desc: "Someone replied to them" },
      { label: "3+ Channels", count: f.three_channels, desc: "Visited 3 or more channels" },
      { label: "D7 Return", count: f.d7_return, desc: "Active 7+ days after joining" },
    ];
    const max = Math.max(f.joined, 1);
    const funnelHTML = stages.map((s, i) => {
      const pct = Math.round((s.count / max) * 100);
      const convRate = i > 0 && stages[i - 1].count ? Math.round(s.count / stages[i - 1].count * 100) : 100;
      return `<div class="funnel-stage-full">
        <div class="funnel-bar-full" style="width:${pct}%">
          <span class="funnel-count">${s.count}</span>
          <span class="funnel-conv">${i > 0 ? convRate + "%" : ""}</span>
        </div>
        <div class="funnel-meta">
          <span class="funnel-label-full">${s.label}</span>
          <span class="funnel-desc">${s.desc}</span>
        </div>
      </div>`;
    }).join("");

    const ttfm = d.time_to_first_msg || {};
    const dist = ttfm.distribution || {};
    const distHTML = Object.entries(dist).map(([k, v]) => {
      const label = k.replace(/_/g, " ").replace("under ", "<");
      return `<div class="home-rank-row"><span class="home-rank-name">${label}</span><span class="home-rank-val">${v}</span></div>`;
    }).join("");

    panel.innerHTML = `
      <header>
        <h2>Newcomer Funnel</h2>
        <div class="subtitle">Activation rate: ${d.activation_rate}%</div>
      </header>

      <details class="panel-about" style="margin:8px 0 14px;">
        <summary style="cursor:pointer; font-size:0.85rem; color:var(--text-muted, #949ba4);">About this report</summary>
        <div style="margin:6px 0 0; padding:10px 14px; background:var(--bg-secondary, #2b2d31); border-radius:6px; font-size:0.85rem; line-height:1.6; color:var(--text-muted, #949ba4);">
          Tracks new members through five onboarding milestones: joining, sending a first message, receiving a reply, visiting 3+ channels, and returning after 7 days.
          The <strong style="color:var(--text-normal, #dbdee1);">activation rate</strong> is the percentage who make it all the way through.
          Big drop-offs between steps reveal where your onboarding experience loses people.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Activation Rate</div>
          <div class="home-card-big">${d.activation_rate}%</div>
          <div class="home-card-sub">Newcomers who complete all onboarding steps</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Time to First Message</div>
          <div class="home-card-big">${ttfm.median_hours || 0}h</div>
          <div class="home-card-sub">Median (target: &lt;4h)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">First Response Latency</div>
          <div class="home-card-big">${(d.first_response_latency || {}).median_minutes || 0}m</div>
          <div class="home-card-sub">How fast newcomers get a reply (target: &lt;5m)</div>
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Funnel</div>
        <div class="funnel-full">${funnelHTML}</div>
      </div>

      <div class="home-card" style="margin-top:14px;">
        <div class="home-card-label">Time to First Message Distribution</div>
        ${distHTML}
      </div>
    `;
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() {} };
}
