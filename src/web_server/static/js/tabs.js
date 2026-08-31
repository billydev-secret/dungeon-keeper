// Shared lazy-loaded tab strip, extracted from config-bios (the reference
// implementation — copy its usage, not this file's internals, when a panel
// wants the same pattern).
import { esc } from "./api.js";
import { mountAsync } from "./config-helpers.js";

// ── Tab strip with lazy per-tab loading ─────────────────────────────────
//
// The shape a multi-section panel wants: a row of buttons switches between
// panes, and each pane's content is fetched only the first time its tab is
// opened — an admin who never clicks past the first section never pays for
// the others' requests.
//
// Each tab's `render` is called from a click handler with nothing awaiting
// it directly — mountAsync is what turns a rejection into a real error
// state with a Retry button in the pane it belongs to, instead of an
// unhandled promise rejection that leaves the pane on "Loading…" forever
// (F1). That only works if the rejection reaches mountAsync: `render` must
// let it propagate (throw / reject) rather than catching it and returning
// normally. Retry re-runs just that one tab's `render` — each tab rebuilds
// its own pane from scratch, so there's no need to remount the whole page.
//
// Usage:
//     import { mountTabs } from "../tabs.js";
//     export function mount(container) {
//       container.innerHTML = `
//         <div class="panel">
//           <header><h2>Bios</h2></header>
//           <div data-tabs></div>
//         </div>
//       `;
//       return mountTabs(container.querySelector("[data-tabs]"), [
//         { key: "config", label: "Settings", render: renderConfigTab,
//           errorMsg: "Couldn't load the bios settings." },
//         { key: "fields", label: "Profile Questions", render: renderFieldsTab,
//           errorMsg: "Couldn't load the profile questions." },
//       ], { ariaLabel: "Bios settings sections" });
//     }
//
// `render(pane)` may be sync or async, and may resolve to an inner handle
// ({ unmount() }) the way mountAsync's loader can — that handle is unmounted
// when the tab is torn down or reopened.
//
// @param {HTMLElement} container  element the tab strip + panes are built
//        into; this helper owns it entirely (same contract as mountAsync)
// @param {Array<{key: string, label: string,
//        render: (pane: HTMLElement) => (any|Promise<any>),
//        errorMsg?: string}>} tabs  at least one required; keys must be unique
// @param {object} [opts]
// @param {string} [opts.ariaLabel]  aria-label for the strip's role="group";
//        defaults to "Sections" — pass a specific one, it reads to a screen
//        reader user in place of any visible panel title
// @param {string} [opts.initial]   key of the tab opened first; defaults to
//        the first entry in `tabs`
// @param {(key: string) => void} [opts.onShow]  called with a tab's key each
//        time it becomes the visible one, including the initial open — a
//        hook for a caller (typically a report panel) that mirrors the
//        active tab into the URL hash via syncHash
// @returns {{ unmount(): void, showTab(key: string): void }}
export function mountTabs(container, tabs, opts = {}) {
  if (!tabs || !tabs.length) {
    throw new Error("mountTabs: at least one tab is required");
  }
  const ariaLabel = opts.ariaLabel || "Sections";
  const initialKey = opts.initial && tabs.some((t) => t.key === opts.initial)
    ? opts.initial
    : tabs[0].key;

  container.innerHTML = `
    <div class="tabs" style="margin-bottom:12px;" role="group" aria-label="${esc(ariaLabel)}">
      ${tabs.map((t) => `
        <button type="button" data-tab="${esc(t.key)}"
          class="tab-btn${t.key === initialKey ? " active" : ""}"
          aria-pressed="${t.key === initialKey ? "true" : "false"}">${esc(t.label)}</button>
      `).join("")}
    </div>
    ${tabs.map((t) => `<div data-pane="${esc(t.key)}"${t.key === initialKey ? "" : ' style="display:none;"'}></div>`).join("")}
  `;

  const panes = {};
  const renderers = {};
  const errorMsgs = {};
  container.querySelectorAll("[data-pane]").forEach((pane) => {
    panes[pane.dataset.pane] = pane;
  });
  for (const t of tabs) {
    renderers[t.key] = t.render;
    errorMsgs[t.key] = t.errorMsg || "Couldn't load this section.";
  }

  const loaded = {};
  const handles = {};

  function openTab(key) {
    handles[key]?.unmount?.();
    handles[key] = mountAsync(panes[key], () => renderers[key](panes[key]), {
      errorMsg: errorMsgs[key],
      retry: () => openTab(key),
    });
  }

  function showTab(key) {
    if (!panes[key]) return;
    container.querySelectorAll(".tab-btn").forEach((b) => {
      const active = b.dataset.tab === key;
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", active ? "true" : "false");
    });
    for (const [name, pane] of Object.entries(panes)) {
      pane.style.display = name === key ? "" : "none";
    }
    if (!loaded[key]) {
      loaded[key] = true;
      openTab(key);
    }
    opts.onShow?.(key);
  }

  container.querySelectorAll(".tab-btn").forEach((btn) => {
    // Native <button>s are keyboard-operable (Tab to focus, Enter/Space to
    // activate) with no extra wiring — the same as the original inline
    // implementation this replaces.
    btn.addEventListener("click", () => showTab(btn.dataset.tab));
  });

  loaded[initialKey] = true;
  openTab(initialKey);
  opts.onShow?.(initialKey);

  return {
    showTab,
    unmount() {
      for (const h of Object.values(handles)) h?.unmount?.();
    },
  };
}
