import { wGet, wPost, wDelete, esc } from "../wellness-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading partners...</div></div>`;

  async function load() {
    let d;
    try { d = await wGet("/api/wellness/partners"); } catch (e) {
      container.querySelector(".panel").innerHTML = `<div class="error">${e.message}</div>`;
      return;
    }

    const accepted = d.partnerships.filter(p => p.status === "accepted");
    const pending = d.partnerships.filter(p => p.status === "pending");

    const acceptedHTML = accepted.length
      ? accepted.map(p => `
          <div class="w-row">
            <div class="w-row-main"><strong>${esc(p.other_name)}</strong></div>
            <div class="w-row-actions">
              <button class="btn-danger" data-dissolve="${p.id}">Dissolve</button>
            </div>
          </div>
        `).join("")
      : '<div class="w-empty">No active partnerships.</div>';

    const pendingHTML = pending.length
      ? `<h3>Pending</h3>` + pending.map(p => `
          <div class="w-row w-row-muted">
            <div class="w-row-main">
              <strong>${esc(p.other_name)}</strong>
              <span class="w-chip w-chip-dim">${p.is_requester ? "You requested" : "Awaiting your response in DMs"}</span>
            </div>
            <div class="w-row-actions">
              <button class="btn-danger" data-dissolve="${p.id}">Cancel</button>
            </div>
          </div>
        `).join("")
      : "";

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Accountability Partners</h2>
        <div class="subtitle">Partners see each other's streaks and can send supportive nudges</div>
      </header>
      <div class="w-list">${acceptedHTML}</div>
      ${pendingHTML}

      <section class="w-section">
        <h3>Request Partner</h3>
        <form data-req-form class="w-form w-inline-form">
          <div class="field">
            <label>Discord User ID</label>
            <input type="text" name="user_id" required placeholder="e.g. 123456789012345678" />
          </div>
          <button type="submit">Send Request</button>
          <span data-req-status></span>
        </form>
      </section>
    `;

    // Dissolve
    container.querySelectorAll("[data-dissolve]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!confirm("Dissolve this partnership?")) return;
        try { await wDelete(`/api/wellness/partners/${btn.dataset.dissolve}`); load(); }
        catch (e) { alert(e.message); }
      });
    });

    // Request
    const form = container.querySelector("[data-req-form]");
    const st = container.querySelector("[data-req-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      st.textContent = "";
      try {
        await wPost("/api/wellness/partners/request", { user_id: new FormData(form).get("user_id") });
        load();
      } catch (err) {
        st.className = "save-status save-err";
        st.textContent = err.message;
      }
    });
  }

  load();
}
