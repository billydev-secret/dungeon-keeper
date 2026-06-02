import { api, apiPost, esc } from "../api.js";
import {
  apiPut,
  buildField,
  categorySelect,
  channelSelect,
  loadCategories,
  loadChannels,
  showStatus,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading bios config…</div></div>`;

  (async () => {
    const [config, channels, categories] = await Promise.all([
      api("/api/bios/config"),
      loadChannels(),
      loadCategories(),
    ]);

    const colorVal = (config.embed_color || "#C8763E").replace(/^#/, "");
    const colorHex = `#${colorVal}`;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Bios — Config</h2>
          <div class="subtitle">Where bios are posted, the wizard category, and timing.</div>
        </header>
        <form class="form" data-form></form>
        <div style="margin-top:1rem; padding-top:1rem; border-top:1px solid var(--border, #333);">
          <h3 style="margin-top:0">Trigger button</h3>
          <p>Posts the persistent <strong>📝 Create / Update Bio</strong> button into the configured bios channel. Members tap it to start the wizard.</p>
          <button type="button" class="btn btn-secondary" data-post-btn>Post trigger button</button>
          <span data-post-status></span>
        </div>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    form.appendChild(
      buildField(
        "Bios channel",
        Object.assign(document.createElement("select"), {
          name: "bios_channel_id",
          innerHTML: channelSelect(channels, config.bios_channel_id),
        }),
        "Where finished bio embeds are posted.",
      ),
    );
    form.appendChild(
      buildField(
        "Wizard category",
        Object.assign(document.createElement("select"), {
          name: "wizard_category_id",
          innerHTML: categorySelect(categories, config.wizard_category_id),
        }),
        "Throwaway wizard channels are created under this category.",
      ),
    );
    form.appendChild(
      buildField(
        "Questions per bio",
        Object.assign(document.createElement("input"), {
          type: "number",
          name: "questions_per_bio",
          min: "1",
          max: "10",
          value: String(config.questions_per_bio || 3),
        }),
        "How many icebreaker questions are drawn from the pool.",
      ),
    );
    form.appendChild(
      buildField(
        "Embed color",
        Object.assign(document.createElement("input"), {
          type: "color",
          name: "embed_color",
          value: colorHex,
        }),
        "Single ember accent shared across all bio embeds.",
      ),
    );
    form.appendChild(
      buildField(
        "Wizard timeout (minutes)",
        Object.assign(document.createElement("input"), {
          type: "number",
          name: "wizard_timeout",
          min: "1",
          max: "120",
          value: String(config.wizard_timeout || 15),
        }),
        "Idle minutes before a session auto-cancels.",
      ),
    );
    form.appendChild(
      buildField(
        "Archive grace (seconds)",
        Object.assign(document.createElement("input"), {
          type: "number",
          name: "archive_grace",
          min: "0",
          max: "3600",
          value: String(config.archive_grace || 60),
        }),
        "Wait this long after completion before deleting the wizard channel.",
      ),
    );
    const submit = document.createElement("div");
    submit.innerHTML = `<button type="submit" class="btn btn-primary">Save</button><span data-status></span>`;
    form.appendChild(submit);

    const statusEl = container.querySelector("[data-status]");
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
        });
        showStatus(statusEl, true);
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    const postBtn = container.querySelector("[data-post-btn]");
    const postStatus = container.querySelector("[data-post-status]");
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
  })();
}
