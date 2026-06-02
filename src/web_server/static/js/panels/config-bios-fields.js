import { api, apiPost, esc } from "../api.js";
import { apiDelete, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading fields…</div></div>`;

  let state = { template_version: 0, fields: [], headline_warning: false };

  function render() {
    const warningHtml = state.headline_warning
      ? `<div class="save-status save-err" style="margin-bottom:0.5rem;">
           ⚠️ No active field is flagged as the headline. The first active field
           (by sort order) will be used as the embed title.
         </div>`
      : "";

    const rows = state.fields.map((f, i) => `
      <tr data-id="${f.id}" data-active="${f.active ? "1" : "0"}">
        <td>${i + 1}</td>
        <td><span class="label-cell">${esc(f.label)}</span></td>
        <td>${esc(f.field_type)}</td>
        <td>${f.required ? "✓" : ""}</td>
        <td>
          <input type="radio" name="headline" ${f.is_headline ? "checked" : ""}
                 ${f.field_type === "short" && f.active ? "" : "disabled"}
                 title="${f.field_type === "short" ? "" : "Headline must be a short field"}"
                 data-headline="${f.id}" />
        </td>
        <td>${f.active ? "active" : "<em>retired</em>"}</td>
        <td>
          <button type="button" class="btn btn-secondary" data-up ${i === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="btn btn-secondary" data-down ${i === state.fields.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="btn btn-secondary" data-edit>Edit</button>
          ${f.active ? `<button type="button" class="btn btn-danger" data-retire>Retire</button>` : ""}
          <span data-row-status></span>
        </td>
      </tr>
    `).join("");

    const emptyRow = `<tr><td colspan="7"><em>Bios template is empty — add your first field below.</em></td></tr>`;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Bios — Fields</h2>
          <div class="subtitle">Profile field set (template v${state.template_version}). Retired fields stay so old bios keep their labels.</div>
        </header>
        ${warningHtml}
        <table class="table">
          <thead><tr><th>#</th><th>Label</th><th>Type</th><th>Req.</th><th>Headline</th><th>State</th><th>Actions</th></tr></thead>
          <tbody>${rows || emptyRow}</tbody>
        </table>
        <div style="margin-top:1rem;">
          <button type="button" class="btn btn-primary" data-add>Add field</button>
          <span data-add-status></span>
        </div>
        <div data-editor></div>
      </div>
    `;
    wire();
  }

  function wire() {
    container.querySelector("[data-add]").addEventListener("click", () => openEditor(null));

    container.querySelectorAll("tr[data-id]").forEach((row) => {
      const id = parseInt(row.dataset.id, 10);
      const status = row.querySelector("[data-row-status]");
      const upBtn = row.querySelector("[data-up]");
      const downBtn = row.querySelector("[data-down]");

      if (upBtn) upBtn.addEventListener("click", () => move(id, -1));
      if (downBtn) downBtn.addEventListener("click", () => move(id, +1));

      row.querySelector("[data-edit]").addEventListener("click", () => {
        const f = state.fields.find((x) => x.id === id);
        if (f) openEditor(f);
      });
      const retireBtn = row.querySelector("[data-retire]");
      if (retireBtn) {
        retireBtn.addEventListener("click", async () => {
          if (!confirm("Retire this field? Old bios keep their stored values; new and edited bios won't include it.")) return;
          try {
            await apiDelete(`/api/bios/fields/${id}`);
            await refresh();
          } catch (err) {
            showStatus(status, false, err.message);
          }
        });
      }
      const radio = row.querySelector("[data-headline]");
      if (radio && !radio.disabled) {
        radio.addEventListener("change", async () => {
          if (!radio.checked) return;
          try {
            await apiPut(`/api/bios/fields/${id}`, { is_headline: true });
            await refresh();
          } catch (err) {
            showStatus(status, false, err.message);
            await refresh();
          }
        });
      }
    });
  }

  async function move(id, delta) {
    const ids = state.fields.map((f) => f.id);
    const idx = ids.indexOf(id);
    if (idx < 0) return;
    const target = idx + delta;
    if (target < 0 || target >= ids.length) return;
    [ids[idx], ids[target]] = [ids[target], ids[idx]];
    try {
      await apiPost("/api/bios/fields/reorder", { ordered_ids: ids });
      await refresh();
    } catch (err) {
      alert(`Reorder failed: ${err.message}`);
    }
  }

  function openEditor(field) {
    const editor = container.querySelector("[data-editor]");
    const isNew = field === null;
    const f = field || { label: "", field_type: "short", choices: [], required: false, is_headline: false, max_len: 1024, active: true };
    const choicesText = (f.choices || []).join("\n");
    editor.innerHTML = `
      <div class="panel" style="margin-top:1rem; padding:1rem; border:1px solid var(--border, #333);">
        <h3 style="margin-top:0">${isNew ? "Add field" : "Edit field"}</h3>
        <form class="form" data-editor-form>
          <div class="field">
            <label>Label</label>
            <input type="text" name="label" required value="${esc(f.label)}" maxlength="128" style="width:100%;" />
            <div class="field-hint">What the wizard shows as the prompt — e.g. "Pronouns", "How you found The Golden Meadow".</div>
          </div>
          <div class="field">
            <label>Type</label>
            <select name="field_type">
              <option value="short" ${f.field_type === "short" ? "selected" : ""}>short (single-line)</option>
              <option value="paragraph" ${f.field_type === "paragraph" ? "selected" : ""}>paragraph (multi-line)</option>
              <option value="choice" ${f.field_type === "choice" ? "selected" : ""}>choice (buttons/select)</option>
            </select>
          </div>
          <div class="field" data-choices-field>
            <label>Choices (one per line)</label>
            <textarea name="choices" rows="4" style="width:100%;">${esc(choicesText)}</textarea>
            <div class="field-hint">≤5 lines render as buttons; more render as a select menu.</div>
          </div>
          <div class="field">
            <label><input type="checkbox" name="required" ${f.required ? "checked" : ""} /> Required</label>
          </div>
          <div class="field">
            <label><input type="checkbox" name="is_headline" ${f.is_headline ? "checked" : ""} /> Headline (embed title)</label>
            <div class="field-hint">Must be a short field. Exactly one is recommended.</div>
          </div>
          <div class="field">
            <label>Max length</label>
            <input type="number" name="max_len" min="1" max="4096" value="${f.max_len || 1024}" style="width:7rem;" />
          </div>
          ${isNew ? "" : `
            <div class="field">
              <label><input type="checkbox" name="active" ${f.active ? "checked" : ""} /> Active</label>
              <div class="field-hint">Unchecking soft-retires the field.</div>
            </div>
          `}
          <div>
            <button type="submit" class="btn btn-primary">${isNew ? "Add" : "Save"}</button>
            <button type="button" class="btn btn-secondary" data-cancel>Cancel</button>
            <span data-editor-status></span>
          </div>
        </form>
      </div>
    `;
    const form = editor.querySelector("[data-editor-form]");
    const status = editor.querySelector("[data-editor-status]");
    const typeSel = form.querySelector("select[name=field_type]");
    const choicesField = editor.querySelector("[data-choices-field]");

    const updateChoicesVisibility = () => {
      choicesField.style.display = typeSel.value === "choice" ? "" : "none";
    };
    typeSel.addEventListener("change", updateChoicesVisibility);
    updateChoicesVisibility();

    editor.querySelector("[data-cancel]").addEventListener("click", () => {
      editor.innerHTML = "";
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const body = {
        label: String(fd.get("label") || "").trim(),
        field_type: String(fd.get("field_type") || "short"),
        choices: String(fd.get("choices") || "")
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
        required: fd.get("required") === "on",
        is_headline: fd.get("is_headline") === "on",
        max_len: parseInt(fd.get("max_len"), 10) || 1024,
      };
      if (!isNew) body.active = fd.get("active") === "on";
      try {
        if (isNew) {
          await apiPost("/api/bios/fields", body);
        } else {
          await apiPut(`/api/bios/fields/${field.id}`, body);
        }
        editor.innerHTML = "";
        await refresh();
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  async function refresh() {
    state = await api("/api/bios/fields");
    render();
  }

  refresh();
}
