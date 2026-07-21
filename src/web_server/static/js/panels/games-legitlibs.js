import { api, apiPost, esc } from "../api.js";
import { apiPut, apiDelete, showStatus } from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const TIER_LABELS = { 1: "Flirty", 2: "Spicy", 3: "Filthy", 4: "Unhinged" };
const TIER_EMOJI  = { 1: "\u{1F338}", 2: "\u{1F336}️", 3: "\u{1F525}", 4: "\u{1F480}" };
const TIER_CHIP   = { 1: "chip-success", 2: "chip-warning", 3: "chip-danger", 4: "chip-danger" };
const TIER_COLOR  = { 1: "#3ba55d", 2: "#e3a12f", 3: "#ed4245", 4: "#b73ba5" };
const STATUS_CHIP = { published: "chip-success", draft: "chip-neutral", archived: "chip-neutral" };

export function mount(container) {
  let axesData = null;
  let currentTier = "";
  let currentStatus = "";
  let currentTagFilter = "";
  let cachedTemplates = [];

  const tierOptions = [1, 2, 3, 4].map((t) => `<option value="${t}">${TIER_EMOJI[t]} ${TIER_LABELS[t]}</option>`).join("");
  const statusOptions = ["draft", "published", "archived"].map((s) => `<option value="${s}">${s}</option>`).join("");

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>LegitLibs Templates</h2>
        <div class="subtitle">Manage LegitLibs fill-in-the-blank game templates. Use {blank_id} placeholders in body text.</div>
      </header>

      <section>
        <div class="section-label">Filters</div>
        <div class="controls">
          <div class="field m-0">
            <label>Tier
              <select data-ctrl="filter-tier">
                <option value="">All</option>
                ${tierOptions}
              </select>
            </label>
          </div>
          <div class="field m-0">
            <label>Status
              <select data-ctrl="filter-status">
                <option value="">All</option>
                ${statusOptions}
              </select>
            </label>
          </div>
          <div class="field m-0">
            <label>Tag / title
              <input type="text" data-ctrl="filter-tag" placeholder="Search…" style="width:120px;" />
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
        <details class="form-section">
          <summary class="form-section-summary">New Template</summary>
          <div data-region="new-form" style="margin-top:12px;">
            ${buildTemplateFormHtml("new", tierOptions, statusOptions)}
          </div>
        </details>
      </section>
    </div>
  `;

  function ctrl(name) { return container.querySelector(`[data-ctrl="${name}"]`); }
  function region(name) { return container.querySelector(`[data-region="${name}"]`); }

  // -- Blanks table helpers ---------------------------------------------------

  function buildPosSelect(selectedValue) {
    const sel = document.createElement("select");
    sel.className = "blank-pos";
    sel.style.fontSize = "12px";
    const none = document.createElement("option");
    none.value = ""; none.textContent = "—";
    sel.appendChild(none);
    for (const p of (axesData?.pos_values || [])) {
      const opt = document.createElement("option");
      opt.value = p.value;
      opt.textContent = p.min_tier > 1 ? `${p.value} (T${p.min_tier}+)` : p.value;
      if (p.value === selectedValue) opt.selected = true;
      sel.appendChild(opt);
    }
    return sel;
  }

  function populateCascadeSelect(sel, entries, selectedValue) {
    while (sel.options.length > 1) sel.remove(1);
    if (!entries || !entries.length) { sel.disabled = true; sel.value = ""; return; }
    sel.disabled = false;
    for (const e of entries) {
      const v = typeof e === "string" ? e : e.value;
      const tier = typeof e === "object" ? (e.min_tier || 1) : 1;
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = tier > 1 ? `${v} (T${tier}+)` : v;
      if (v === selectedValue) opt.selected = true;
      sel.appendChild(opt);
    }
  }

  function refreshRowFromPos(row, preserveSelection) {
    const pos = row.querySelector(".blank-pos").value;
    const domSel = row.querySelector(".blank-domain");
    const formSel = row.querySelector(".blank-form");
    const keepDomain = preserveSelection ? (domSel.dataset.initial || domSel.value) : "";
    const keepForm = preserveSelection ? (formSel.dataset.initial || formSel.value) : "";
    populateCascadeSelect(domSel, (axesData?.domains_by_pos || {})[pos] || [], keepDomain);
    populateCascadeSelect(formSel, (axesData?.forms_by_pos || {})[pos] || [], keepForm);
    if (!preserveSelection) { domSel.dataset.initial = ""; formSel.dataset.initial = ""; }
  }

  function syncBlanksEmpty(blanksSection) {
    const empty = !blanksSection.querySelector(".blanks-tbody").rows.length;
    blanksSection.querySelector(".blanks-empty").style.display = empty ? "" : "none";
  }

  function addBlankRow(blanksSection, id, pos, domain, form) {
    const tbody = blanksSection.querySelector(".blanks-tbody");
    const tr = document.createElement("tr");

    const tdId = document.createElement("td");
    tdId.style.padding = "3px 4px";
    const inp = document.createElement("input");
    inp.type = "text"; inp.className = "blank-id";
    inp.style.cssText = "font-family:monospace;font-size:12px;width:80px;";
    inp.value = id || ""; inp.placeholder = "b1";
    tdId.appendChild(inp);

    const tdPos = document.createElement("td");
    tdPos.style.padding = "3px 4px";
    const posSel = buildPosSelect(pos || "");
    tdPos.appendChild(posSel);

    const tdDom = document.createElement("td");
    tdDom.style.padding = "3px 4px";
    const domSel = document.createElement("select");
    domSel.className = "blank-domain"; domSel.style.fontSize = "12px";
    domSel.dataset.initial = domain || "";
    domSel.innerHTML = `<option value="">— (none)</option>`; domSel.disabled = true;
    tdDom.appendChild(domSel);

    const tdForm = document.createElement("td");
    tdForm.style.padding = "3px 4px";
    const formSel = document.createElement("select");
    formSel.className = "blank-form"; formSel.style.fontSize = "12px";
    formSel.dataset.initial = form || "";
    formSel.innerHTML = `<option value="">— (none)</option>`; formSel.disabled = true;
    tdForm.appendChild(formSel);

    const tdBtn = document.createElement("td");
    tdBtn.style.padding = "3px 4px";
    const removeBtn = document.createElement("button");
    removeBtn.type = "button"; removeBtn.className = "btn btn-sm"; removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => { tr.remove(); syncBlanksEmpty(blanksSection); });
    tdBtn.appendChild(removeBtn);

    tr.append(tdId, tdPos, tdDom, tdForm, tdBtn);
    tbody.appendChild(tr);
    refreshRowFromPos(tr, true);
    posSel.addEventListener("change", () => refreshRowFromPos(tr, false));
    syncBlanksEmpty(blanksSection);
  }

  function gatherBlanksFromTable(formEl) {
    const tbody = formEl.querySelector(".blanks-tbody");
    if (!tbody || !tbody.rows.length) return null;
    const seen = new Set();
    const rows = [...tbody.rows].flatMap((tr) => {
      const id = tr.querySelector(".blank-id")?.value?.trim() || "";
      if (!id || seen.has(id)) return [];
      seen.add(id);
      const pos = tr.querySelector(".blank-pos")?.value || "";
      const domain = tr.querySelector(".blank-domain")?.value || "";
      const form = tr.querySelector(".blank-form")?.value || "";
      const obj = { id, pos };
      if (domain) obj.domain = domain;
      if (form) obj.form = form;
      return [obj];
    });
    return rows.length ? rows : null;
  }

  function renderStoryPreview(formEl) {
    const bodyEl = formEl.querySelector(".template-body");
    const previewEl = formEl.querySelector(".story-preview");
    const previewBody = formEl.querySelector(".story-preview-body");
    if (!bodyEl || !previewEl || !previewBody) return;

    const text = bodyEl.value;
    if (!text.trim()) { previewEl.style.display = "none"; return; }

    const blankMap = {};
    const tbody = formEl.querySelector(".blanks-tbody");
    if (tbody) {
      for (const tr of tbody.rows) {
        const id = tr.querySelector(".blank-id")?.value?.trim();
        const pos = tr.querySelector(".blank-pos")?.value || "";
        const domain = tr.querySelector(".blank-domain")?.value || "";
        const form = tr.querySelector(".blank-form")?.value || "";
        if (id) {
          const parts = [pos || "?"];
          if (domain) parts.push(domain);
          if (form) parts.push(form);
          blankMap[id] = parts.join(" · ");
        }
      }
    }

    let html = "";
    for (const part of text.split(/(\{[^}]+\})/)) {
      const m = part.match(/^\{([^}]+)\}$/);
      if (m) {
        const id = m[1];
        const label = blankMap[id];
        const cls = label ? "chip-warning" : "chip-neutral";
        const display = label ? `${esc(id)}: ${esc(label)}` : esc(id);
        html += `<span class="chip ${cls}" style="font-family:monospace;font-size:12px;">{${display}}</span>`;
      } else {
        html += esc(part).replace(/\n/g, "<br>");
      }
    }
    previewBody.innerHTML = html;
    previewEl.style.display = "";
  }

  async function checkResolutions(formEl, prefix) {
    const resultsEl = formEl.querySelector(".resolution-results");
    if (!resultsEl) return;
    const blanks = gatherBlanksFromTable(formEl);
    if (!blanks || !blanks.length) {
      resultsEl.innerHTML = `<div class="empty" style="font-size:12px;">No blanks to resolve.</div>`;
      return;
    }
    const tierEl = formEl.querySelector(`[data-ctrl="${prefix}-tier"]`);
    const tier = parseInt(tierEl?.value) || 2;
    resultsEl.innerHTML = `<div class="empty" style="font-size:12px;">Checking…</div>`;
    try {
      const data = await apiPost("/api/games/legitlibs/resolve", { blanks, tier });
      const rows = (data.resolutions || []).map((r) => {
        const cell = r.error
          ? `<span style="color:var(--red,#f55);">! ${esc(r.error)}</span>`
          : `<em>${esc(r.prompt || "")}</em>${r.examples_preview
              ? ` <span style="color:var(--ink-dim,#888);">e.g. ${esc(r.examples_preview)}</span>`
              : ""}`;
        return `<tr>
          <td style="padding:3px 6px;font-family:monospace;font-size:12px;color:var(--ink-dim,#888);">{${esc(r.marker)}}</td>
          <td style="padding:3px 6px;font-size:12px;color:var(--ink-dim,#888);">${esc(r.axis_label)}</td>
          <td style="padding:3px 6px;font-size:12px;">${cell}</td>
        </tr>`;
      }).join("");
      resultsEl.innerHTML = rows
        ? `<table style="width:100%;border-collapse:collapse;margin-top:4px;"><tbody>${rows}</tbody></table>`
        : `<div class="empty" style="font-size:12px;">No resolutions returned.</div>`;
    } catch (err) {
      resultsEl.innerHTML = `<div class="empty" style="font-size:12px;color:var(--red,#f55);">Error: ${esc(err.message)}</div>`;
    }
  }

  function initBlanksTable(formEl, prefix) {
    const blanksSection = formEl.querySelector(".blanks-section");
    if (!blanksSection) return;

    blanksSection.querySelector('[data-action="add-blank-row"]').addEventListener("click", () => {
      addBlankRow(blanksSection, "", "", "", "");
    });

    blanksSection.querySelector('[data-action="ai-prep"]').addEventListener("click", async () => {
      const bodyEl = formEl.querySelector(".template-body");
      const tierEl = formEl.querySelector(`[data-ctrl$="-tier"]`);
      const btn = blanksSection.querySelector('[data-action="ai-prep"]');
      if (!bodyEl) return;
      const rawText = bodyEl.value.trim();
      if (!rawText) { toast("Paste some text into the Body field first.", "info"); return; }
      const tier = parseInt(tierEl?.value) || 2;
      const origLabel = btn.textContent;
      btn.disabled = true; btn.textContent = "Working…";
      try {
        const result = await apiPost("/api/games/legitlibs/ai-prep", { raw_text: rawText, tier });
        bodyEl.value = result.body;
        blanksSection.querySelector(".blanks-tbody").replaceChildren();
        for (const b of result.blanks) addBlankRow(blanksSection, b.id || "", b.pos || "", b.domain || "", b.form || "");
        syncBlanksEmpty(blanksSection);
        renderStoryPreview(formEl);
      } catch (err) {
        toast(`AI prep failed: ${err.message}`, "error");
      } finally {
        btn.disabled = false; btn.textContent = origLabel;
      }
    });

    blanksSection.querySelector('[data-action="detect-blanks"]').addEventListener("click", () => {
      const bodyEl = formEl.querySelector(".template-body");
      if (!bodyEl) return;
      const unique = [...new Set([...bodyEl.value.matchAll(/\{([^}]+)\}/g)].map((m) => m[1]))];
      if (!unique.length) { toast("No {blank_id} placeholders found in body.", "info"); return; }
      const tbody = blanksSection.querySelector(".blanks-tbody");
      const existing = new Set([...tbody.querySelectorAll(".blank-id")].map((i) => i.value.trim()));
      for (const id of unique) {
        if (!existing.has(id)) addBlankRow(blanksSection, id, "", "", "");
      }
      renderStoryPreview(formEl);
    });

    blanksSection.querySelector('[data-action="check-resolutions"]').addEventListener("click", () => {
      checkResolutions(formEl, prefix);
    });

    blanksSection.querySelector(".blanks-tbody").addEventListener("change", () => renderStoryPreview(formEl));

    const bodyEl = formEl.querySelector(".template-body");
    if (bodyEl) bodyEl.addEventListener("input", () => renderStoryPreview(formEl));
  }

  // -- Data loading -----------------------------------------------------------

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
      cachedTemplates = data.templates || [];
      renderList(cachedTemplates, currentTagFilter);
    } catch (err) {
      listEl.innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
    }
  }

  function parseTags(raw) {
    if (Array.isArray(raw)) return raw;
    if (typeof raw === "string" && raw) return raw.split(",").map((s) => s.trim()).filter(Boolean);
    return [];
  }

  function renderList(templates, tagFilter) {
    const listEl = region("list");

    let visible = templates;
    if (tagFilter) {
      const lc = tagFilter.toLowerCase();
      visible = templates.filter((t) => {
        const tags = parseTags(t.tags);
        return tags.some((tag) => tag.toLowerCase().includes(lc)) || t.title.toLowerCase().includes(lc);
      });
    }

    if (!visible.length) {
      listEl.innerHTML = templates.length
        ? `<div class="empty">No templates match "${esc(tagFilter)}".</div>`
        : `<div class="empty">No templates yet. Create the first one below.</div>`;
      return;
    }

    const counts = { published: 0, draft: 0, archived: 0 };
    for (const t of templates) counts[t.status] = (counts[t.status] || 0) + 1;

    const statsHtml = `<div class="ll-stats-bar">
      <span class="field-hint m-0">${templates.length} template${templates.length === 1 ? "" : "s"}</span>
      <span class="chip chip-success" style="font-size:11px;">&#10003; ${counts.published} published</span>
      ${counts.draft ? `<span class="chip chip-neutral" style="font-size:11px;">${counts.draft} draft</span>` : ""}
      ${counts.archived ? `<span class="chip chip-neutral" style="font-size:11px;">${counts.archived} archived</span>` : ""}
    </div>`;

    const cards = visible.map((t) => {
      const tierColor = TIER_COLOR[t.tier] || "#888";
      const tierEmoji = TIER_EMOJI[t.tier] || "";
      const tierLabel = TIER_LABELS[t.tier] || t.tier;
      const tierCls = TIER_CHIP[t.tier] || "chip-neutral";
      const statusCls = STATUS_CHIP[t.status] || "chip-neutral";
      const tags = parseTags(t.tags);
      const tagsHtml = tags.map((tag) => `<span class="ll-tag">${esc(tag)}</span>`).join("");
      const publishBtn = t.status !== "published"
        ? `<button class="btn btn-sm btn-primary" data-action="publish-template" data-tid="${t.template_id}">Publish</button>`
        : `<button class="btn btn-sm" data-action="unpublish-template" data-tid="${t.template_id}">Unpublish</button>`;
      const playerRange = t.player_min
        ? `<span class="ll-stat">${t.player_min}${t.player_max ? "–" + t.player_max : "+"} players</span>`
        : "";
      return `<div class="ll-card" data-tid="${t.template_id}">
        <div class="ll-accent" style="background:${tierColor};"></div>
        <div class="ll-body">
          <div style="display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap;">
            <div style="flex:1;min-width:0;">
              <div class="ll-title">${esc(t.title)}</div>
              <div class="ll-id">#${t.template_id}</div>
              ${tagsHtml ? `<div class="ll-tags">${tagsHtml}</div>` : ""}
            </div>
            <div style="display:flex;gap:4px;flex-wrap:wrap;flex-shrink:0;align-items:flex-start;">
              <span class="chip ${tierCls}" style="font-size:11px;">${tierEmoji} ${esc(tierLabel)}</span>
              <span class="chip ${statusCls}" style="font-size:11px;">${esc(t.status)}</span>
            </div>
          </div>
          <div class="ll-stats">
            <span class="ll-stat"><strong>${t.blanks_count || 0}</strong> blanks</span>
            <span class="ll-stat"><strong>${t.use_count || 0}</strong> plays</span>
            ${playerRange}
          </div>
        </div>
        <div class="ll-actions">
          <button class="btn btn-sm" data-action="edit-template" data-tid="${t.template_id}">Edit</button>
          ${publishBtn}
          <button class="btn btn-sm" data-action="del-template" data-tid="${t.template_id}">Delete</button>
        </div>
      </div>`;
    }).join("");

    listEl.innerHTML = `${statsHtml}<div class="ll-list">${cards}</div>`;

    listEl.querySelectorAll('[data-action="del-template"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!(await confirmDialog(`Delete template #${btn.dataset.tid}?`, { danger: true, confirmLabel: "Delete" }))) return;
        try {
          await apiDelete(`/api/games/legitlibs/templates/${btn.dataset.tid}`);
          loadList();
        } catch (err) { toast(`Delete failed: ${err.message}`, "error"); }
      });
    });

    listEl.querySelectorAll('[data-action="publish-template"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await apiPut(`/api/games/legitlibs/templates/${btn.dataset.tid}`, { status: "published" });
          loadList();
        } catch (err) { toast(`Publish failed: ${err.message}`, "error"); }
      });
    });

    listEl.querySelectorAll('[data-action="unpublish-template"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await apiPut(`/api/games/legitlibs/templates/${btn.dataset.tid}`, { status: "draft" });
          loadList();
        } catch (err) { toast(`Unpublish failed: ${err.message}`, "error"); }
      });
    });

    listEl.querySelectorAll('[data-action="edit-template"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const t = await api(`/api/games/legitlibs/templates/${btn.dataset.tid}`);
          openEditForm(t, btn.closest(".ll-card"));
        } catch (err) { toast(`Load failed: ${err.message}`, "error"); }
      });
    });
  }

  function openEditForm(t, card) {
    const existing = region("list").querySelector(".ll-edit-form");
    if (existing) existing.remove();

    const editDiv = document.createElement("div");
    editDiv.className = "ll-edit-form";
    const prefix = `edit-${t.template_id}`;
    editDiv.innerHTML = buildTemplateFormHtml(prefix, tierOptions, statusOptions);
    card.after(editDiv);

    function ef(name) { return editDiv.querySelector(`[data-ctrl="${prefix}-${name}"]`); }
    ef("title").value = t.title || "";
    ef("body").value = typeof t.body === "string" ? t.body : "";
    ef("tier").value = t.tier || 1;
    ef("status").value = t.status || "draft";
    ef("tags").value = Array.isArray(t.tags) ? t.tags.join(", ") : (t.tags || "");
    ef("player_min").value = t.player_min || "";
    ef("player_max").value = t.player_max || "";
    ef("notes").value = t.notes || "";

    initBlanksTable(editDiv, prefix);
    const blanksSection = editDiv.querySelector(".blanks-section");
    if (Array.isArray(t.blanks)) {
      for (const b of t.blanks) {
        addBlankRow(blanksSection, b.id || "", b.pos || "", b.domain || "", b.form || "");
      }
    }
    renderStoryPreview(editDiv);

    editDiv.querySelector('[data-action="cancel-form"]').addEventListener("click", () => editDiv.remove());
    editDiv.querySelector('[data-action="save-form"]').addEventListener("click", async () => {
      const st = editDiv.querySelector('[data-status="form"]');
      try {
        await apiPut(`/api/games/legitlibs/templates/${t.template_id}`, gatherFormBody(editDiv, prefix));
        editDiv.remove();
        loadList();
      } catch (err) { showStatus(st, false, err.message); }
    });
  }

  // -- New form setup ---------------------------------------------------------

  const newFormEl = region("new-form");
  initBlanksTable(newFormEl, "new");

  newFormEl.querySelector('[data-action="cancel-form"]').addEventListener("click", () => {
    newFormEl.querySelectorAll("input,textarea,select").forEach((el) => {
      el.value = el.tagName === "SELECT" ? el.options[0]?.value || "" : "";
    });
    newFormEl.querySelector(".blanks-tbody").innerHTML = "";
    syncBlanksEmpty(newFormEl.querySelector(".blanks-section"));
    newFormEl.querySelector(".story-preview").style.display = "none";
    newFormEl.querySelector(".resolution-results").innerHTML = "";
  });

  newFormEl.querySelector('[data-action="save-form"]').addEventListener("click", async () => {
    const st = newFormEl.querySelector('[data-status="form"]');
    try {
      const r = await apiPost("/api/games/legitlibs/templates", gatherFormBody(newFormEl, "new"));
      showStatus(st, true, `Created #${r.template_id}`);
      loadList();
    } catch (err) { showStatus(st, false, err.message); }
  });

  function gatherFormBody(formEl, prefix) {
    function fv(name) {
      const el = formEl.querySelector(`[data-ctrl="${prefix}-${name}"]`);
      return el ? el.value : "";
    }
    const blanks = gatherBlanksFromTable(formEl);
    return {
      title: fv("title"),
      body: fv("body"),
      tier: parseInt(fv("tier")) || 1,
      tags: fv("tags") || "",
      status: fv("status") || "draft",
      player_min: parseInt(fv("player_min")) || null,
      player_max: parseInt(fv("player_max")) || null,
      blanks: blanks ? JSON.stringify(blanks) : null,
      notes: fv("notes") || null,
    };
  }

  // -- Filter -----------------------------------------------------------------

  container.querySelector('[data-action="filter"]').addEventListener("click", () => {
    currentTier = ctrl("filter-tier").value;
    currentStatus = ctrl("filter-status").value;
    currentTagFilter = ctrl("filter-tag").value.trim();
    loadList();
  });

  ctrl("filter-tag").addEventListener("input", () => {
    currentTagFilter = ctrl("filter-tag").value.trim();
    renderList(cachedTemplates, currentTagFilter);
  });

  loadAxes().then(() => loadList());

  return { unmount() {} };
}

function buildTemplateFormHtml(prefix, tierOptions, statusOptions) {
  return `<div class="form">
    <div class="field-row">
      <div class="field" style="flex:2;min-width:200px;">
        <label>Title<input class="w-full" type="text" data-ctrl="${prefix}-title" /></label>
      </div>
      <div class="field">
        <label>Tier<select data-ctrl="${prefix}-tier">${tierOptions}</select></label>
      </div>
      <div class="field">
        <label>Status<select data-ctrl="${prefix}-status">${statusOptions}</select></label>
      </div>
      <div class="field">
        <label>Min players<input type="number" data-ctrl="${prefix}-player_min" style="width:70px;" min="2" /></label>
      </div>
      <div class="field">
        <label>Max players<input type="number" data-ctrl="${prefix}-player_max" style="width:70px;" /></label>
      </div>
    </div>
    <div class="field">
      <label>Body <small style="font-weight:normal;color:var(--ink-dim,#888);">use {blank_id} for fill-in slots</small>
        <textarea data-ctrl="${prefix}-body" class="template-body" rows="5" style="width:100%;font-family:monospace;"></textarea>
      </label>
    </div>
    <div class="card story-preview" style="display:none;">
      <div style="font-size:11px;font-weight:600;color:var(--ink-dim);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px;">Story Preview</div>
      <div class="story-preview-body" style="line-height:1.9;font-size:14px;"></div>
    </div>
    <div class="card blanks-section">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <div>
          <span style="font-weight:600;font-size:13px;">Blanks</span>
          <span class="field-hint" style="margin-left:6px;">map each {bN} marker to a type</span>
        </div>
        <div style="display:flex;gap:6px;">
          <button type="button" class="btn btn-sm btn-primary" data-action="ai-prep">AI prep</button>
          <button type="button" class="btn btn-sm" data-action="detect-blanks">Detect from Body</button>
          <button type="button" class="btn btn-sm" data-action="add-blank-row">+ Row</button>
        </div>
      </div>
      <table class="data-table" style="font-size:12px;">
        <thead>
          <tr>
            <th style="width:90px;">Marker</th>
            <th style="width:120px;">POS</th>
            <th style="width:140px;">Domain</th>
            <th style="width:140px;">Form</th>
            <th style="width:36px;"></th>
          </tr>
        </thead>
        <tbody class="blanks-tbody"></tbody>
      </table>
      <p class="blanks-empty empty" style="padding:10px 0;margin:0;">
        No blanks yet — click Detect from Body or + Row.
      </p>
      <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--rule);">
        <button type="button" class="btn btn-sm" data-action="check-resolutions">Check Resolutions</button>
        <div class="resolution-results mt-8"></div>
      </div>
    </div>
    <div class="field-row">
      <div class="field" style="flex:2;">
        <label>Tags <small style="font-weight:normal;color:var(--ink-dim,#888);">comma-separated</small>
          <input class="w-full" type="text" data-ctrl="${prefix}-tags" />
        </label>
      </div>
      <div class="field" style="flex:1;">
        <label>Notes
          <textarea class="w-full" data-ctrl="${prefix}-notes" rows="2"></textarea>
        </label>
      </div>
    </div>
    <div class="controls" style="padding:0;">
      <button type="button" class="btn btn-primary" data-action="save-form">Save</button>
      <button type="button" class="btn" data-action="cancel-form">Cancel</button>
      <span data-status="form" class="save-status"></span>
    </div>
  </div>`;
}