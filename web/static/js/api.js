// Tiny fetch wrapper. All endpoints are same-origin JSON.

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
      if (body.detail) detail = body.detail;
    } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}
