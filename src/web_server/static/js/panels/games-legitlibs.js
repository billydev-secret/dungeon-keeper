import { api, apiPost, esc } from "../api.js";
import { apiPut, apiDelete, showStatus } from "../config-helpers.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const TIER_LABELS = { 1: "Flirty", 2: "Spicy", 3: "Filthy", 4: "Unhinged" };

export function mount(container) {
  let axesData = null;
  let currentTier = "";
  let currentStatus = "";

  const tierOptions = [1, 2, 3, 4].map((t) => `<option value="${t}">${t} — ${TIER_LABELS[t]}</option>`).join("");
  const statusOptions = ["draft", "published", "archived"].map((s) => `<option value="${s}">${s}</option>`).join("");

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>LegitLibs Templates</h2>
        <div class="subtitle">Manage LegitLibs fill-in-the-blank game templates. Use {blank_id} placeholders in body text.</div>
      </header>

      <section>
        <div class="section-label">Filters</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:10px;">
          <div class="field" style="margin:0;">
            <label>Tier
              <select data-ctrl="filter-tier">
                <option value="">All</option>
                ${tierOptions}
              </select>
            </label>
          </div>
          <div class="field" style="margin:0;">
            <label>Status
              <select data-ctrl="filter-status">
                <option value="">All</option>
                ${statusOptions}
              </select>
            </label>
          </div>
          <button class="btn btn-primary" data-action="filter">Filter</button>
        </div>
      </section>

      <section>
        <div class="section-label">Templates</div>
        <div data-region="list"><div class="empty">Loading</div></div>
      </section>

      <section style="margin-top:16px;">
        <details>
          <summary style="cursor:pointer;font-weight:600;padding:8px 0;">New Template</summary>
          <div data-region="new-form" style="margin-top:8px;">
            ${buildTemplateFormHtml("new", tierOptions, statusOptions)}
          </div>
        </details>
      </section>
    </div>
  `;

  function ctrl(name) { return container.querySelector(`[data-ctrl="${name}"]`); }
  function region(name) { return container.querySelector(`[data-region="${name}"]`); }

  // Load axes data
  async function loadAxes() {
    try {
      axesData = await api("/api/games/legitlibs/axes");
    } catch (_) {
      axesData = { pos_values: [], domains_by_pos: {}, forms_by_pos: {} };
    }
  }

  async function loadList() {
    const listEl = region("list");
    listEl.innerHTML = `<div class="empty">Loading</div>`;
    try {
      const params = {};
      if (currentTier) params.tier = currentTier;
      if (currentStatus) params.status = currentStatus;
      const data = await api("/api/games/legitlibs/templates", params);
      renderList(data.templates || []);
    } catch (err) {
      listEl.innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
    }
  }

  function renderList(templates) {
    const listEl = region("list");
    if (!templates.length) {
      listEl.innerHTML = `<div class="empty">No templates found.</div>`;
      return;
    }
    let rows = "";
    for (const t of templates) {
      const tierLabel = TIER_LABELS[t.tier] || t.tier;
      rows += `<tr data-tid="${t.template_id}">
        <td>${esc(t.title)}</td>
        <td>${t.tier} (${esc(tierLabel)})</td>
        <td>${esc(t.status)}</td>
        <td>${t.blanks_count || 0}</td>
        <td>${t.use_count || 0}</td>
        <td>
          <button class="btn" style="padding:2px 6px;font-size:12px;" data-action="edit-template" data-tid="${t.template_id}">Edit</button>
          ${t.status !== "published" ? `<button class="btn btn-primary" style="padding:2px 6px;font-size:12px;" data-action="publish-template" data-tid="${t.template_id}">Publish</button>` : ""}
          <button class="btn" style="padding:2px 6px;font-size:12px;" data-action="del-template" data-tid="${t.template_id}">Del</button>
        </td>
      </tr>`;
    }
    listEl.innerHTML = `<table style="width:100%;">
      <thead><tr>
        <th>Title</th><th>Tier</th><th>Status</th><th>Blanks</th><th>Uses</th><th style="width:140px;">Actions</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

    listEl.querySelectorAll('[data-action="del-template"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const tid = btn.dataset.tid;
        if (!confirm(`Delete template #${tid}?`)) return;
        try {
          await apiDelete(`/api/games/legitlibs/templates/${tid}`);
          loadList();
        } catch (err) { alert(`Delete failed: ${err.message}`); }
      });
    });

    listEl.querySelectorAll('[data-action="publish-template"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const tid = btn.dataset.tid;
        try {
          await apiPut(`/api/games/legitlibs/templates/${tid}`, { status: "published" });
          loadList();
        } catch (err) { alert(`Publish failed: ${err.message}`); }
      });
    });

    listEl.querySelectorAll('[data-action="edit-template"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const tid = btn.dataset.tid;
        try {
          const t = await api(`/api/games/legitlibs/templates/${tid}`);
          openEditForm(t, btn.closest("tr"));
        } catch (err) { alert(`Load failed: ${err.message}`); }
      });
    });
  }

  function openEditForm(t, row) {
    // Remove any existing edit row
    const existing = region("list").querySelector("tr.edit-row");
    if (existing) existing.remove();

    const editRow = document.createElement("tr");
    editRow.className = "edit-row";
    const td = document.createElement("td");
    td.colSpan = 6;
    td.innerHTML = buildTemplateFormHtml(`edit-${t.template_id}`, tierOptions, statusOptions);
    editRow.appendChild(td);
    row.after(editRow);

    // Fill fields
    const prefix = `edit-${t.template_id}`;
    function ef(name) { return td.querySelector(`[data-ctrl="${prefix}-${name}"]`); }
    ef("title").value = t.title || "";
    ef("body").value = typeof t.body === "string" ? t.body : "";
    ef("tier").value = t.tier || 1;
    ef("status").value = t.status || "draft";
    ef("tags").value = Array.isArray(t.tags) ? t.tags.join(", ") : (t.tags || "");
    ef("player_min").value = t.player_min || "";
    ef("player_max").value = t.player_max || "";
    ef("notes").value = t.notes || "";

    if (Array.isArray(t.blanks)) {
      ef("blanks").value = JSON.stringify(t.blanks, null, 2);
    } else if (t.blanks) {
      ef("blanks").value = typeof t.blanks === "string" ? t.blanks : JSON.stringify(t.blanks);
    }

    // Wire save/cancel
    td.querySelector('[data-action="cancel-form"]').addEventListener("click", () => editRow.remove());
    td.querySelector('[data-action="save-form"]').addEventListener("click", async () => {
      const st = td.querySelector('[data-status="form"]');
      try {
        const body = gatherFormBody(td, prefix);
        await apiPut(`/api/games/legitlibs/templates/${t.template_id}`, body);
        editRow.remove();
        loadList();
      } catch (err) { showStatus(st, false, err.message); }
    });

    td.querySelector('[data-action="detect-blanks"]')?.addEventListener("click", () => detectBlanks(td, prefix));
  }

  // New template form
  const newFormEl = region("new-form");
  const prefix = "new";
  newFormEl.querySelector('[data-action="cancel-form"]').addEventListener("click", () => {
    newFormEl.querySelectorAll("input,textarea,select").forEach((el) => { el.value = el.tagName === "SELECT" ? el.options[0]?.value || "" : ""; });
  });
  newFormEl.querySelector('[data-action="save-form"]').addEventListener("click", async () => {
    const st = newFormEl.querySelector('[data-status="form"]');
    try {
      const body = gatherFormBody(newFormEl, prefix);
      const r = await apiPost("/api/games/legitlibs/templates", body);
      showStatus(st, true, `Created #${r.template_id}`);
      loadList();
    } catch (err) { showStatus(st, false, err.message); }
  });
  newFormEl.querySelector('[data-action="detect-blanks"]')?.addEventListener("click", () => detectBlanks(newFormEl, prefix));

  function gatherFormBody(formEl, prefix) {
    function fv(name) {
      const el = formEl.querySelector(`[data-ctrl="${prefix}-${name}"]`);
      return el ? el.value : "";
    }
    return {
      title: fv("title"),
      body: fv("body"),
      tier: parseInt(fv("tier")) || 1,
      tags: fv("tags") || null,
      status: fv("status") || "draft",
      player_min: parseInt(fv("player_min")) || null,
      player_max: parseInt(fv("player_max")) || null,
      blanks: fv("blanks") || null,
      notes: fv("notes") || null,
    };
  }

  function detectBlanks(formEl, prefix) {
    const bodyEl = formEl.querySelector(`[data-ctrl="${prefix}-body"]`);
    const blanksEl = formEl.querySelector(`[data-ctrl="${prefix}-blanks"]`);
    if (!bodyEl || !blanksEl) return;
    const text = bodyEl.value;
    const matches = [...text.matchAll(/\{([^}]+)\}/g)].map((m) => m[1]);
    const unique = [...new Set(matches)];
    if (!unique.length) { alert("No {blank_id} placeholders found in body."); return; }

    const posValues = (axesData?.pos_values || []).map((p) => p.value);
    const posOpts = posValues.map((v) => `<option value="${v}">${v}</option>`).join("");

    const blanks = unique.map((id) => {
      const domainsForPos = (axesData?.domains_by_pos || {});
      return {
        id,
        pos: posValues[0] || "",
        domain: "",
        form: "",
      };
    });

    let blanksHtml = `<div style="border:1px solid var(--border,#333);border-radius:6px;padding:10px;margin-top:6px;">
      <div style="font-weight:600;margin-bottom:8px;">Detected blanks (${unique.length})</div>`;
    for (const b of blanks) {
      const domainsForPos = (axesData?.domains_by_pos || {})[b.pos] || [];
      const domainOpts = domainsForPos.map((d) => `<option value="${d}">${d}</option>`).join("");
      const formsForPos = (axesData?.forms_by_pos || {})[b.pos] || [];
      const formOpts = formsForPos.map((f) => `<option value="${f}">${f}</option>`).join("");
      blanksHtml += `<div style="display:flex;gap:6px;align-items:center;margin-bottom:4px;">
        <span style="min-width:80px;font-size:12px;">{${esc(b.id)}}</span>
        <select data-blank-pos="${esc(b.id)}">${posOpts}</select>
        <select data-blank-domain="${esc(b.id)}"><option value="">(any)</option>${domainOpts}</select>
        <select data-blank-form="${esc(b.id)}"><option value="">(any)</option>${formOpts}</select>
      </div>`;
    }
    blanksHtml += `<button class="btn btn-primary" style="margin-top:6px;" data-action="apply-blanks">Apply to Blanks JSON</button></div>`;

    // Insert/replace blanks editor below blanks textarea
    const existingEditor = formEl.querySelector(".blanks-editor");
    if (existingEditor) existingEditor.remove();
    const editorDiv = document.createElement("div");
    editorDiv.className = "blanks-editor";
    editorDiv.innerHTML = blanksHtml;
    blanksEl.after(editorDiv);

    editorDiv.querySelector('[data-action="apply-blanks"]').addEventListener("click", () => {
      const result = unique.map((id) => {
        const pos = editorDiv.querySelector(`[data-blank-pos="${id}"]`)?.value || "";
        const domain = editorDiv.querySelector(`[data-blank-domain="${id}"]`)?.value || "";
        const form = editorDiv.querySelector(`[data-blank-form="${id}"]`)?.value || "";
        const obj = { id, pos };
        if (domain) obj.domain = domain;
        if (form) obj.form = form;
        return obj;
      });
      blanksEl.value = JSON.stringify(result, null, 2);
    });
  }

  // Filter
  container.querySelector('[data-action="filter"]').addEventListener("click", () => {
    currentTier = ctrl("filter-tier").value;
    currentStatus = ctrl("filter-status").value;
    loadList();
  });

  loadAxes().then(() => loadList());

  return { unmount() {} };
}

function buildTemplateFormHtml(prefix, tierOptions, statusOptions) {
  return `<div class="form">
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <div class="field" style="flex:1;min-width:200px;">
        <label>Title<input type="text" data-ctrl="${prefix}-title" style="width:100%;" /></label>
      </div>
      <div class="field" style="margin:0;">
        <label>Tier<select data-ctrl="${prefix}-tier">${tierOptions}</select></label>
      </div>
      <div class="field" style="margin:0;">
        <label>Status<select data-ctrl="${prefix}-status">${statusOptions}</select></label>
      </div>
      <div class="field" style="margin:0;">
        <label>Min players<input type="number" data-ctrl="${prefix}-player_min" style="width:70px;" min="2" /></label>
      </div>
      <div class="field" style="margin:0;">
        <label>Max players<input type="number" data-ctrl="${prefix}-player_max" style="width:70px;" /></label>
      </div>
    </div>
    <div class="field">
      <label>Body (use {blank_id} for fill-in slots)
        <textarea data-ctrl="${prefix}-body" rows="5" style="width:100%;font-family:monospace;"></textarea>
      </label>
      <button class="btn" style="padding:2px 8px;font-size:12px;margin-top:4px;" data-action="detect-blanks">Detect blanks</button>
    </div>
    <div class="field">
      <label>Blanks JSON (auto-filled by Detect)
        <textarea data-ctrl="${prefix}-blanks" rows="3" style="width:100%;font-family:monospace;" placeholder='[{"id":"x","pos":"noun"}]'></textarea>
      </label>
    </div>
    <div class="field">
      <label>Tags (comma-separated)
        <input type="text" data-ctrl="${prefix}-tags" style="width:100%;" />
      </label>
    </div>
    <div class="field">
      <label>Notes
        <textarea data-ctrl="${prefix}-notes" rows="2" style="width:100%;"></textarea>
      </label>
    </div>
    <div style="display:flex;gap:8px;">
      <button class="btn btn-primary" data-action="save-form">Save</button>
      <button class="btn" data-action="cancel-form">Cancel</button>
      <span data-status="form" class="save-status" style="margin-left:8px;"></span>
    </div>
  </div>`;
}