// Tiny fetch wrapper. All endpoints are same-origin JSON.

/** Escape a string for safe insertion into innerHTML, including attributes. */
export function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

/**
 * Canonical timestamp formatter: "Jun 10, 3:42 PM", with the year added
 * when it isn't the current year. Accepts unix seconds, ISO strings, or Date.
 */
export function fmtTs(ts) {
  if (!ts) return "—";
  const d = ts instanceof Date ? ts : new Date(typeof ts === "number" ? ts * 1000 : ts);
  if (isNaN(d.getTime())) return "—";
  const opts = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleString(undefined, opts);
}

/** Compact age/duration from seconds: "5d 4h", "3h 12m", "45m", "30s". */
export function fmtAge(seconds) {
  if (seconds == null || !isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

export async function api(path, params) {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      url.searchParams.set(k, v);
    }
  }
  const res = await fetch(url, { credentials: "same-origin" });
  if (res.status === 401) {
    window.location = "/login";
    return new Promise(() => {}); // hang — page is navigating away
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body.detail) detail = Array.isArray(body.detail)
        ? body.detail.map(e => e.msg || JSON.stringify(e)).join("; ")
        : String(body.detail);
    } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (res.status === 401) {
    window.location = "/login";
    return new Promise(() => {});
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const b = await res.json();
      if (b.detail) detail = Array.isArray(b.detail)
        ? b.detail.map(e => e.msg || JSON.stringify(e)).join("; ")
        : String(b.detail);
    } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}
