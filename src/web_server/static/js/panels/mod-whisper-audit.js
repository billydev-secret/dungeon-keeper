import { api } from "../api.js";

const STATE_LABELS = {
  pending:  "Pending",
  expired:  "Expired",
  rejected: "Rejected",
  accepted: "Accepted",
};

const STATE_BADGE = {
  pending:  "badge-info",
  expired:  "badge-dim",
  rejected: "badge-danger",
  accepted: "badge-success",
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

function yesNo(val, yesCls, noCls) {
  return el("span", { className: `badge ${val ? yesCls : noCls}` }, val ? "Yes" : "No");
}

function buildRow(e) {
  const stateLabel = STATE_LABELS[e.state] || e.state;
  const stateCls   = STATE_BADGE[e.state]  || "";
  const reports = e.report_count > 0
    ? el("span", { className: "badge badge-danger" }, String(e.report_count))
    : document.createTextNode("0");
  return el("tr", null,
    el("td", null, String(e.id)),
    el("td", null, e.sender_name || e.sender_id),
    el("td", null, e.target_name || e.target_id),
    el("td", null, badge(stateLabel, stateCls)),
    el("td", null, yesNo(e.solved, "badge-success", "badge-dim")),
    el("td", null, yesNo(e.exposed, "badge-warning", "badge-dim")),
    el("td", null, reports),
    el("td", null, fmtTs(e.created_at)),
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
      el("th", null, "Sender"),
      el("th", null, "Target"),
      el("th", null, "State"),
      el("th", null, "Solved"),
      el("th", null, "Exposed"),
      el("th", null, "Reports"),
      el("th", null, "Sent"),
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

  const stateSel = el("select", null,
    mkOpt("", "All", true),
    mkOpt("pending", "Pending"),
    mkOpt("expired", "Expired"),
    mkOpt("rejected", "Rejected"),
    mkOpt("accepted", "Accepted"),
  );
  const limitSel = el("select", null,
    mkOpt("50", "50"), mkOpt("100", "100", true), mkOpt("200", "200"),
  );
  const reportedCb = el("input", { type: "checkbox" });
  const tableWrap = el("div", { className: "table-scroll" });
  tableWrap.replaceChildren(el("div", { className: "empty" }, "Loading…"));

  const refresh = async () => {
    const params = { limit: limitSel.value };
    if (stateSel.value) params.state = stateSel.value;
    if (reportedCb.checked) params.reported_only = "true";
    try {
      const data = await api("/api/moderation/whisper-audit", params);
      if (!data.entries.length) {
        tableWrap.replaceChildren(el("div", { className: "empty" }, "No whispers found."));
        return;
      }
      tableWrap.replaceChildren(buildTable(data));
    } catch (err) {
      tableWrap.replaceChildren(el("div", { className: "error" }, err.message));
    }
  };

  stateSel.addEventListener("change", refresh);
  limitSel.addEventListener("change", refresh);
  reportedCb.addEventListener("change", refresh);

  const panel = el("div", { className: "panel" },
    el("header", null,
      el("h2", null, "Whisper Audit Log"),
      el("div", { className: "subtitle" },
        "Whisper send history — reflects current record state, not a durable event log"),
    ),
    el("div", { className: "controls" },
      el("label", null, "State ", stateSel),
      el("label", null, "Show ", limitSel),
      el("label", null, reportedCb, " Reported only"),
    ),
    tableWrap,
  );
  container.append(panel);
  refresh();

  return { unmount() {} };
}
