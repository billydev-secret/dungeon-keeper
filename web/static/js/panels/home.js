import { api } from "../api.js";
import { WIDGET_MAP, DEFAULT_HOME, DEFAULT_ADMIN, ALL_WIDGETS, loadRenderer } from "../widget-registry.js";
import { renderGrid, showWidgetPicker } from "../widget-grid.js";
import { esc } from "../tiles/tile-helpers.js";

const STORAGE_VERSION = 1;

function getLayout(userId, isAdmin) {
  try {
    const raw = localStorage.getItem(`dk_layout_${userId}`);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed.version === STORAGE_VERSION && Array.isArray(parsed.widgets)) {
        // Filter out any widgets that no longer exist in registry
        const valid = parsed.widgets.filter(id => WIDGET_MAP[id]);
        if (valid.length) return valid;
      }
    }
  } catch (_) {}
  return isAdmin ? [...DEFAULT_ADMIN] : [...DEFAULT_HOME];
}

function saveLayout(userId, layout) {
  localStorage.setItem(`dk_layout_${userId}`, JSON.stringify({
    version: STORAGE_VERSION,
    widgets: layout,
  }));
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel home-panel">
      <div class="home-loading">Loading dashboard...</div>
    </div>
  `;

  const user = window.__dk_user || {};
  const userId = user.user_id || "0";
  const perms = user.perms || new Set();
  const isAdmin = perms.has("admin");

  let layout = getLayout(userId, isAdmin);
  let editMode = false;
  let refreshTimer = null;
  let data = { home: null, health: null };

  async function fetchData() {
    // Determine which sources are needed
    const needsHome = layout.some(id => WIDGET_MAP[id]?.source === "home");
    const needsHealth = layout.some(id => WIDGET_MAP[id]?.source === "health");

    const promises = [];
    if (needsHome) promises.push(api("/api/home").then(d => { data.home = d; }));
    else promises.push(Promise.resolve());
    if (needsHealth) promises.push(api("/api/health/tiles").then(d => { data.health = d; }));
    else promises.push(Promise.resolve());

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

  async function render() {
    const panel = container.querySelector(".panel");
    panel.innerHTML = renderHeader() + '<div class="home-grid"></div>';

    const gridEl = panel.querySelector(".home-grid");
    if (editMode) gridEl.classList.add("edit-mode");

    // Filter layout to only widgets user has perms for
    const visibleLayout = layout.filter(id => {
      const w = WIDGET_MAP[id];
      return w && (!w.perms.length || w.perms.every(p => perms.has(p)));
    });

    await renderGrid(gridEl, visibleLayout, data, {
      editMode,
      onReorder(newLayout) {
        // Re-insert any perm-filtered widgets at their original positions
        layout = newLayout;
        saveLayout(userId, layout);
        render();
      },
      onRemove(id) {
        layout = layout.filter(wid => wid !== id);
        saveLayout(userId, layout);
        render();
      },
      onAdd() {
        showWidgetPicker(layout, perms, (id) => {
          layout.push(id);
          saveLayout(userId, layout);
          render();
        });
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
        layout = isAdmin ? [...DEFAULT_ADMIN] : [...DEFAULT_HOME];
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
      if (panel) panel.innerHTML = `<div class="error">${esc(String(err))}</div>`;
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
