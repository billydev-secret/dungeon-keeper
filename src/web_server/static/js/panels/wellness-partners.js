import { wGet, wPost, wDelete, esc } from "../wellness-helpers.js";
import { toast, confirmDialog } from "../ui.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading your partners…")}</div>`;

  async function load() {
    let d;
    try { d = await wGet("/api/wellness/partners"); } catch (e) {
      container.querySelector(".panel").innerHTML =
        renderError(`Couldn’t load your partners — try again. (${e.message})`);
      return;
    }

    const accepted = d.partnerships.filter(p => p.status === "accepted");
    const pending = d.partnerships.filter(p => p.status === "pending");

    const acceptedHTML = accepted.length
      ? accepted.map(p => `
          <div class="w-row">
            <div class="w-row-main"><strong>${esc(p.other_name)}</strong></div>
            <div class="w-row-actions">
              <button class="btn btn-sm btn-danger" data-dissolve="${p.id}">Dissolve</button>
            </div>
          </div>
        `).join("")
      : renderEmpty("No partners yet. Send a request below and, once they accept, you’ll see each other’s streaks.");

    const pendingHTML = pending.length
      ? `<div class="section-label">Pending</div>` + pending.map(p => `
          <div class="w-row w-row-muted">
            <div class="w-row-main">
              <strong>${esc(p.other_name)}</strong>
              <span class="chip chip-neutral">${p.is_requester ? "You requested" : "Awaiting your response in DMs"}</span>
            </div>
            <div class="w-row-actions">
              <button class="btn btn-sm btn-danger" data-dissolve="${p.id}">Cancel</button>
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

      <div class="section-label">Request Partner</div>
      <form data-req-form class="form w-inline-form">
        <div class="field">
          <label for="w-partner-id">Discord User ID
            <input type="text" name="user_id" id="w-partner-id" required
                   placeholder="e.g. 123456789012345678" />
          </label>
          <div class="field-hint">Turn on Developer Mode in Discord, then right-click your
            partner and choose Copy User ID. They get a DM asking them to accept.</div>
        </div>
        <button type="submit" class="btn btn-primary">Send Request</button>
        <span data-req-status></span>
      </form>
    `;

    // Dissolve
    container.querySelectorAll("[data-dissolve]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const ok = await confirmDialog(
          "End this partnership? You’ll both stop seeing each other’s streaks and nudges.",
          { title: "End Partnership", danger: true, confirmLabel: "End Partnership" },
        );
        if (!ok) return;
        try { await wDelete(`/api/wellness/partners/${btn.dataset.dissolve}`); load(); }
        catch (e) { toast(`Couldn’t end the partnership — ${e.message}`, "error"); }
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
        toast("Partner request sent — they’ll get a DM.");
        load();
      } catch (err) {
        st.className = "save-status save-err";
        st.textContent = `Couldn’t send the request — ${err.message}`;
      }
    });
  }

  load();
}
