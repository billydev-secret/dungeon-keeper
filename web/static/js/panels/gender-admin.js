import { api, apiPost, esc } from "../api.js";
import { renderSortableTable } from "../table.js";

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
    const buttons = GENDERS.map((g) => `<button data-gender="${g.value}">${g.label}</button>`).join(" ");
    classifyPane.innerHTML = `
      <div style="padding:24px; text-align:center;">
        <div style="font-size:1.5em; margin-bottom:8px;">${esc(m.display_name || m.user_id)}</div>
        <div class="subtitle" style="margin-bottom:16px;">${cursor + 1} of ${queue.length}</div>
        <div style="display:flex; gap:8px; justify-content:center;">${buttons}<button data-skip>Skip</button></div>
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

  async function loadList() {
    listPane.textContent = "Loading…";
    try {
      const data = await api("/api/gender/list", {});
      if (!data.classified.length) {
        listPane.textContent = "No tagged members yet.";
        return;
      }
      listPane.innerHTML = `<div data-table></div>`;
      renderSortableTable(listPane.querySelector("[data-table]"), {
        columns: [
          { key: "display_name", label: "Member", format: (v, r) => esc(r.display_name || r.user_id) },
          { key: "gender", label: "Gender" },
          { key: "set_at", label: "Tagged at", format: (v) => v ? new Date(v * 1000).toLocaleString() : "" },
        ],
        data: data.classified,
        defaultSort: "set_at",
      });
    } catch (err) {
      listPane.textContent = `Error: ${err.message}`;
    }
  }

  loadClassify();
  return { unmount() {} };
}
