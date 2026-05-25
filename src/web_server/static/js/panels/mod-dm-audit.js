import { api } from "../api.js";

const ACTION_LABELS = {
  request_asked:        "Request Asked",
  request_accepted:     "Request Accepted",
  request_denied:       "Request Denied",
  request_expired:      "Request Expired",
  relationship_revoked: "Revoked",
};

const ACTION_BADGE = {
  request_asked:        "badge-info",
  request_accepted:     "badge-success",
  request_denied:       "badge-danger",
  request_expired:      "badge-dim",
  relationship_revoked: "badge-warning",
};

const TYPE_LABELS = { dm: "DM", friend: "Friend Request" };
const TYPE_BADGE  = { dm: "badge-info", friend: "badge-warning" };

function parseType(notes) {
  if (!notes) return null;
  const m = notes.match(/^type=(\w+)/);
  return m ? m[1] : null;
}

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
      else if (k === "title") node.title = v;
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

function mkOpt(value, label) {
  const o = el("option", null, label);
  o.value = value;
  return o;
}

function badge(text, cls) {
  return el("span", { className: `badge ${cls}` }, text);
}

function setContent(wrap, node) {
  wrap.replaceChildren(node instanceof Node ? node : document.createTextNode(String(node)));
}

function buildRow(e) {
  const label    = ACTION_LABELS[e.action] || e.action;
  const badgeCls = ACTION_BADGE[e.action] || "";
  const actor = e.actor_name  || e.actor_id  || "—";
  const userA = e.user_a_name || e.user_a_id || "—";
  const userB = e.user_b_name || e.user_b_id || "—";

  const reqType = parseType(e.notes);
  const restNotes = reqType
    ? (e.notes.replace(/^type=\w+[,;]?\s*/, "") || "—")
    : (e.notes || "—");

  const actionTd = el("td", null, badge(label, badgeCls));
  if (reqType) {
    actionTd.append(
      " ",
      badge(TYPE_LABELS[reqType] || reqType, TYPE_BADGE[reqType] || "badge-dim"),
    );
  }

  return el("tr", null,
    actionTd,
    el("td", null, actor),
    el("td", null, userA),
    el("td", null, userB),
    el("td", { className: "reason-cell", title: restNotes }, restNotes),
    el("td", null, fmtTs(e.timestamp)),
  );
}

function buildTable(data) {
  const summary = el("div",
    { style: "color:var(--ink-dim);font-size:12px;margin-bottom:8px;" },
    `Showing ${data.entries.length} of ${data.total} entries`,
  );
  const head = el("thead", null,
    el("tr", null,
      el("th", null, "Action"),
      el("th", null, "Actor"),
      el("th", null, "User A"),
      el("th", null, "User B"),
      el("th", null, "Notes"),
      el("th", null, "Time"),
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

  const actionSel = el("select", null,
    mkOpt("", "All Actions"),
    mkOpt("request_asked", "Request Asked"),
    mkOpt("request_accepted", "Request Accepted"),
    mkOpt("request_denied", "Request Denied"),
    mkOpt("request_expired", "Request Expired"),
    mkOpt("relationship_revoked", "Revoked"),
  );
  const typeSel = el("select", null,
    mkOpt("", "All Types"),
    mkOpt("dm", "DM"),
    mkOpt("friend", "Friend Request"),
  );
  const limitSel = el("select", null,
    mkOpt("50", "50"), mkOpt("100", "100"), mkOpt("200", "200"),
  );
  const tableWrap = el("div", { className: "table-scroll" });
  setContent(tableWrap, el("div", { className: "empty" }, "Loading…"));

  const refresh = async () => {
    const params = { limit: limitSel.value };
    if (actionSel.value) params.action = actionSel.value;
    if (typeSel.value) params.type = typeSel.value;
    try {
      const data = await api("/api/moderation/dm-audit", params);
      if (!data.entries.length) {
        setContent(tableWrap, el("div", { className: "empty" }, "No DM audit entries found."));
        return;
      }
      tableWrap.replaceChildren(buildTable(data));
    } catch (err) {
      setContent(tableWrap, el("div", { className: "error" }, err.message));
    }
  };

  actionSel.addEventListener("change", refresh);
  typeSel.addEventListener("change", refresh);
  limitSel.addEventListener("change", refresh);

  const panel = el("div", { className: "panel" },
    el("header", null,
      el("h2", null, "DM Audit Log"),
      el("div", { className: "subtitle" }, "DM permission request and relationship history"),
    ),
    el("div", { className: "controls" },
      el("label", null, "Action ", actionSel),
      el("label", null, "Type ", typeSel),
      el("label", null, "Show ", limitSel),
    ),
    tableWrap,
  );
  container.append(panel);
  refresh();

  return { unmount() {} };
}
