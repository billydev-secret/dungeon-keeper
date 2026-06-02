import { api, apiPost, esc } from "../api.js";
import { apiDelete, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading questions…</div></div>`;

  let questions = [];

  function render() {
    const rows = questions.map((q) => `
      <tr data-id="${q.id}">
        <td>
          <input type="text" data-prompt value="${esc(q.prompt)}" style="width: 100%;" />
        </td>
        <td>
          <input type="number" data-weight min="1" max="1000" value="${q.weight}" style="width: 5rem;" />
        </td>
        <td>
          <label>
            <input type="checkbox" data-active ${q.active ? "checked" : ""} /> active
          </label>
        </td>
        <td>
          <button type="button" class="btn btn-secondary" data-save>Save</button>
          <button type="button" class="btn btn-danger" data-retire>Retire</button>
          <span data-row-status></span>
        </td>
      </tr>
    `).join("");

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Bios — Questions</h2>
          <div class="subtitle">Rotating icebreaker pool. Soft-retire keeps old answers intact.</div>
        </header>
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
      </div>
    `;
    wire();
  }

  function wire() {
    container.querySelector("[data-add-form]").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.target;
      const fd = new FormData(form);
      const statusEl = container.querySelector("[data-add-status]");
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

    container.querySelectorAll("tr[data-id]").forEach((row) => {
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
        if (!confirm("Retire this question? Its existing answers stay intact in posted bios.")) return;
        try {
          await apiDelete(`/api/bios/questions/${id}`);
          await refresh();
        } catch (err) {
          showStatus(status, false, err.message);
        }
      });
    });
  }

  async function refresh() {
    questions = await api("/api/bios/questions");
    render();
  }

  refresh();
}
