import { api, esc } from "../api.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>Chilling Effect</h2>
        <div class="subtitle">Members whose arrival in a channel correlates with others going quiet</div>
      </header>
      <div class="controls">
        <label>Lookback days
          <input type="number" data-control="lookback" min="1" max="365" value="${initialParams.lookback_days || 30}" />
        </label>
        <label>Entry gap (min)
          <input type="number" data-control="gap" min="5" max="1440" value="${initialParams.entry_gap_minutes || 60}" />
        </label>
        <label>Window (min)
          <input type="number" data-control="window" min="5" max="240" value="${initialParams.window_minutes || 30}" />
        </label>
        <label>Channel
          <input type="text" data-control="channel" placeholder="optional channel id" />
        </label>
      </div>
      <div data-status></div>
      <div data-results style="margin-top:12px;"></div>
    </div>
  `;
  container.innerHTML = html;

  const lookbackEl = container.querySelector('[data-control="lookback"]');
  const gapEl = container.querySelector('[data-control="gap"]');
  const windowEl = container.querySelector('[data-control="window"]');
  const channelEl = container.querySelector('[data-control="channel"]');
  const statusEl = container.querySelector('[data-status]');
  const resultsEl = container.querySelector('[data-results]');

  function fmtTs(ts) { return new Date(ts * 1000).toLocaleString(); }

  async function refresh() {
    const params = {
      lookback_days: parseInt(lookbackEl.value) || 30,
      entry_gap_minutes: parseInt(gapEl.value) || 60,
      window_minutes: parseInt(windowEl.value) || 30,
    };
    if (channelEl.value.trim()) params.channel_id = channelEl.value.trim();
    statusEl.textContent = "Analysing… (may take a moment for long lookbacks)";
    resultsEl.textContent = "";
    try {
      const data = await api("/api/reports/chilling-effect", params);
      statusEl.textContent = `${data.total_events} entry events across ${data.channel_count} channel(s) over ${data.lookback_days}d.`;
      if (!data.ranked.length) {
        resultsEl.textContent = "No chilling-effect candidates found.";
        return;
      }
      const blocks = data.ranked.slice(0, 25).map((p) => {
        const samples = p.sample_events.map((ev) => {
          const victims = ev.victims.map((v) => `<li><strong>${esc(v.user_name || v.user_id)}</strong> at ${fmtTs(v.last_message_ts)}: ${esc(v.last_message_preview)}</li>`).join("");
          return `<details><summary>#${esc(ev.channel_name || ev.channel_id)} at ${fmtTs(ev.entry_ts)} — "${esc(ev.entry_preview)}"</summary><ul>${victims}</ul></details>`;
        }).join("");
        return `<div style="border:1px solid var(--border); border-radius:6px; padding:8px; margin-bottom:8px;"><strong>${esc(p.user_name || p.user_id)}</strong> — ${p.silence_count} entries / ${p.total_victims} victims${samples}</div>`;
      }).join("");
      resultsEl.innerHTML = blocks;
    } catch (err) {
      statusEl.textContent = `Error: ${err.message}`;
    }
  }
  lookbackEl.addEventListener("change", refresh);
  gapEl.addEventListener("change", refresh);
  windowEl.addEventListener("change", refresh);
  channelEl.addEventListener("change", refresh);
  refresh();
  return { unmount() {} };
}
