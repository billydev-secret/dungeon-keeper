// Shared helpers for the audit-log panels (moderation, quotes, guess, etc.).
// Pure DOM builders — intentionally dependency-free so a panel's dynamic import
// never fails on a missing transitive module.

export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === "className") node.className = v;
      else if (k === "style") node.style.cssText = v;
      else node.setAttribute(k, v);
    }
  }
  for (const c of children) {
    if (c == null) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function badge(text, cls) {
  return el("span", { className: `badge ${cls || ""}`.trim() }, text);
}

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

// Column descriptor for a unix-seconds timestamp field.
export function tsColumn(field, label = "Time") {
  return { label, render: (e) => fmtTs(e[field]) };
}

function mkOpt(value, label, selected) {
  const o = el("option", null, label);
  o.value = value;
  if (selected) o.selected = true;
  return o;
}

function buildControl(filter) {
  if (filter.type === "checkbox") {
    const cb = el("input", { type: "checkbox" });
    const label = el("label", null, cb, ` ${filter.label}`);
    return { node: label, read: (params) => { if (cb.checked) params[filter.name] = "true"; }, input: cb };
  }
  const sel = el("select");
  for (const opt of filter.options || []) {
    sel.append(mkOpt(opt.value, opt.label, false));
  }
  const label = el("label", null, `${filter.label} `, sel);
  return { node: label, read: (params) => { if (sel.value) params[filter.name] = sel.value; }, input: sel };
}

function buildCell(col, e) {
  const attrs = {};
  if (col.className) attrs.className = col.className;
  if (col.title) {
    const t = col.title(e);
    if (t != null) attrs.title = t;
  }
  return el("td", attrs, col.render(e));
}

function buildTable(rows, total, columns) {
  const summary = el("div",
    { style: "color:var(--ink-dim);font-size:12px;margin-bottom:8px;" },
    total != null
      ? `Showing ${rows.length} of ${total} entries`
      : `Showing ${rows.length} entries`,
  );
  const head = el("thead", null,
    el("tr", null, ...columns.map((c) => el("th", null, c.label))),
  );
  const body = el("tbody");
  for (const e of rows) {
    body.append(el("tr", null, ...columns.map((c) => buildCell(c, e))));
  }
  const frag = document.createDocumentFragment();
  frag.append(summary, el("table", { className: "data-table" }, head, body));
  return frag;
}

// Renders a standard audit-log panel: header, filter controls + a "Show N"
// limit selector, and a table driven by `config.columns` and `config.fetch`.
//
// config = {
//   title, subtitle, empty,
//   filters: [{ name, label, options:[{value,label}] } | { name, label, type:"checkbox" }],
//   columns: [{ label, render(e), className?, title?(e) }],
//   fetch: async (params) => ({ rows, total? }),
// }
export function auditPanel(container, config) {
  container.replaceChildren();

  const controls = (config.filters || []).map(buildControl);

  const limitSel = el("select", null,
    mkOpt("50", "50", true), mkOpt("100", "100"), mkOpt("200", "200"),
  );

  const tableWrap = el("div", { className: "table-scroll" });
  tableWrap.replaceChildren(el("div", { className: "empty" }, "Loading…"));

  const refresh = async () => {
    const params = { limit: limitSel.value };
    for (const c of controls) c.read(params);
    try {
      const { rows = [], total } = await config.fetch(params);
      if (!rows.length) {
        tableWrap.replaceChildren(el("div", { className: "empty" }, config.empty || "No entries found."));
        return;
      }
      tableWrap.replaceChildren(buildTable(rows, total, config.columns));
    } catch (err) {
      tableWrap.replaceChildren(el("div", { className: "error" }, err.message));
    }
  };

  for (const c of controls) c.input.addEventListener("change", refresh);
  limitSel.addEventListener("change", refresh);

  const panel = el("div", { className: "panel" },
    el("header", null,
      el("h2", null, config.title),
      config.subtitle ? el("div", { className: "subtitle" }, config.subtitle) : null,
    ),
    el("div", { className: "controls" },
      ...controls.map((c) => c.node),
      el("label", null, "Show ", limitSel),
    ),
    tableWrap,
  );
  container.append(panel);
  refresh();

  return { unmount() {} };
}
