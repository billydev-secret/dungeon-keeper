/**
 * Sortable data table utility.
 *
 * Usage:
 *   import { renderSortableTable } from "../table.js";
 *   renderSortableTable(wrapEl, {
 *     columns: [
 *       { key: "user_name", label: "Member", format: (v, row) => row.user_name || row.user_id },
 *       { key: "score",     label: "Score",  format: (v) => v.toFixed(1) },
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
 */
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
      const cls = c.key === sortKey ? (sortAsc ? "sort-asc" : "sort-desc") : "";
      return `<th data-sort="${c.key}" class="${cls}">${c.label}</th>`;
    }).join("");

    const bodyRows = rows.map((row, idx) => {
      const cells = columns.map((c) => {
        const raw = row[c.key];
        const display = c.format ? c.format(raw, row, idx) : (raw ?? "");
        return `<td>${display}</td>`;
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
