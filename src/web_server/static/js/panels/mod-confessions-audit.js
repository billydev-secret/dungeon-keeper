import { api } from "../api.js";

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

function buildRow(e) {
  const jumpLink = el("a",
    { href: `https://discord.com/channels/@me/${e.channel_id}/${e.message_id}`, target: "_blank", rel: "noopener noreferrer" },
    `#${e.message_id}`,
  );
  const threadCell = e.thread_id && e.thread_id !== "0"
    ? el("a",
        { href: `https://discord.com/channels/@me/${e.thread_id}`, target: "_blank", rel: "noopener noreferrer" },
        `#${e.thread_id}`,
      )
    : document.createTextNode("—");
  const text = e.content || "—";
  const truncated = text.length > 120 ? text.slice(0, 120) + "…" : text;
  return el("tr", null,
    el("td", null, e.author_name || e.author_id),
    el("td", { className: "reason-cell", title: text }, truncated),
    el("td", null, jumpLink),
    el("td", null, threadCell),
    el("td", null, fmtTs(e.created_at)),
  );
}

function buildTable(data) {
  const summary = el("div",
    { style: "color:var(--ink-dim);font-size:12px;margin-bottom:8px;" },
    `Showing ${data.entries.length} of ${data.total} confessions`,
  );
  const head = el("thead", null,
    el("tr", null,
      el("th", null, "Author"),
      el("th", null, "Content"),
      el("th", null, "Message"),
      el("th", null, "Thread"),
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

  const limitSel = el("select", null,
    mkOpt("50", "50", true), mkOpt("100", "100"), mkOpt("200", "200"),
  );
  const tableWrap = el("div", { className: "table-scroll" });
  tableWrap.replaceChildren(el("div", { className: "empty" }, "Loading…"));

  const refresh = async () => {
    const params = { limit: limitSel.value };
    try {
      const data = await api("/api/moderation/confessions-audit", params);
      if (!data.entries.length) {
        tableWrap.replaceChildren(el("div", { className: "empty" }, "No confessions found."));
        return;
      }
      tableWrap.replaceChildren(buildTable(data));
    } catch (err) {
      tableWrap.replaceChildren(el("div", { className: "error" }, err.message));
    }
  };

  limitSel.addEventListener("change", refresh);

  const panel = el("div", { className: "panel" },
    el("header", null,
      el("h2", null, "Confessions Audit Log"),
      el("div", { className: "subtitle" }, "Confession submission history with real author identity"),
    ),
    el("div", { className: "controls" },
      el("label", null, "Show ", limitSel),
    ),
    tableWrap,
  );
  container.append(panel);
  refresh();

  return { unmount() {} };
}
