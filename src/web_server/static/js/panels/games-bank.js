import { api, apiPost, esc } from "../api.js";
import { apiPut, apiDelete, showStatus } from "../config-helpers.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const GAME_TYPES = ["wyr", "nhie", "mlt", "rushmore", "price", "clapback", "ama"];
const GAME_ICONS = { wyr: "🤔", nhie: "⛔", mlt: "👑", rushmore: "🗿", price: "💰", clapback: "⚔️", ama: "🎙️" };
const GAME_NAMES = {
  wyr: "Would You Rather", nhie: "Never Have I Ever", mlt: "Most Likely To",
  rushmore: "Mt. Rushmore Draft", price: "Name Your Price", clapback: "Clapback", ama: "Anonymous AMA",
};

export function mount(container) {
  let currentPage = 1;
  let currentGameType = "";
  let currentCategory = "";
  let currentSearch = "";

  const gtOptions = GAME_TYPES.map((g) => `<option value="${g}">${GAME_ICONS[g] || ""} ${GAME_NAMES[g] || g}</option>`).join("");

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Question Bank</h2>
        <div class="subtitle">Browse, edit, add, and bulk-import questions for all game types.</div>
      </header>
      <div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap;">
        <div style="flex:1;min-width:0;">
          <section>
            <div class="section-label">Filters</div>
            <div class="form" style="display:flex;flex-direction:row;gap:8px;flex-wrap:wrap;align-items:flex-end;max-width:none;">
              <div class="field" style="margin:0;">
                <label>Game type
                  <select data-ctrl="filter-gt">
                    <option value="">All</option>
                    ${gtOptions}
                  </select>
                </label>
              </div>
              <div class="field" style="margin:0;">
                <label>Category
                  <select data-ctrl="filter-cat">
                    <option value="">All</option>
                    <option value="sfw">SFW</option>
                    <option value="nsfw">NSFW</option>
                  </select>
                </label>
              </div>
              <div class="field" style="margin:0;flex:1;min-width:160px;">
                <label>Search
                  <input type="text" data-ctrl="filter-search" placeholder="keyword" style="width:100%;" />
                </label>
              </div>
              <button class="btn btn-primary" data-action="filter">Filter</button>
            </div>
          </section>
          <section style="margin-top:16px;">
            <div class="section-label">Questions</div>
            <div data-region="table-wrap"><div class="empty">Loading</div></div>
            <div data-region="pagination" style="display:flex;gap:8px;align-items:center;margin-top:8px;"></div>
          </section>
        </div>
        <div style="width:200px;flex-shrink:0;">
          <div class="section-label">Stats</div>
          <div data-region="stats"><div class="empty">Loading</div></div>
        </div>
      </div>

      <section style="margin-top:16px;">
        <div class="section-label">Add Question</div>
        <div class="form" style="max-width:520px;">
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <div class="field" style="margin:0;flex:1;min-width:140px;">
              <label>Game type<select data-ctrl="add-gt">${gtOptions}</select></label>
            </div>
            <div class="field" style="margin:0;">
              <label>Category
                <select data-ctrl="add-cat">
                  <option value="sfw">SFW</option>
                  <option value="nsfw">NSFW</option>
                </select>
              </label>
            </div>
          </div>
          <div class="field">
            <label>Question text
              <textarea data-ctrl="add-text" rows="3" placeholder="Enter question text"></textarea>
            </label>
          </div>
          <div style="display:flex;align-items:center;gap:8px;">
            <button class="btn btn-primary" data-action="add">Add Question</button>
            <span data-status="add" class="save-status"></span>
          </div>
        </div>
      </section>

      <section style="margin-top:16px;">
        <div class="section-label">Bulk Add</div>
        <div class="form" style="max-width:520px;">
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <div class="field" style="margin:0;flex:1;min-width:140px;">
              <label>Game type<select data-ctrl="bulk-gt">${gtOptions}</select></label>
            </div>
            <div class="field" style="margin:0;">
              <label>Category
                <select data-ctrl="bulk-cat">
                  <option value="sfw">SFW</option>
                  <option value="nsfw">NSFW</option>
                </select>
              </label>
            </div>
          </div>
          <div class="field">
            <label>Questions (one per line)
              <textarea data-ctrl="bulk-text" rows="6" placeholder="Line 1&#10;Line 2&#10;..."></textarea>
            </label>
          </div>
          <div style="display:flex;align-items:center;gap:8px;">
            <button class="btn btn-primary" data-action="bulk">Bulk Add</button>
            <span data-status="bulk" class="save-status"></span>
          </div>
        </div>
      </section>

      <section style="margin-top:16px;">
        <div class="section-label">Import / Export</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;">
          <div class="field" style="margin:0;">
            <label>Export game type
              <select data-ctrl="export-gt">
                <option value="">All</option>
                ${gtOptions}
              </select>
            </label>
          </div>
          <button class="btn" data-action="export">Export JSON</button>
          <div style="flex:1;min-width:20px;"></div>
          <div class="field" style="margin:0;">
            <label>Import JSON file
              <input type="file" data-ctrl="import-file" accept=".json" />
            </label>
          </div>
          <button class="btn btn-primary" data-action="import">Import</button>
          <span data-status="import" class="save-status"></span>
        </div>
      </section>
    </div>
  `;

  function ctrl(name) { return container.querySelector(`[data-ctrl="${name}"]`); }
  function region(name) { return container.querySelector(`[data-region="${name}"]`); }
  function statusEl(name) { return container.querySelector(`[data-status="${name}"]`); }

  async function loadStats() {
    try {
      const data = await api("/api/games/stats");
      const el = region("stats");
      const bbt = data.bank_by_type || {};
      let rows = "";
      for (const gt of GAME_TYPES) {
        const entry = bbt[gt] || {};
        const icon = GAME_ICONS[gt] || "";
        rows += `<tr><td title="${esc(GAME_NAMES[gt] || gt)}">${icon} ${esc(gt)}</td><td class="num">${entry.sfw || 0}</td><td class="num">${entry.nsfw || 0}</td></tr>`;
      }
      el.innerHTML = `<table style="font-size:12px;width:100%;"><thead><tr><th>Game</th><th class="num" style="text-align:right;">SFW</th><th class="num" style="text-align:right;">NSFW</th></tr></thead><tbody>${rows}</tbody></table>
        <div style="margin-top:8px;font-size:12px;color:var(--ink-dim);">Total: <b style="color:var(--ink-bright);">${data.total_questions}</b>&ensp;Games: <b style="color:var(--ink-bright);">${data.games_played}</b></div>`;
    } catch (_) {
      region("stats").innerHTML = `<div class="empty">Stats unavailable</div>`;
    }
  }

  async function loadTable() {
    const wrap = region("table-wrap");
    wrap.innerHTML = `<div class="empty">Loading</div>`;
    try {
      const params = { page: currentPage, per_page: 50 };
      if (currentGameType) params.game_type = currentGameType;
      if (currentCategory) params.category = currentCategory;
      if (currentSearch) params.search = currentSearch;
      const data = await api("/api/games/bank", params);
      renderTable(data);
      renderPagination(data.total, data.page, data.total_pages);
    } catch (err) {
      wrap.innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
    }
  }

  function renderTable(data) {
    const wrap = region("table-wrap");
    if (!data.questions.length) {
      wrap.innerHTML = `<div class="empty">No questions found.</div>`;
      return;
    }
    let rows = "";
    for (const q of data.questions) {
      const added = q.added_at ? String(q.added_at).slice(0, 10) : "";
      const safeText = esc(q.question_text).replace(/"/g, "&quot;");
      const qIcon = GAME_ICONS[q.game_type] || "";
      const catChip = q.category === "sfw"
        ? `<span class="chip chip-success">sfw</span>`
        : `<span class="chip chip-danger">nsfw</span>`;
      rows += `<tr data-qid="${q.question_id}">
        <td>${q.question_id}</td>
        <td title="${esc(GAME_NAMES[q.game_type] || q.game_type)}">${qIcon} ${esc(q.game_type)}</td>
        <td>${catChip}</td>
        <td class="q-text" style="word-break:break-word;">${esc(q.question_text)}</td>
        <td style="font-size:12px;color:var(--ink-dim);">${esc(added)}</td>
        <td>
          <button class="btn btn-sm" data-action="edit"
            data-qid="${q.question_id}" data-text="${safeText}" data-cat="${esc(q.category)}">Edit</button>
          <button class="btn btn-sm" data-action="del"
            data-qid="${q.question_id}">Del</button>
        </td>
      </tr>`;
    }
    wrap.innerHTML = `<table class="data-table" style="table-layout:fixed;">
      <thead><tr>
        <th style="width:60px;">ID</th><th style="width:70px;">Game</th>
        <th style="width:55px;">Cat</th><th>Question</th>
        <th style="width:110px;">Added</th><th style="width:100px;">Actions</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

    wrap.querySelectorAll('[data-action="del"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const qid = parseInt(btn.dataset.qid);
        if (!confirm(`Delete question #${qid}?`)) return;
        try {
          await apiDelete(`/api/games/bank/${qid}`);
          loadTable(); loadStats();
        } catch (err) { alert(`Delete failed: ${err.message}`); }
      });
    });

    wrap.querySelectorAll('[data-action="edit"]').forEach((btn) => {
      btn.addEventListener("click", () => {
        const qid = parseInt(btn.dataset.qid);
        const row = wrap.querySelector(`tr[data-qid="${qid}"]`);
        const textCell = row.querySelector(".q-text");
        const origText = btn.dataset.text
          .replace(/&quot;/g, '"').replace(/&amp;/g, "&")
          .replace(/&lt;/g, "<").replace(/&gt;/g, ">");
        const origCat = btn.dataset.cat;

        textCell.innerHTML = `<textarea rows="3" style="width:100%;" data-edit-text></textarea>
          <select data-edit-cat style="margin-top:4px;">
            <option value="sfw">sfw</option><option value="nsfw">nsfw</option>
          </select>`;
        textCell.querySelector("[data-edit-text]").value = origText;
        textCell.querySelector("[data-edit-cat]").value = origCat;

        const actionsCell = row.cells[5];
        actionsCell.innerHTML = `
          <button class="btn btn-primary btn-sm" data-action="save-edit">Save</button>
          <button class="btn btn-sm" data-action="cancel-edit">Cancel</button>`;

        actionsCell.querySelector('[data-action="cancel-edit"]').addEventListener("click", () => loadTable());
        actionsCell.querySelector('[data-action="save-edit"]').addEventListener("click", async () => {
          const newText = textCell.querySelector("[data-edit-text]").value.trim();
          const newCat = textCell.querySelector("[data-edit-cat]").value;
          if (!newText) { alert("Question text cannot be empty."); return; }
          try {
            await apiPut(`/api/games/bank/${qid}`, { question_text: newText, category: newCat });
            loadTable(); loadStats();
          } catch (err) { alert(`Save failed: ${err.message}`); }
        });
      });
    });
  }

  function renderPagination(total, page, totalPages) {
    const el = region("pagination");
    if (totalPages <= 1) { el.innerHTML = `<span style="font-size:12px;">${total} question(s)</span>`; return; }
    el.innerHTML = `
      <button class="btn" data-pg="prev" ${page <= 1 ? "disabled" : ""}>Prev</button>
      <span style="font-size:12px;">Page ${page} / ${totalPages} (${total} total)</span>
      <button class="btn" data-pg="next" ${page >= totalPages ? "disabled" : ""}>Next</button>`;
    el.querySelector('[data-pg="prev"]')?.addEventListener("click", () => { currentPage--; loadTable(); });
    el.querySelector('[data-pg="next"]')?.addEventListener("click", () => { currentPage++; loadTable(); });
  }

  container.querySelector('[data-action="filter"]').addEventListener("click", () => {
    currentGameType = ctrl("filter-gt").value;
    currentCategory = ctrl("filter-cat").value;
    currentSearch = ctrl("filter-search").value.trim();
    currentPage = 1;
    loadTable();
  });
  ctrl("filter-search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") container.querySelector('[data-action="filter"]').click();
  });

  container.querySelector('[data-action="add"]').addEventListener("click", async () => {
    const st = statusEl("add");
    const gt = ctrl("add-gt").value;
    const cat = ctrl("add-cat").value;
    const text = ctrl("add-text").value.trim();
    if (!text) { showStatus(st, false, "Question text required"); return; }
    try {
      await apiPost("/api/games/bank", { game_type: gt, category: cat, question_text: text });
      ctrl("add-text").value = "";
      showStatus(st, true, "Added");
      loadTable(); loadStats();
    } catch (err) { showStatus(st, false, err.message); }
  });

  container.querySelector('[data-action="bulk"]').addEventListener("click", async () => {
    const st = statusEl("bulk");
    const gt = ctrl("bulk-gt").value;
    const cat = ctrl("bulk-cat").value;
    const lines = ctrl("bulk-text").value.split("\n").map((l) => l.trim()).filter(Boolean);
    if (!lines.length) { showStatus(st, false, "No lines entered"); return; }
    try {
      const r = await apiPost("/api/games/bank/bulk", { game_type: gt, category: cat, lines });
      ctrl("bulk-text").value = "";
      showStatus(st, true, `Added ${r.added}`);
      loadTable(); loadStats();
    } catch (err) { showStatus(st, false, err.message); }
  });

  container.querySelector('[data-action="export"]').addEventListener("click", async () => {
    const gt = ctrl("export-gt").value;
    try {
      const params = {};
      if (gt) params.game_type = gt;
      const data = await api("/api/games/bank/export", params);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `question-bank${gt ? "-" + gt : ""}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) { alert(`Export failed: ${err.message}`); }
  });

  container.querySelector('[data-action="import"]').addEventListener("click", async () => {
    const st = statusEl("import");
    const fileInput = ctrl("import-file");
    if (!fileInput.files.length) { showStatus(st, false, "Select a file first"); return; }
    try {
      const text = await fileInput.files[0].text();
      const parsed = JSON.parse(text);
      const res = await fetch("/api/games/bank/import", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed),
      });
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new Error(b.detail || res.statusText);
      }
      const r = await res.json();
      showStatus(st, true, `Imported ${r.imported}`);
      fileInput.value = "";
      loadTable(); loadStats();
    } catch (err) { showStatus(st, false, err.message); }
  });

  loadStats();
  loadTable();

  return { unmount() {} };
}