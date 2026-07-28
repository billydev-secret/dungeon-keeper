// Tiny read-only view of the nav's page list, published by app.js at boot.
//
// Exists purely to break a cycle: the usage report needs "every panel id that
// exists" to compute the never-opened list, but that list is defined in
// app.js — the entry module. A panel statically importing app.js is legal ESM
// and *usually* resolves to the same instance, but only as long as the
// cache-bust rewrite produces byte-identical URLs for both the <script> tag
// and the panel's relative import. If those ever diverge the browser would
// evaluate app.js a second time and boot the dashboard twice. This module has
// no imports of its own, so it can never do that.
let pageIds = [];

/** Called once by app.js as it builds the nav. */
export function setPageIds(ids) {
  pageIds = [...ids];
}

/** Every panel id the nav defines, before per-user permission filtering. */
export function allPageIds() {
  // setPageIds already copied; callers filter/map rather than mutate.
  return pageIds;
}
