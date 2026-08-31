import { api, esc } from "../api.js";
import { WIDGET_MAP, DEFAULT_HOME, DEFAULT_MOD, DEFAULT_ADMIN } from "../widget-registry.js";
import { renderGrid, showWidgetPicker } from "../widget-grid.js";
import { renderError } from "../states.js";

const STORAGE_VERSION = 3;

function entryId(e) { return typeof e === "string" ? e : e?.id; }

// Widgets added after a user already had a saved layout would otherwise never
// appear for them. Each entry is offered exactly once: if they remove it, the
// flag stays set and it doesn't come back.
const ONE_TIME_ADDITIONS = [
  { id: "setup-suggestions", flag: "dk_seen_setup_suggestions", adminOnly: true },
  { id: "config-problems", flag: "dk_seen_config_problems", adminOnly: true },
];

function injectNewWidgets(layout, userId, isAdmin) {
  const present = new Set(layout.map(entryId));
  for (const { id, flag, adminOnly } of ONE_TIME_ADDITIONS) {
    if (adminOnly && !isAdmin) continue;
    const key = `${flag}_${userId}`;
    try {
      if (localStorage.getItem(key)) continue;
      localStorage.setItem(key, "1");
    } catch (_) {
      continue; // no storage → don't nag every load
    }
    if (!present.has(id)) layout.unshift(id);
  }
  return layout;
}

function getLayout(userId, isAdmin, isMod) {
  try {
    const raw = localStorage.getItem(`dk_layout_${userId}`);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed.widgets) &&
          [1, 2, STORAGE_VERSION].includes(parsed.version)) {
        const valid = parsed.widgets.filter(e => WIDGET_MAP[entryId(e)]);
        if (valid.length) return injectNewWidgets(valid, userId, isAdmin);
      }
    }
  } catch (_) {}
  return isAdmin ? [...DEFAULT_ADMIN] : isMod ? [...DEFAULT_MOD] : [...DEFAULT_HOME];
}

function saveLayout(userId, layout) {
  const serialized = layout.map(e => {
    if (typeof e === "string") return e;
    const out = { id: e.id };
    if (e.rows && e.rows > 1) out.rows = e.rows;
    if (typeof e.cols === "number") out.cols = e.cols;
    return Object.keys(out).length > 1 ? out : e.id;
  });
  localStorage.setItem(`dk_layout_${userId}`, JSON.stringify({
    version: STORAGE_VERSION,
    widgets: serialized,
  }));
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel home-panel">
      <div class="home-loading">Loading dashboard…</div>
    </div>
  `;

  const user = window.__dk_user || {};
  const userId = user.user_id || "0";
  const perms = user.perms || new Set();
  const isAdmin = perms.has("admin");
  const isMod = perms.has("moderator") || isAdmin;

  let layout = getLayout(userId, isAdmin, isMod);
  let editMode = false;
  let refreshTimer = null;
  let data = {
    home: null, health: null, economy: null, suggestions: null, channelHealth: null,
  };
  // Tracked separately from data.suggestions so a failed fetch can be told
  // apart from a genuinely empty ("nothing left to set up") response — both
  // leave the suggestions list empty, but only one should hide the card.
  let suggestionsFailed = false;

  async function fetchData() {
    // Determine which sources are needed
    const needsHome = layout.some(e => WIDGET_MAP[entryId(e)]?.source === "home");
    const needsHealth = layout.some(e => WIDGET_MAP[entryId(e)]?.source === "health");
    const needsEconomy = layout.some(e => WIDGET_MAP[entryId(e)]?.source === "economy");
    const needsSuggestions = layout.some(e => WIDGET_MAP[entryId(e)]?.source === "suggestions");
    const needsChannelHealth = layout.some(e => WIDGET_MAP[entryId(e)]?.source === "channelHealth");

    const promises = [];
    if (needsHome) promises.push(api("/api/home").then(d => { data.home = d; }));
    else promises.push(Promise.resolve());
    if (needsHealth) promises.push(api("/api/health/tiles").then(d => { data.health = d; }));
    else promises.push(Promise.resolve());
    if (needsEconomy) promises.push(api("/api/economy/metrics").then(d => { data.economy = d; }));
    else promises.push(Promise.resolve());
    // Suggestions are advisory — a failure here must not blank the dashboard.
    // A failed fetch is kept out of data.suggestions (left null) rather than
    // faked as an empty list, so it can't be mistaken for "nothing left to
    // suggest" — see the visibleLayout filter in render().
    if (needsSuggestions) {
      promises.push(
        api("/api/help/suggestions?limit=3")
          .then(d => { data.suggestions = d; suggestionsFailed = false; })
          .catch(() => { data.suggestions = null; suggestionsFailed = true; }),
      );
    } else promises.push(Promise.resolve());
    // Advisory like suggestions — a failure must not blank the dashboard, and
    // "I couldn't check" is reported as unavailable rather than as all-clear.
    if (needsChannelHealth) {
      promises.push(
        api("/api/config/channel-health")
          .then(d => { data.channelHealth = d; })
          .catch(() => { data.channelHealth = { available: false, issues: [] }; }),
      );
    } else promises.push(Promise.resolve());

    await Promise.all(promises);
  }

  function renderHeader() {
    const guildName = data.home?.guild?.name || "Server";
    const memberCount = data.home?.guild?.member_count || "—";
    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

    return `
      <header>
        <h2>${esc(guildName)}</h2>
        <div class="subtitle">
          ${memberCount} members &middot; updated ${time}
          <button class="home-edit-toggle" title="${editMode ? "Done editing" : "Customize dashboard"}">
            ${editMode ? "&#10003; Done" : "&#9998;"}
          </button>
          ${editMode ? '<button class="home-reset-btn" title="Reset to default layout">Reset</button>' : ""}
        </div>
      </header>
    `;
  }

  // A 136-panel dashboard needs a "start here" for people seeing it for the
  // first time. Shown to admins (who do the setup) until dismissed (W-H3).
  const HINT_KEY = "dk_home_hint_dismissed";

  function renderHint() {
    if (!isAdmin) return "";
    try {
      if (localStorage.getItem(HINT_KEY) === "1") return "";
    } catch (_) {}
    return `
      <div class="home-hint">
        <p>New here? The First-Time Setup guide walks through the settings worth
           configuring before you invite members.</p>
        <a class="btn" href="#/help-start">Open Setup Guide</a>
        <button class="home-hint-x" type="button" aria-label="Dismiss">&times;</button>
      </div>`;
  }

  async function render() {
    const panel = container.querySelector(".panel");
    panel.innerHTML = renderHeader() + renderHint() + '<div class="home-grid"></div>';

    panel.querySelector(".home-hint-x")?.addEventListener("click", () => {
      try { localStorage.setItem(HINT_KEY, "1"); } catch (_) {}
      panel.querySelector(".home-hint")?.remove();
    });

    const gridEl = panel.querySelector(".home-grid");
    if (editMode) gridEl.classList.add("edit-mode");

    // Filter layout to only widgets user has perms for
    const visibleLayout = layout.filter(e => {
      const id = entryId(e);
      const w = WIDGET_MAP[id];
      if (!w || (w.perms.length && !w.perms.every(p => perms.has(p)))) return false;
      // Setup suggestions: when the fetch succeeded and came back with
      // nothing outstanding, the card shouldn't render at all — not even an
      // "all done" message. A failed fetch is a different state (surfaced by
      // fetchData leaving data.suggestions null) and must not be hidden the
      // same way, or "couldn't check" would read as "nothing to do".
      if (id === "setup-suggestions" && !suggestionsFailed &&
          data.suggestions && (data.suggestions.suggestions || []).length === 0) {
        return false;
      }
      return true;
    });

    await renderGrid(gridEl, visibleLayout, data, {
      editMode,
      onReorder(newLayout) {
        // newLayout is a reorder of visibleLayout only. Entries left out of
        // visibleLayout (perm-gated, or setup-suggestions hidden because it's
        // all done) must be re-inserted at their original spots rather than
        // dropped from the saved layout — otherwise finishing setup and later
        // dragging any other card would permanently lose the suggestions
        // widget, and it would never come back even if something later
        // becomes unconfigured again.
        const visibleIds = new Set(visibleLayout.map(entryId));
        const hidden = layout.filter(e => !visibleIds.has(entryId(e)));
        if (hidden.length) {
          const hiddenIds = new Set(hidden.map(entryId));
          const nextVisible = [...newLayout];
          layout = layout.map(e => hiddenIds.has(entryId(e)) ? e : nextVisible.shift());
        } else {
          layout = newLayout;
        }
        saveLayout(userId, layout);
        render();
      },
      onRemove(id) {
        layout = layout.filter(e => entryId(e) !== id);
        saveLayout(userId, layout);
        render();
      },
      onAdd() {
        showWidgetPicker(layout.map(entryId), perms, (id) => {
          layout.push(id);
          saveLayout(userId, layout);
          render();
        });
      },
      onResize(id, rows, cols) {
        layout = layout.map(e => {
          if (entryId(e) !== id) return e;
          const widget = WIDGET_MAP[id];
          const entry = { id };
          if (rows > 1) entry.rows = rows;
          // Persist cols whenever the user has resized a "wide" widget,
          // so we remember the override even when the chosen size is 1.
          if (cols > 1 || widget?.wide) entry.cols = cols;
          const hasExtra = entry.rows !== undefined || entry.cols !== undefined;
          return hasExtra ? entry : id;
        });
        saveLayout(userId, layout);
        render();
      },
    });

    // Bind header buttons
    const editBtn = panel.querySelector(".home-edit-toggle");
    if (editBtn) {
      editBtn.addEventListener("click", () => {
        editMode = !editMode;
        render();
      });
    }

    const resetBtn = panel.querySelector(".home-reset-btn");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        layout = isAdmin ? [...DEFAULT_ADMIN] : isMod ? [...DEFAULT_MOD] : [...DEFAULT_HOME];
        saveLayout(userId, layout);
        render();
      });
    }
  }

  async function load() {
    try {
      await fetchData();
      await render();
    } catch (err) {
      console.error("[home] load/render error:", err);
      const panel = container.querySelector(".panel");
      if (panel) panel.innerHTML = renderError(err);
    }
  }

  load();
  refreshTimer = setInterval(() => {
    if (!editMode) load();
  }, 60_000);

  return {
    unmount() {
      if (refreshTimer) clearInterval(refreshTimer);
    },
  };
}
