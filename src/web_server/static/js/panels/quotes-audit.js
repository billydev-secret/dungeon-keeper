import { api } from "../api.js";

const THEME_LABELS = {
  golden_meadow: "Golden Meadow",
  midnight:      "Midnight",
  rose:          "Rose",
};

const THEME_BADGE = {
  golden_meadow: "badge-warning",
  midnight:      "badge-info",
  rose:          "badge-danger",
};

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
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

function mkOpt(value, label, selected) {
  const o = el("option", null, label);
  o.value = value;
  if (selected) o.selected = true;
  return o;
}

function badge(text, cls) {
  return el("span", { className: `badge ${cls}` }, text);
}

function buildRow(e) {
  const themeLabel = THEME_LABELS[e.theme] || e.theme;
  const themeCls   = THEME_BADGE[e.theme]  || "";
  return el("tr", null,
    el("td", null, String(e.id)),
    el("td", null, e.quoter_name || e.quoter_id),
    el("td", null, e.quoted_user_name || e.quoted_user_id),
    el("td", null, badge(themeLabel, themeCls)),
    el("td", null, e.font),
    el("td", null, fmtTs(e.ts)),
  );
}

function buildTable(data) {
  const summary = el("div",
    { style: "color:var(--ink-dim);font-size:12px;margin-bottom:8px;" },
    `Showing ${data.entries.length} of ${data.total} entries`,
  );
  const head = el("thead", null,
    el("tr", null,
      el("th", null, "#"),
      el("th", null, "Quoter"),
      el("th", null, "Quoted"),
      el("th", null, "Theme"),
      el("th", null, "Font"),
      el("th", null, "Posted"),
    ),
  );
  const body = el("tbody");
  for (const e of data.entries) body.append(buildRow(e));
  const frag = document.createDocumentFragment();
  frag.append(summary, el("table", { className: "data-table" }, head, body));
  return frag;
}

export function mount(container) {
  container.replaceChildren();

  const themeSel = el("select", null,
    mkOpt("", "All themes", true),
    mkOpt("golden_meadow", "Golden Meadow"),
    mkOpt("midnight", "Midnight"),
    mkOpt("rose", "Rose"),
  );
  const limitSel = el("select", null,
    mkOpt("50", "50", true), mkOpt("100", "100"), mkOpt("200", "200"),
  );
  const tableWrap = el("div", { className: "table-scroll" });
  tableWrap.replaceChildren(el("div", { className: "empty" }, "Loading…"));

  const refresh = async () => {
    const params = { limit: limitSel.value };
    if (themeSel.value) params.theme = themeSel.value;
    try {
      const data = await api("/api/quotes/audit", params);
      if (!data.entries.length) {
        tableWrap.replaceChildren(el("div", { className: "empty" }, "No quotes posted yet."));
        return;
      }
      tableWrap.replaceChildren(buildTable(data));
    } catch (err) {
      tableWrap.replaceChildren(el("div", { className: "error" }, err.message));
    }
  };

  themeSel.addEventListener("change", refresh);
  limitSel.addEventListener("change", refresh);

  const panel = el("div", { className: "panel" },
    el("header", null,
      el("h2", null, "Quotes Audit Log"),
      el("div", { className: "subtitle" }, "All quote cards posted to channels"),
    ),
    el("div", { className: "controls" },
      el("label", null, "Theme ", themeSel),
      el("label", null, "Show ", limitSel),
    ),
    tableWrap,
  );
  container.append(panel);
  refresh();

  return { unmount() {} };
}
