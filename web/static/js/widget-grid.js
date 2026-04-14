// Widget grid renderer — renders widgets in a CSS grid with optional edit mode.
// Supports drag-and-drop reorder, remove, and add-widget picker.

import { WIDGET_MAP, ALL_WIDGETS, loadRenderer } from "./widget-registry.js";
import { esc } from "./tiles/tile-helpers.js";

/**
 * Render the widget grid into a container.
 *
 * @param {HTMLElement} gridEl  - The .home-grid element to populate
 * @param {string[]}    layout  - Ordered array of widget IDs
 * @param {object}      data    - { home: apiResponse|null, health: { tiles, channel_names, user_names }|null }
 * @param {object}      opts    - { editMode, onReorder(newLayout), onRemove(id), onAdd() }
 */
function entryToParts(entry) {
  if (typeof entry === "string") return { id: entry, rows: 1 };
  return { id: entry.id, rows: entry.rows || 1 };
}

export async function renderGrid(gridEl, layout, data, opts = {}) {
  gridEl.innerHTML = "";

  const parts = layout.map(entryToParts);

  const renderers = await Promise.all(
    parts.map(p => loadRenderer(p.id).catch(err => {
      console.error(`[widget-grid] failed to load renderer for "${p.id}":`, err);
      return null;
    }))
  );

  for (let i = 0; i < parts.length; i++) {
    const { id, rows } = parts[i];
    const widget = WIDGET_MAP[id];
    if (!widget) continue;

    const renderTile = renderers[i];
    if (!renderTile) continue;

    const card = document.createElement("div");
    card.className = "home-card"
      + (widget.wide ? " home-card-wide" : "")
      + (rows === 2 ? " home-card-tall" : "");
    card.dataset.widgetId = id;
    card.dataset.rows = String(rows);

    // Edit mode controls
    if (opts.editMode) {
      card.draggable = true;
      card.innerHTML = `
        <div class="widget-handle" title="Drag to reorder">&#9776;</div>
        <button class="widget-remove" title="Remove widget">&times;</button>
        <div class="widget-resize" title="Drag to resize">&#8690;</div>
      `;
      card.querySelector(".widget-remove").addEventListener("click", (e) => {
        e.stopPropagation();
        if (opts.onRemove) opts.onRemove(id);
      });
    }

    // Render tile content into a wrapper so edit controls stay separate
    const content = document.createElement("div");
    content.className = "widget-content";

    try {
      if (widget.source === "health" && data.health) {
        const tileData = (data.health.tiles || {})[widget.tileKey];
        if (tileData) {
          const names = widget.needsNames
            ? { channels: data.health.channel_names || {}, users: data.health.user_names || {} }
            : null;
          if (names) {
            renderTile(content, tileData, names);
          } else {
            renderTile(content, tileData);
          }
        } else {
          content.innerHTML = `<div class="home-card-label">${esc(widget.label)}</div><div class="home-dim">No data</div>`;
        }
      } else if (widget.source === "home" && data.home) {
        renderTile(content, data.home);
      } else {
        content.innerHTML = `<div class="home-card-label">${esc(widget.label)}</div><div class="home-dim">Loading...</div>`;
      }
    } catch (err) {
      content.innerHTML = `<div class="home-card-label">${esc(widget.label)}</div><div class="error">Render error</div>`;
    }

    card.appendChild(content);

    // Tile click-through to report page (not in edit mode)
    if (!opts.editMode && widget.nav) {
      card.style.cursor = "pointer";
      card.classList.add("home-card-clickable");
      card.addEventListener("click", () => {
        window.location.hash = `#/${widget.nav}`;
      });
    }

    gridEl.appendChild(card);
  }

  // Add widget button (edit mode only)
  if (opts.editMode) {
    const addBtn = document.createElement("div");
    addBtn.className = "home-card widget-add-btn";
    addBtn.innerHTML = `<span class="widget-add-icon">+</span><span>Add Widget</span>`;
    addBtn.addEventListener("click", () => {
      if (opts.onAdd) opts.onAdd();
    });
    gridEl.appendChild(addBtn);

    _setupDragDrop(gridEl, layout, opts);
    _setupResize(gridEl, opts);
  }
}

/**
 * Show the widget picker modal.
 *
 * @param {string[]}   currentLayout  - Currently placed widget IDs
 * @param {Set}        userPerms      - User's permission set
 * @param {function}   onSelect       - Called with widget ID when user picks one
 */
export function showWidgetPicker(currentLayout, userPerms, onSelect) {
  // Remove existing picker if any
  const existing = document.querySelector(".widget-picker-overlay");
  if (existing) existing.remove();

  const currentSet = new Set(currentLayout);

  // Filter available widgets by perms and not already placed
  const available = ALL_WIDGETS.filter(w => {
    if (w.perms.length && !w.perms.every(p => userPerms.has(p))) return false;
    return true;
  });

  // Group by category
  const groups = {};
  for (const w of available) {
    if (!groups[w.category]) groups[w.category] = [];
    groups[w.category].push(w);
  }

  const overlay = document.createElement("div");
  overlay.className = "widget-picker-overlay";

  let html = `<div class="widget-picker">
    <div class="widget-picker-header">
      <h3>Add Widget</h3>
      <button class="widget-picker-close">&times;</button>
    </div>
    <div class="widget-picker-body">`;

  for (const [category, widgets] of Object.entries(groups)) {
    html += `<div class="widget-picker-category">${esc(category)}</div>`;
    for (const w of widgets) {
      const placed = currentSet.has(w.id);
      html += `
        <button class="widget-picker-item${placed ? " widget-picker-placed" : ""}"
                data-widget-id="${w.id}" ${placed ? "disabled" : ""}>
          <span class="widget-picker-item-label">${esc(w.label)}</span>
          ${placed ? '<span class="widget-picker-item-badge">Added</span>' : ""}
        </button>
      `;
    }
  }

  html += `</div></div>`;
  overlay.innerHTML = html;

  overlay.querySelector(".widget-picker-close").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });

  overlay.querySelectorAll(".widget-picker-item:not([disabled])").forEach(btn => {
    btn.addEventListener("click", () => {
      onSelect(btn.dataset.widgetId);
      overlay.remove();
    });
  });

  document.body.appendChild(overlay);
}


// ── Drag and drop ──────────────────────────────────────────────────

function _setupDragDrop(gridEl, layout, opts) {
  let dragSrcIndex = null;

  gridEl.addEventListener("dragstart", (e) => {
    const card = e.target.closest(".home-card[data-widget-id]");
    if (!card) return;
    dragSrcIndex = [...gridEl.querySelectorAll(".home-card[data-widget-id]")].indexOf(card);
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", card.dataset.widgetId);
  });

  gridEl.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const card = e.target.closest(".home-card[data-widget-id]");
    if (card) {
      // Clear all drag-over states
      gridEl.querySelectorAll(".drag-over").forEach(el => el.classList.remove("drag-over"));
      card.classList.add("drag-over");
    }
  });

  gridEl.addEventListener("dragleave", (e) => {
    const card = e.target.closest(".home-card[data-widget-id]");
    if (card) card.classList.remove("drag-over");
  });

  gridEl.addEventListener("drop", (e) => {
    e.preventDefault();
    gridEl.querySelectorAll(".drag-over").forEach(el => el.classList.remove("drag-over"));
    gridEl.querySelectorAll(".dragging").forEach(el => el.classList.remove("dragging"));

    const card = e.target.closest(".home-card[data-widget-id]");
    if (!card || dragSrcIndex === null) return;

    const dropIndex = [...gridEl.querySelectorAll(".home-card[data-widget-id]")].indexOf(card);
    if (dropIndex === -1 || dropIndex === dragSrcIndex) return;

    // Reorder layout
    const newLayout = [...layout];
    const [moved] = newLayout.splice(dragSrcIndex, 1);
    newLayout.splice(dropIndex, 0, moved);

    if (opts.onReorder) opts.onReorder(newLayout);
    dragSrcIndex = null;
  });

  gridEl.addEventListener("dragend", () => {
    gridEl.querySelectorAll(".dragging").forEach(el => el.classList.remove("dragging"));
    gridEl.querySelectorAll(".drag-over").forEach(el => el.classList.remove("drag-over"));
    dragSrcIndex = null;
  });
}


// ── Resize (corner-drag, snaps to 1 or 2 rows) ─────────────────────

function _setupResize(gridEl, opts) {
  gridEl.addEventListener("pointerdown", (e) => {
    const handle = e.target.closest(".widget-resize");
    if (!handle) return;
    const card = handle.closest(".home-card[data-widget-id]");
    if (!card) return;

    e.preventDefault();
    e.stopPropagation();

    // Disable native drag while resizing so card doesn't get picked up
    const wasDraggable = card.draggable;
    card.draggable = false;

    const rect = card.getBoundingClientRect();
    const startRows = parseInt(card.dataset.rows || "1", 10);
    const rowHeight = rect.height / startRows;
    const startTop = rect.top;

    let currentRows = startRows;
    card.classList.add("resizing");

    try { handle.setPointerCapture(e.pointerId); } catch (_) {}

    const onMove = (ev) => {
      const proposed = Math.max(1, Math.min(2,
        Math.round((ev.clientY - startTop) / rowHeight)));
      if (proposed !== currentRows) {
        currentRows = proposed;
        card.classList.toggle("home-card-tall", currentRows === 2);
      }
    };

    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      try { handle.releasePointerCapture(e.pointerId); } catch (_) {}
      card.classList.remove("resizing");
      card.draggable = wasDraggable;
      card.dataset.rows = String(currentRows);
      if (currentRows !== startRows && opts.onResize) {
        opts.onResize(card.dataset.widgetId, currentRows);
      }
    };

    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });

  // Prevent native drag from starting when grabbing the resize handle
  gridEl.addEventListener("dragstart", (e) => {
    if (e.target.closest(".widget-resize")) e.preventDefault();
  }, true);
}
