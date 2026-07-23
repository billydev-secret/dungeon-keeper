import { api } from "../api.js";

const MONTH_NAMES = [
  "", "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const WINDOWS = [
  { value: 30,  label: "Next 30 days" },
  { value: 60,  label: "Next 60 days" },
  { value: 90,  label: "Next 90 days" },
  { value: 365, label: "Full year" },
];

function fmtDate(m, d) {
  return `${MONTH_NAMES[m]} ${d}`;
}

function fmtDaysUntil(n) {
  if (n === 0) return "Today";
  if (n === 1) return "Tomorrow";
  return `${n} days`;
}

function badgeColor(n) {
  if (n === 0) return "#c0392b";
  if (n <= 7)  return "#d4a020";
  return "var(--ink-dim)";
}

function el(tag, styles, text) {
  const node = document.createElement(tag);
  if (styles) node.style.cssText = styles;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function mount(container) {
  const panel = el("div");
  panel.className = "panel";
  container.innerHTML = "";
  container.appendChild(panel);

  const header = document.createElement("header");
  const h2 = el("h2", null, "Birthday Calendar");
  const sub = el("div", null, "Upcoming member birthdays and their requests");
  sub.className = "subtitle";
  header.appendChild(h2);
  header.appendChild(sub);
  panel.appendChild(header);

  // Window selector
  const controls = el("div", null);
  controls.className = "controls";
  const label = el("label", null, "Show Birthdays ");
  const windowEl = document.createElement("select");
  windowEl.setAttribute("data-window", "");
  windowEl.setAttribute("aria-label", "How far ahead to show birthdays");
  for (const w of WINDOWS) {
    const opt = document.createElement("option");
    opt.value = String(w.value);
    opt.textContent = w.label;
    if (w.value === 90) opt.selected = true;
    windowEl.appendChild(opt);
  }
  label.appendChild(windowEl);
  controls.appendChild(label);
  panel.appendChild(controls);

  const summaryEl = el("div", "margin-bottom:12px;");
  summaryEl.className = "subtitle";
  panel.appendChild(summaryEl);

  const listEl = el("div");
  panel.appendChild(listEl);

  async function refresh() {
    const days = parseInt(windowEl.value, 10);
    summaryEl.textContent = "Loading birthdays…";
    listEl.innerHTML = "";

    let entries;
    try {
      entries = await api("/api/birthday/calendar", { days });
    } catch (err) {
      summaryEl.textContent = "";
      const errDiv = el("div", null,
        `Couldn’t load the birthday calendar — try again. (${err.message})`);
      errDiv.className = "error";
      listEl.appendChild(errDiv);
      return;
    }

    if (!entries.length) {
      summaryEl.textContent = "";
      const emptyDiv = el("div", null,
        "No birthdays coming up in this window. Members add theirs with /birthday in "
        + "Discord — widen the window above to look further ahead.");
      emptyDiv.className = "empty";
      listEl.appendChild(emptyDiv);
      return;
    }

    summaryEl.textContent = `${entries.length} birthday${entries.length === 1 ? "" : "s"} coming up`;

    const table = el("table", "width:100%; border-collapse:collapse;");

    // Header row — static, no user data
    const thead = document.createElement("thead");
    const hrow = document.createElement("tr");
    hrow.style.cssText = "text-align:left; border-bottom:2px solid var(--rule);";
    for (const label of ["Member", "Date", "Coming Up In", "Birthday Request"]) {
      const th = el("th", "padding:6px 10px; font-weight:600; font-size:12px; color:var(--ink-dim); text-transform:uppercase; letter-spacing:.05em;", label);
      hrow.appendChild(th);
    }
    thead.appendChild(hrow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const [i, entry] of entries.entries()) {
      const tr = document.createElement("tr");
      tr.style.cssText = `border-bottom:1px solid var(--rule); background:${i % 2 === 0 ? "transparent" : "var(--bg-subtle, rgba(0,0,0,.03))"};`;

      // Member cell — avatar initial + name
      const nameTd = el("td", "padding:10px;");
      const nameWrap = el("div", "display:flex; align-items:center; gap:10px;");
      const initial = el(
        "span",
        "display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:50%;background:var(--accent-dim,#5865f2);color:#fff;font-weight:700;font-size:13px;flex-shrink:0;",
        (entry.name || "?")[0].toUpperCase(),
      );
      const nameSpan = el("span", "font-weight:500;", entry.name);
      nameWrap.appendChild(initial);
      nameWrap.appendChild(nameSpan);
      nameTd.appendChild(nameWrap);

      // Date cell
      const dateTd = el("td", "padding:10px; white-space:nowrap;", fmtDate(entry.birth_month, entry.birth_day));

      // Days-until cell
      const inTd = el("td", "padding:10px; white-space:nowrap;");
      const badge = el("span", `font-weight:600;color:${badgeColor(entry.days_until)};`, fmtDaysUntil(entry.days_until));
      inTd.appendChild(badge);

      // Preference cell
      const prefTd = el("td", "padding:10px; color:var(--ink-dim); font-style:italic; max-width:280px;", entry.preference || "");

      tr.appendChild(nameTd);
      tr.appendChild(dateTd);
      tr.appendChild(inTd);
      tr.appendChild(prefTd);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    listEl.appendChild(table);
  }

  windowEl.addEventListener("change", refresh);
  refresh();
}
