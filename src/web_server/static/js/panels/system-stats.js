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
  if (pct < 60) return "var(--green)";
  if (pct < 85) return "var(--yellow)";
  return "var(--red)";
}

function fmtAge(seconds) {
  if (seconds == null) return "never";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
  return Math.floor(seconds / 86400) + "d ago";
}

// Backups (finding B3). Two rows, because they fail independently and protect
// against different things: the local copy covers a bad migration, and only the
// off-device copy covers the disk dying. A green local row next to a stale
// off-device one is the state worth being able to see at a glance.
function backupRow(label, age, ok, detail) {
  // The words OK / Stale, so the text tier. pctColor above stays saturated:
  // its value is a bar fill.
  const color = ok ? "var(--green-text)" : "var(--red-text)";
  return `<tr>
    <td style="white-space:nowrap">${label}</td>
    <td style="color:${color};white-space:nowrap">${ok ? "OK" : "Stale"}</td>
    <td class="num">${esc(fmtAge(age))}</td>
    <td class="num-dim">${esc(detail)}</td>
  </tr>`;
}

function renderBackups(b) {
  if (!b || b.available === false) {
    return `<div class="section-label">Backups</div>
      <div class="empty">Backup status is unavailable.</div>`;
  }

  const problems = b.problems || [];
  const codes = new Set(problems.map((p) => p.code));
  const local = b.local || {};
  const offsite = b.offsite || {};

  const localOk = !codes.has("backup_stale") && !codes.has("backup_none");
  const localDetail = `${local.count || 0} file${local.count === 1 ? "" : "s"}, ${fmtBytes(
    local.total_bytes || 0,
  )}, every ${local.interval_hours || 6}h`;

  let rows = backupRow("Local", local.age_seconds, localOk, localDetail);
  if (offsite.configured) {
    const offsiteOk = !codes.has("offsite_stale") && !codes.has("offsite_never");
    const detail = `${offsite.host || "off-device"}, kept ${
      offsite.retention_days || "?"
    } days`;
    rows += backupRow("Off-device", offsite.age_seconds, offsiteOk, detail);
  }

  const warnings = problems
    .map(
      (p) =>
        `<div class="error" style="margin-top:8px"><strong>${esc(
          p.title,
        )}</strong> — ${esc(p.message)}</div>`,
    )
    .join("");

  const failures = local.consecutive_failures || 0;
  const failNote = failures
    ? `<div class="home-dim" style="margin-top:6px">Last error: ${esc(
        local.last_error || "(not recorded)",
      )}</div>`
    : "";

  return `<div class="section-label">Backups</div>
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr><th>Copy</th><th>State</th><th class="num">Last run</th><th>Detail</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    ${warnings}
    ${failNote}`;
}

function renderStats(container, data) {
  const cpuColor = pctColor(data.cpu_percent);
  const memColor = pctColor(data.memory.percent);
  const diskColor = pctColor(data.disk.percent);

  let ifaceRows = "";
  for (const iface of data.interfaces) {
    // Skip loopback and inactive interfaces
    if (iface.bytes_sent === 0 && iface.bytes_recv === 0) continue;
    const errCount = iface.errin + iface.errout;
    ifaceRows += `<tr>
      <td style="white-space:nowrap">${iface.name}</td>
      <td class="num">${fmtBytes(iface.bytes_sent)}</td>
      <td class="num">${fmtBytes(iface.bytes_recv)}</td>
      <td class="num">${fmtRate(iface.send_rate)}</td>
      <td class="num">${fmtRate(iface.recv_rate)}</td>
      <td class="num ${errCount > 0 ? 'num-err' : 'num-dim'}">${errCount}</td>
    </tr>`;
  }

  container.innerHTML = `<div class="panel">
    <header>
      <h2>System Stats</h2>
      <div class="subtitle">The machine Dungeon Keeper runs on &mdash; up ${fmtUptime(data.uptime)}. Refreshes every 3 seconds.</div>
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
        <div class="home-card-label">Network Totals</div>
        <div style="display:flex;gap:20px;margin-top:4px">
          <div>
            <div style="font-size:11px;color:var(--ink-dim)">Sent</div>
            <div style="font-size:18px;font-weight:700">${fmtBytes(data.network.total_bytes_sent)}</div>
            <div style="font-size:12px;color:var(--gold-solid)">${fmtRate(data.network.send_rate)}</div>
          </div>
          <div>
            <div style="font-size:11px;color:var(--ink-dim)">Received</div>
            <div style="font-size:18px;font-weight:700">${fmtBytes(data.network.total_bytes_recv)}</div>
            <div style="font-size:12px;color:var(--gold-solid)">${fmtRate(data.network.recv_rate)}</div>
          </div>
        </div>
      </div>
    </div>

    <div class="section-label">Network Interfaces</div>
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>Interface</th>
            <th class="num">Sent</th>
            <th class="num">Received</th>
            <th class="num">Send Rate</th>
            <th class="num">Receive Rate</th>
            <th class="num">Errors</th>
          </tr>
        </thead>
        <tbody>${ifaceRows || '<tr><td colspan="6" class="empty">No network interface has sent or received anything yet.</td></tr>'}</tbody>
      </table>
    </div>

    ${renderBackups(data.backups)}
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
      container.innerHTML = `<div class="panel"><div class="error">Couldn’t load system stats — retrying every 3 seconds. (${esc(err.message)})</div></div>`;
    }
    if (alive) timer = setTimeout(poll, 3000);
  }

  container.innerHTML = `<div class="panel"><div class="empty">Loading system stats…</div></div>`;
  poll();

  return {
    unmount() {
      alive = false;
      if (timer) clearTimeout(timer);
    },
  };
}
