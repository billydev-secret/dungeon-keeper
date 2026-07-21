import { api, apiPost, esc } from "../api.js";
import { toast, confirmDialog } from "../ui.js";
import {
  apiDelete,
  apiPut,
  buildField,
  categorySelect,
  channelSelect,
  loadCategories,
  loadChannels,
  showStatus,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Bios</h2>
        <div class="subtitle">Wizard config, profile fields, and the icebreaker question pool.</div>
      </header>
      <div class="tabs" style="margin-bottom:12px;">
        <button data-tab="config"    class="tab-btn active">Config</button>
        <button data-tab="fields"    class="tab-btn">Fields</button>
        <button data-tab="questions" class="tab-btn">Questions</button>
      </div>
      <div data-pane="config"></div>
      <div data-pane="fields"    style="display:none;"></div>
      <div data-pane="questions" style="display:none;"></div>
    </div>
  `;

  const panes = {
    config: container.querySelector('[data-pane="config"]'),
    fields: container.querySelector('[data-pane="fields"]'),
    questions: container.querySelector('[data-pane="questions"]'),
  };
  const loaded = { config: false, fields: false, questions: false };

  const loaders = {
    config: () => renderConfigTab(panes.config),
    fields: () => renderFieldsTab(panes.fields),
    questions: () => renderQuestionsTab(panes.questions),
  };

  container.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      container.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      for (const [name, pane] of Object.entries(panes)) {
        pane.style.display = btn.dataset.tab === name ? "" : "none";
      }
      const name = btn.dataset.tab;
      if (!loaded[name]) {
        loaded[name] = true;
        loaders[name]();
      }
    });
  });

  // Initial tab
  loaded.config = true;
  loaders.config();
}

// ── Config tab ──────────────────────────────────────────────────────

async function renderConfigTab(pane) {
  pane.innerHTML = `<div class="empty">Loading bios config…</div>`;
  const [config, channels, categories] = await Promise.all([
    api("/api/bios/config"),
    loadChannels(),
    loadCategories(),
  ]);

  const colorVal = (config.embed_color || "#C8763E").replace(/^#/, "");
  const colorHex = `#${colorVal}`;

  pane.innerHTML = `
    <form class="form" data-form></form>
    <div style="margin-top:1rem; padding-top:1rem; border-top:1px solid var(--border, #333);">
      <h3 style="margin-top:0">Trigger button</h3>
      <p>Posts the persistent <strong>📝 Create / Update Bio</strong> button into the configured bios channel, using the <strong>Trigger title</strong> and <strong>Trigger message</strong> above. Members tap it to start the wizard. Re-post after editing the copy to refresh the live message.</p>
      <button type="button" class="btn btn-secondary" data-post-btn>Post trigger button</button>
      <span data-post-status></span>
    </div>
  `;

  const form = pane.querySelector("[data-form]");
  const mkSelect = (name, html) => {
    const el = document.createElement("select");
    el.name = name;
    el.innerHTML = html;
    return el;
  };
  const mkInput = (name, attrs) => {
    const el = document.createElement("input");
    el.name = name;
    Object.assign(el, attrs);
    return el;
  };
  const mkTextarea = (name, attrs) => {
    const el = document.createElement("textarea");
    el.name = name;
    Object.assign(el, attrs);
    return el;
  };

  form.appendChild(buildField(
    "Bios channel",
    mkSelect("bios_channel_id", channelSelect(channels, config.bios_channel_id)),
    "Where finished bio embeds are posted.",
  ));
  form.appendChild(buildField(
    "Wizard category",
    mkSelect("wizard_category_id", categorySelect(categories, config.wizard_category_id)),
    "Throwaway wizard channels are created under this category.",
  ));
  form.appendChild(buildField(
    "Questions per bio",
    mkInput("questions_per_bio", { type: "number", min: "1", max: "10", value: String(config.questions_per_bio || 3) }),
    "How many icebreaker questions are drawn from the pool.",
  ));
  form.appendChild(buildField(
    "Embed color",
    mkInput("embed_color", { type: "color", value: colorHex }),
    "Single ember accent shared across all bio embeds.",
  ));
  form.appendChild(buildField(
    "Wizard timeout (minutes)",
    mkInput("wizard_timeout", { type: "number", min: "1", max: "120", value: String(config.wizard_timeout || 15) }),
    "Idle minutes before a session auto-cancels.",
  ));
  form.appendChild(buildField(
    "Archive grace (seconds)",
    mkInput("archive_grace", { type: "number", min: "0", max: "3600", value: String(config.archive_grace || 60) }),
    "Wait this long after completion before deleting the wizard channel.",
  ));
  form.appendChild(buildField(
    "Trigger title",
    mkInput("trigger_title", { type: "text", maxLength: 256, value: config.trigger_title || "📝 Share your bio", style: "width:100%;" }),
    "Heading on the trigger-button embed in the bios channel.",
  ));
  form.appendChild(buildField(
    "Trigger message",
    mkTextarea("trigger_body", { rows: 3, maxLength: 2000, value: config.trigger_body || "", style: "width:100%;" }),
    "Body under the heading. Discord markdown works. Re-post the button below to apply edits.",
  ));
  const submit = document.createElement("div");
  submit.innerHTML = `<button type="submit" class="btn btn-primary">Save</button><span data-status></span>`;
  form.appendChild(submit);

  const statusEl = pane.querySelector("[data-status]");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    try {
      await apiPut("/api/bios/config", {
        bios_channel_id: String(fd.get("bios_channel_id") || "0"),
        wizard_category_id: String(fd.get("wizard_category_id") || "0"),
        questions_per_bio: parseInt(fd.get("questions_per_bio"), 10) || 3,
        embed_color: String(fd.get("embed_color") || "#C8763E"),
        wizard_timeout: parseInt(fd.get("wizard_timeout"), 10) || 15,
        archive_grace: parseInt(fd.get("archive_grace"), 10) || 60,
        trigger_title: String(fd.get("trigger_title") || "").trim(),
        trigger_body: String(fd.get("trigger_body") || "").trim(),
      });
      showStatus(statusEl, true);
    } catch (err) {
      showStatus(statusEl, false, err.message);
    }
  });

  const postBtn = pane.querySelector("[data-post-btn]");
  const postStatus = pane.querySelector("[data-post-status]");
  postBtn.addEventListener("click", async () => {
    postBtn.disabled = true;
    postStatus.textContent = "Posting…";
    postStatus.className = "save-status";
    try {
      const res = await apiPost("/api/bios/post-trigger-button");
      postStatus.className = "save-status save-ok";
      postStatus.textContent = `Posted (message ${esc(String(res.message_id))}).`;
    } catch (err) {
      postStatus.className = "save-status save-err";
      postStatus.textContent = err.message;
    } finally {
      postBtn.disabled = false;
    }
  });
}

// ── Fields tab ──────────────────────────────────────────────────────

async function renderFieldsTab(pane) {
  pane.innerHTML = `<div class="empty">Loading fields…</div>`;
  let state = { template_version: 0, fields: [], headline_warning: false };

  async function refresh() {
    state = await api("/api/bios/fields");
    render();
  }

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

    pane.innerHTML = `
      <div class="subtitle" style="margin-bottom:0.5rem;">Profile field set (template v${state.template_version}). Retired fields stay so old bios keep their labels.</div>
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
    `;
    wire();
  }

  function wire() {
    pane.querySelector("[data-add]").addEventListener("click", () => openEditor(null));

    pane.querySelectorAll("tr[data-id]").forEach((row) => {
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
          if (!(await confirmDialog("Retire this field? Old bios keep their stored values; new and edited bios won't include it.", { danger: true, confirmLabel: "Retire" }))) return;
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
      toast(`Reorder failed: ${err.message}`, "error");
    }
  }

  function openEditor(field) {
    const editor = pane.querySelector("[data-editor]");
    const isNew = field === null;
    const f = field || { label: "", field_type: "short", choices: [], required: false, is_headline: false, max_len: 1024, active: true, hint: "" };
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
            <label>Example / hint <span style="opacity:0.6;">(optional)</span></label>
            <input type="text" name="hint" value="${esc(f.hint || "")}" maxlength="256" style="width:100%;" placeholder='e.g. "Hollow Knight, or that one indie nobody''s heard of"' />
            <div class="field-hint">Shown under the prompt to help members know what to write. Leave blank for none.</div>
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
        hint: String(fd.get("hint") || "").trim(),
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

  await refresh();
}

// ── Questions tab ───────────────────────────────────────────────────

async function renderQuestionsTab(pane) {
  pane.innerHTML = `<div class="empty">Loading questions…</div>`;
  let questions = [];

  async function refresh() {
    questions = await api("/api/bios/questions");
    render();
  }

  function render() {
    const rows = questions.map((q) => `
      <tr data-id="${q.id}">
        <td><input type="text" data-prompt value="${esc(q.prompt)}" style="width: 100%;" /></td>
        <td><input type="number" data-weight min="1" max="1000" value="${q.weight}" style="width: 5rem;" /></td>
        <td><label><input type="checkbox" data-active ${q.active ? "checked" : ""} /> active</label></td>
        <td>
          <button type="button" class="btn btn-secondary" data-save>Save</button>
          <button type="button" class="btn btn-danger" data-retire>Retire</button>
          <span data-row-status></span>
        </td>
      </tr>
    `).join("");

    pane.innerHTML = `
      <div class="subtitle" style="margin-bottom:0.5rem;">Rotating icebreaker pool. Soft-retire keeps old answers intact.</div>
      <form data-add-form class="form" style="margin-bottom:1rem;">
        <div class="field">
          <label>New question</label>
          <input type="text" name="prompt" placeholder="e.g. What's the last song that made you cry?" required style="width:100%;" />
        </div>
        <div class="field">
          <label>Weight</label>
          <input type="number" name="weight" min="1" max="1000" value="1" style="width:5rem;" />
          <div class="field-hint">Higher weight = drawn more often.</div>
        </div>
        <div>
          <button type="submit" class="btn btn-primary">Add</button>
          <span data-add-status></span>
        </div>
      </form>
      <table class="table">
        <thead><tr><th>Prompt</th><th>Weight</th><th>Active</th><th>Actions</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="4"><em>No questions yet — add one above.</em></td></tr>`}</tbody>
      </table>
    `;
    wire();
  }

  function wire() {
    pane.querySelector("[data-add-form]").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.target;
      const fd = new FormData(form);
      const statusEl = pane.querySelector("[data-add-status]");
      try {
        await apiPost("/api/bios/questions", {
          prompt: String(fd.get("prompt") || "").trim(),
          weight: parseInt(fd.get("weight"), 10) || 1,
        });
        await refresh();
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    pane.querySelectorAll("tr[data-id]").forEach((row) => {
      const id = row.dataset.id;
      const status = row.querySelector("[data-row-status]");
      row.querySelector("[data-save]").addEventListener("click", async () => {
        try {
          await apiPut(`/api/bios/questions/${id}`, {
            prompt: row.querySelector("[data-prompt]").value.trim(),
            weight: parseInt(row.querySelector("[data-weight]").value, 10) || 1,
            active: row.querySelector("[data-active]").checked,
          });
          showStatus(status, true);
        } catch (err) {
          showStatus(status, false, err.message);
        }
      });
      row.querySelector("[data-retire]").addEventListener("click", async () => {
        if (!(await confirmDialog("Retire this question? Its existing answers stay intact in posted bios.", { danger: true, confirmLabel: "Retire" }))) return;
        try {
          await apiDelete(`/api/bios/questions/${id}`);
          await refresh();
        } catch (err) {
          showStatus(status, false, err.message);
        }
      });
    });
  }

  await refresh();
}
