import { api } from "../api.js";

const ACTION_LABELS = {
  submit:        "Submit",
  delete:        "Delete",
  solve:         "Solve",
  guess_cap_hit: "Guess Cap Hit",
};

const ACTION_BADGE = {
  submit: "badge-info",
  delete: "badge-danger",
  solve: "badge-success",
  guess_cap_hit: "badge-warning",
};

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function fmtDetails(raw) {
  if (!raw) return "—";
  try {
    const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (!parsed || typeof parsed !== "object" || Object.keys(parsed).length === 0) return "—";
    return Object.entries(parsed)
      .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
      .join(", ");
  } catch (_) {
    return String(raw);
  }
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "className") node.className = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
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

function mkSelect(name, options) {
  const s = el("select", { dataset: { control: name } });
  for (const [val, label, selected] of options) s.append(mkOpt(val, label, !!selected));
  return s;
}

function buildHeader() {
  const h2 = el("h2", null, "Veil Audit Log");
  const sub = el("div", { className: "subtitle" },
    "Recent submit, delete, solve, and guess-cap events for the Veil game");
  return el("header", null, h2, sub);
}

function buildControls(onChange) {
  const actionSel = mkSelect("action", [
    ["", "All", true],
    ["submit", "Submit"],
    ["delete", "Delete"],
    ["solve", "Solve"],
    ["guess_cap_hit", "Guess Cap Hit"],
  ]);
  const limitSel = mkSelect("limit", [
    ["50", "50"], ["100", "100", true], ["200", "200"], ["500", "500"],
  ]);
  const refreshBtn = el("button", { type: "button", className: "btn" }, "Refresh");
  actionSel.addEventListener("change", onChange);
  limitSel.addEventListener("change", onChange);
  refreshBtn.addEventListener("click", onChange);
  return {
    node: el("div", { className: "controls" },
      el("label", null, "Action", actionSel),
      el("label", null, "Show", limitSel),
      refreshBtn,
    ),
    actionSel, limitSel,
  };
}

function buildRow(e) {
  const label = ACTION_LABELS[e.action] || e.action;
  const badgeClass = `badge ${ACTION_BADGE[e.action] || ""}`;
  const round = e.round_id != null ? `#${e.round_id}` : "—";
  return el("tr", null,
    el("td", null, el("span", { className: badgeClass }, label)),
    el("td", null, round),
    el("td", { className: "user-cell" }, String(e.actor_id)),
    el("td", { className: "reason-cell" }, fmtDetails(e.details)),
    el("td", null, fmtTs(e.ts)),
  );
}

function buildTable(events) {
  const head = el("thead", null,
    el("tr", null,
      el("th", null, "Action"),
      el("th", null, "Round"),
      el("th", null, "Actor"),
      el("th", null, "Details"),
      el("th", null, "Time"),
    ),
  );
  const body = el("tbody");
  for (const e of events) body.append(buildRow(e));
  const summary = el("div",
    { style: "color:var(--ink-dim);font-size:12px;margin-bottom:8px;" },
    `Showing ${events.length} event(s)`,
  );
  const wrap = document.createDocumentFragment();
  wrap.append(summary, el("table", { className: "data-table" }, head, body));
  return wrap;
}

function setEmpty(wrap, message, cls = "empty") {
  wrap.replaceChildren(el("div", { className: cls }, message));
}

export function mount(container) {
  container.replaceChildren();
  const tableWrap = el("div", { className: "table-scroll" });
  setEmpty(tableWrap, "Loading…");

  let actionSel, limitSel;
  const refresh = async () => {
    const params = { limit: limitSel.value };
    if (actionSel.value) params.action = actionSel.value;
    try {
      const data = await api("/api/veil/audit", params);
      if (!data.events.length) {
        setEmpty(tableWrap, "No audit events yet.");
        return;
      }
      tableWrap.replaceChildren(buildTable(data.events));
    } catch (err) {
      setEmpty(tableWrap, err.message, "error");
    }
  };

  const controls = buildControls(refresh);
  actionSel = controls.actionSel;
  limitSel = controls.limitSel;

  const panel = el("div", { className: "panel" },
    buildHeader(),
    controls.node,
    tableWrap,
  );
  container.append(panel);
  refresh();

  return { unmount() {} };
}
