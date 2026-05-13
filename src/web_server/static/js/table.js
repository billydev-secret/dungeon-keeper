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
 *   });
 */

export function renderSortableTable(container, { columns, data, defaultSort, defaultAsc }) {
  if (!data || !data.length) { container.innerHTML = ""; return; }

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
    const rows = sorted();
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
    `;
  }

  render();

  container.addEventListener("click", (e) => {
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
  });
}
