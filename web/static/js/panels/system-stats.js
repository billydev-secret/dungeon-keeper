// System stats panel — live network, CPU, memory, disk from the host OS.
import { api, esc } from "../api.js";

function fmtBytes(b) {
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
  if (b < 1024 * 1024 * 1024) return (b / (1024 * 1024)).toFixed(1) + " MB";
  return (b / (1024 * 1024 * 1024)).toFixed(2) + " GB";
}

function fmtRate(bps) {
  if (bps < 1024) return bps.toFixed(0) + " B/s";
  if (bps < 1024 * 1024) return (bps / 1024).toFixed(1) + " KB/s";
  return (bps / (1024 * 1024)).toFixed(2) + " MB/s";
}

function fmtUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const parts = [];
  if (d) parts.push(d + "d");
  if (h) parts.push(h + "h");
  parts.push(m + "m");
  return parts.join(" ");
}

function pctBar(pct, color) {
  return `<div style="background:var(--bg);border-radius:4px;height:8px;overflow:hidden;margin-top:4px">
    <div style="width:${pct}%;height:100%;background:${color};border-radius:4px"></div>
  </div>`;
}

function pctColor(pct) {
  if (pct < 60) return "var(--success)";
  if (pct < 85) return "var(--warning)";
  return "var(--danger)";
}

function renderStats(container, data) {
  const cpuColor = pctColor(data.cpu_percent);
  const memColor = pctColor(data.memory.percent);
  const diskColor = pctColor(data.disk.percent);

  let ifaceRows = "";
  for (const iface of data.interfaces) {
    // Skip loopback and inactive interfaces
    if (iface.bytes_sent === 0 && iface.bytes_recv === 0) continue;
    ifaceRows += `<tr>
      <td style="white-space:nowrap">${iface.name}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${fmtBytes(iface.bytes_sent)}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${fmtBytes(iface.bytes_recv)}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${fmtRate(iface.send_rate)}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${fmtRate(iface.recv_rate)}</td>
      <td style="text-align:right;color:${iface.errin + iface.errout > 0 ? 'var(--danger)' : 'var(--text-dim)'}">${iface.errin + iface.errout}</td>
    </tr>`;
  }

  container.innerHTML = `<div class="panel" style="overflow-y:auto">
    <header>
      <h2>System Stats</h2>
      <div class="subtitle">Host OS &mdash; uptime ${fmtUptime(data.uptime)}</div>
    </header>

    <div class="home-grid" style="margin-bottom:20px">
      <div class="home-card">
        <div class="home-card-label">CPU</div>
        <div class="home-card-big">${data.cpu_percent.toFixed(1)}%</div>
        ${pctBar(data.cpu_percent, cpuColor)}
      </div>
      <div class="home-card">
        <div class="home-card-label">Memory</div>
        <div class="home-card-big">${data.memory.percent.toFixed(1)}%</div>
        <div class="home-card-sub">${fmtBytes(data.memory.used)} / ${fmtBytes(data.memory.total)}</div>
        ${pctBar(data.memory.percent, memColor)}
      </div>
      <div class="home-card">
        <div class="home-card-label">Disk</div>
        <div class="home-card-big">${data.disk.percent.toFixed(1)}%</div>
        <div class="home-card-sub">${fmtBytes(data.disk.used)} / ${fmtBytes(data.disk.total)}</div>
        ${pctBar(data.disk.percent, diskColor)}
      </div>
      <div class="home-card">
        <div class="home-card-label">Network totals</div>
        <div style="display:flex;gap:20px;margin-top:4px">
          <div>
            <div style="font-size:11px;color:var(--text-dim)">Sent</div>
            <div style="font-size:18px;font-weight:700">${fmtBytes(data.network.total_bytes_sent)}</div>
            <div style="font-size:12px;color:var(--accent)">${fmtRate(data.network.send_rate)}</div>
          </div>
          <div>
            <div style="font-size:11px;color:var(--text-dim)">Received</div>
            <div style="font-size:18px;font-weight:700">${fmtBytes(data.network.total_bytes_recv)}</div>
            <div style="font-size:12px;color:var(--accent)">${fmtRate(data.network.recv_rate)}</div>
          </div>
        </div>
      </div>
    </div>

    <h3 style="font-size:15px;margin:0 0 10px">Network interfaces</h3>
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>Interface</th>
            <th style="text-align:right">Sent</th>
            <th style="text-align:right">Recv</th>
            <th style="text-align:right">Send rate</th>
            <th style="text-align:right">Recv rate</th>
            <th style="text-align:right">Errors</th>
          </tr>
        </thead>
        <tbody>${ifaceRows || '<tr><td colspan="6" class="empty">No active interfaces</td></tr>'}</tbody>
      </table>
    </div>
  </div>`;
}

export function mount(container) {
  let timer = null;
  let alive = true;

  async function poll() {
    try {
      const data = await api("/api/system/stats");
      if (!data || !alive) return;
      renderStats(container, data);
    } catch (err) {
      container.innerHTML = `<div class="panel"><div class="error">Failed to load system stats: ${esc(err.message)}</div></div>`;
    }
    if (alive) timer = setTimeout(poll, 3000);
  }

  container.innerHTML = `<div class="panel"><div class="empty">Loading system stats...</div></div>`;
  poll();

  return {
    unmount() {
      alive = false;
      if (timer) clearTimeout(timer);
    },
  };
}
