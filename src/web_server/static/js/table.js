/**
 * Sortable data table utility.
 *
 * Usage:
 *   import { renderSortableTable } from "../table.js";
 *   renderSortableTable(wrapEl, {
 *     columns: [
 *       { key: "user_name", label: "Member", format: (v, row) => row.user_name || row.user_id },
 *       { key: "score",     label: "Score",  format: (v) => v.toFixed(1) },
 *       // Opt in per column when the format() return is deliberately markup:
 *       { key: "trend", label: "Trend", html: true,
 *         format: (v) => `<span style="color:var(--green)">${v}%</span>` },
 *     ],
 *     data: entries,           // array of objects
 *     defaultSort: "score",    // initial sort key
 *     defaultAsc: false,       // initial sort direction
 *     emptyMsg: "No members match this filter.",
 *                              // optional: styled empty state when data is
 *                              // empty (omit to keep the legacy clear-to-
 *                              // nothing behavior)
 *     maxRows: 200,            // optional row cap; adds a "Showing first
 *                              // N of M rows." footer when data exceeds it
 *   });
 *
 * ESCAPING (S1) — cells are TEXT by default.
 * ------------------------------------------
 * Every cell — the plain `row[key]` value and anything a `format()` returns —
 * is HTML-escaped before it reaches innerHTML. That default is not a nicety:
 * six report panels feed this table raw Discord display names, which are
 * member-controlled, so a nickname of `<img src=x onerror=…>` used to execute
 * in the moderator's session on every page view (stored XSS).
 *
 * A column that legitimately renders markup — a colored `<span>`, a `<code>` —
 * opts in with `html: true`, and then owns its own escaping of any value it
 * interpolates. Keep those to computed values (numbers, fixed class names);
 * never opt in a column that renders a name, a label, or anything else that
 * came from a user.
 */
import { esc } from "./api.js";
import { renderEmpty } from "./states.js";

// Panels that re-render on a filter change call this repeatedly against the
// same container. Each call used to add another click listener, and because
// every closure kept its own sortKey/sortAsc, clicking a header after a couple
// of re-renders sorted several different ways at once. Track the live handler
// per container and detach the old one first.
const _sortHandlers = new WeakMap();

function _detach(container) {
  const prev = _sortHandlers.get(container);
  if (prev) {
    container.removeEventListener("click", prev);
    _sortHandlers.delete(container);
  }
}

export function renderSortableTable(container, { columns, data, defaultSort, defaultAsc, emptyMsg, maxRows }) {
  _detach(container);

  if (!data || !data.length) {
    container.innerHTML = emptyMsg ? renderEmpty(emptyMsg) : "";
    return;
  }

  let sortKey = defaultSort || columns[0].key;
  let sortAsc = defaultAsc ?? false;

  function sorted() {
    return [...data].sort((a, b) => {
      let av = a[sortKey], bv = b[sortKey];
      if (av == null) av = "";
      if (bv == null) bv = "";
      if (typeof av === "string" && typeof bv === "string") {
        return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      return sortAsc ? av - bv : bv - av;
    });
  }

  function render() {
    let rows = sorted();
    let capNote = "";
    if (maxRows && rows.length > maxRows) {
      capNote = `<div class="field-hint" style="padding:6px 2px;">Showing first ${maxRows} of ${rows.length} rows.</div>`;
      rows = rows.slice(0, maxRows);
    }
    const headCells = columns.map((c) => {
      const sortCls = c.key === sortKey ? (sortAsc ? "sort-asc" : "sort-desc") : "";
      const cls = [sortCls, c.cls].filter(Boolean).join(" ");
      // Labels are escaped too: they read as developer constants, but
      // interaction-graph builds one out of a member's display name
      // (`% of ${userName}'s total`), which is the same untrusted input.
      return `<th data-sort="${esc(c.key)}" class="${esc(cls)}">${esc(c.label)}</th>`;
    }).join("");

    const bodyRows = rows.map((row, idx) => {
      const cells = columns.map((c) => {
        const raw = row[c.key];
        const display = c.format ? c.format(raw, row, idx) : (raw ?? "");
        // Text by default; `html: true` is the deliberate opt-out (see the
        // ESCAPING note at the top of this file).
        const cell = c.html ? display : esc(display);
        // `cls` lets a column carry .num (right-aligned tabular figures) —
        // otherwise numeric tables have to be hand-rolled to get it.
        return c.cls ? `<td class="${esc(c.cls)}">${cell}</td>` : `<td>${cell}</td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");

    container.innerHTML = `
      <table class="data-table">
        <thead><tr>${headCells}</tr></thead>
        <tbody>${bodyRows}</tbody>
      </table>
    ${capNote}`;
  }

  render();

  const onClick = (e) => {
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    const key = th.dataset.sort;
    if (sortKey === key) {
      sortAsc = !sortAsc;
    } else {
      sortKey = key;
      // Default: strings ascending, numbers descending
      const sample = data.find((d) => d[key] != null);
      sortAsc = sample && typeof sample[key] === "string";
    }
    render();
  };
  container.addEventListener("click", onClick);
  _sortHandlers.set(container, onClick);
}
