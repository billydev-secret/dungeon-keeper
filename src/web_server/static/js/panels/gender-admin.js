import { api, apiPost, esc } from "../api.js";

const GENDERS = [
  { value: "male",      label: "Male" },
  { value: "female",    label: "Female" },
  { value: "nonbinary", label: "Non-binary" },
];

export function mount(container) {
  const html = `
    <div class="panel">
      <header>
        <h2>Gender Tagging</h2>
        <div class="subtitle">Tag members for NSFW analytics. Tags are private; only used for the NSFW-by-gender report.</div>
      </header>
      <div class="tabs" style="margin-bottom:12px;">
        <button data-tab="classify" class="tab-btn active">Classify Next</button>
        <button data-tab="list" class="tab-btn">Tagged Members</button>
      </div>
      <div data-pane="classify"></div>
      <div data-pane="list" style="display:none;"></div>
    </div>
  `;
  container.innerHTML = html;

  const classifyPane = container.querySelector('[data-pane="classify"]');
  const listPane = container.querySelector('[data-pane="list"]');
  container.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      container.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      classifyPane.style.display = btn.dataset.tab === "classify" ? "" : "none";
      listPane.style.display = btn.dataset.tab === "list" ? "" : "none";
      if (btn.dataset.tab === "list") loadList();
      else loadClassify();
    });
  });

  let queue = [];
  let cursor = 0;

  async function loadClassify() {
    classifyPane.textContent = "Loading unclassified members…";
    try {
      const data = await api("/api/gender/unclassified", {});
      queue = data.members || [];
      cursor = 0;
      renderClassify();
    } catch (err) {
      classifyPane.textContent = `Error: ${err.message}`;
    }
  }

  function renderClassify() {
    if (cursor >= queue.length) {
      classifyPane.innerHTML = `<div class="empty">All members tagged. 🎉<br><br>Total tagged: <span data-tagged-count></span></div>`;
      api("/api/gender/list", {}).then((d) => {
        const el = classifyPane.querySelector("[data-tagged-count]");
        if (el) el.textContent = d.classified.length;
      }).catch(() => {});
      return;
    }
    const m = queue[cursor];
    const buttons = GENDERS.map((g) => `<button class="btn" data-gender="${g.value}">${g.label}</button>`).join(" ");
    classifyPane.innerHTML = `
      <div style="padding:24px; text-align:center;">
        <div style="font-size:1.5em; margin-bottom:8px;">${esc(m.display_name || m.user_id)}</div>
        <div class="subtitle" style="margin-bottom:16px;">${cursor + 1} of ${queue.length}</div>
        <div style="display:flex; gap:8px; justify-content:center;">${buttons}<button class="btn btn-ghost" data-skip>Skip</button></div>
        <div data-cstatus style="margin-top:12px;"></div>
      </div>
    `;
    classifyPane.querySelectorAll("[data-gender]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const status = classifyPane.querySelector("[data-cstatus]");
        status.textContent = "Saving…";
        try {
          await apiPost("/api/gender/set", { user_id: m.user_id, gender: btn.dataset.gender });
          cursor++;
          renderClassify();
        } catch (err) {
          status.textContent = `Error: ${err.message}`;
        }
      });
    });
    classifyPane.querySelector("[data-skip]").addEventListener("click", () => { cursor++; renderClassify(); });
  }

  // Persists across tab switches so filter/sort survive a reload.
  const listState = { all: [], sortKey: "set_at", sortAsc: false, search: "", genderFilter: "" };

  function listRows() {
    const q = listState.search.trim().toLowerCase();
    const filtered = listState.all.filter((r) => {
      if (listState.genderFilter && r.gender !== listState.genderFilter) return false;
      if (q && !(r.display_name || r.user_id || "").toLowerCase().includes(q)) return false;
      return true;
    });
    const k = listState.sortKey, asc = listState.sortAsc;
    return filtered.sort((a, b) => {
      let av = k === "display_name" ? (a.display_name || a.user_id || "") : a[k];
      let bv = k === "display_name" ? (b.display_name || b.user_id || "") : b[k];
      if (av == null) av = "";
      if (bv == null) bv = "";
      if (typeof av === "string" && typeof bv === "string") {
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      return asc ? av - bv : bv - av;
    });
  }

  function genderSelectHtml(userId, current) {
    const opts = GENDERS.map(
      (g) => `<option value="${g.value}"${g.value === current ? " selected" : ""}>${g.label}</option>`,
    ).join("");
    return `<select class="gender-edit" data-user="${esc(userId)}">${opts}</select>`;
  }

  function renderListTable() {
    const body = listPane.querySelector("[data-table-body]");
    const countEl = listPane.querySelector("[data-count]");
    if (!body) return;
    const rows = listRows();
    countEl.textContent = `${rows.length} of ${listState.all.length} tagged`;
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="4" style="padding:20px; color:var(--ink-dim); text-align:center;">No members match this filter.</td></tr>`;
    } else {
      body.innerHTML = rows.map((r) => {
        const when = r.set_at ? new Date(r.set_at * 1000).toLocaleString() : "";
        return `<tr>
          <td>${esc(r.display_name || r.user_id)}</td>
          <td>${genderSelectHtml(r.user_id, r.gender)}</td>
          <td>${esc(when)}</td>
          <td><span data-status style="font-size:12px;"></span></td>
        </tr>`;
      }).join("");
    }
    listPane.querySelectorAll("th[data-sort]").forEach((th) => {
      th.classList.remove("sort-asc", "sort-desc");
      if (th.dataset.sort === listState.sortKey) {
        th.classList.add(listState.sortAsc ? "sort-asc" : "sort-desc");
      }
    });
  }

  async function loadList() {
    listPane.textContent = "Loading…";
    try {
      const data = await api("/api/gender/list", {});
      listState.all = data.classified || [];
      if (!listState.all.length) {
        listPane.textContent = "No tagged members yet.";
        return;
      }
      listPane.innerHTML = `
        <div class="controls">
          <label>Search
            <input type="text" data-search placeholder="Member name…" />
          </label>
          <label>Gender
            <select data-gender-filter>
              <option value="">All</option>
              ${GENDERS.map((g) => `<option value="${g.value}">${g.label}</option>`).join("")}
            </select>
          </label>
          <span class="subtitle" data-count style="margin-left:auto;"></span>
        </div>
        <div style="max-height:520px; overflow-y:auto;">
          <table class="data-table">
            <thead><tr>
              <th data-sort="display_name" style="cursor:pointer;">Member</th>
              <th data-sort="gender" style="cursor:pointer;">Gender</th>
              <th data-sort="set_at" style="cursor:pointer;">Tagged at</th>
              <th>Saved</th>
            </tr></thead>
            <tbody data-table-body></tbody>
          </table>
        </div>
      `;

      const searchEl = listPane.querySelector("[data-search]");
      const filterEl = listPane.querySelector("[data-gender-filter]");
      searchEl.value = listState.search;
      filterEl.value = listState.genderFilter;
      renderListTable();

      searchEl.addEventListener("input", () => { listState.search = searchEl.value; renderListTable(); });
      filterEl.addEventListener("change", () => { listState.genderFilter = filterEl.value; renderListTable(); });

      listPane.querySelector("thead").addEventListener("click", (e) => {
        const th = e.target.closest("th[data-sort]");
        if (!th) return;
        const key = th.dataset.sort;
        if (listState.sortKey === key) {
          listState.sortAsc = !listState.sortAsc;
        } else {
          listState.sortKey = key;
          listState.sortAsc = key !== "set_at"; // names ascending, newest-first for dates
        }
        renderListTable();
      });

      // One delegated listener survives every re-render of the tbody.
      listPane.querySelector("[data-table-body]").addEventListener("change", async (e) => {
        const sel = e.target.closest("select.gender-edit");
        if (!sel) return;
        const userId = sel.dataset.user;
        const gender = sel.value;
        const statusEl = sel.closest("tr").querySelector("[data-status]");
        const rec = listState.all.find((r) => String(r.user_id) === String(userId));
        const prev = rec ? rec.gender : null;
        statusEl.textContent = "Saving…";
        statusEl.style.color = "var(--ink-dim)";
        sel.disabled = true;
        try {
          await apiPost("/api/gender/set", { user_id: userId, gender });
          if (rec) { rec.gender = gender; rec.set_at = Date.now() / 1000; }
          statusEl.textContent = "✓ Saved";
          statusEl.style.color = "var(--green)";
        } catch (err) {
          if (rec) rec.gender = prev;
          sel.value = prev;
          statusEl.textContent = `Error: ${err.message}`;
          statusEl.style.color = "var(--red)";
        } finally {
          sel.disabled = false;
        }
      });
    } catch (err) {
      listPane.textContent = `Error: ${err.message}`;
    }
  }

  loadClassify();
  return { unmount() {} };
}
