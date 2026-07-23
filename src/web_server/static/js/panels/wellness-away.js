import { wGet, wPost, esc, showStatus } from "../wellness-helpers.js";
import { guardForm } from "../config-helpers.js";
import { renderLoading, renderError } from "../states.js";

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading your away message…")}</div>`;

  (async () => {
    let d;
    try { d = await wGet("/api/wellness/away"); } catch (e) {
      container.querySelector(".panel").innerHTML =
        renderError(`Couldn’t load your away settings — try again. (${e.message})`);
      return;
    }

    if (!d.opted_in) {
      container.querySelector(".panel").innerHTML = `
        <header><h2>Away Message</h2></header>
        <div class="w-notice"><p>You haven’t joined the wellness program yet, so there’s nothing to set here.</p>
        <p>Run <code>/wellness setup</code> in Discord to opt in, then come back.</p></div>`;
      return;
    }

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Away Message</h2>
        <div class="subtitle">When enabled, the bot replies to mentions on your behalf</div>
      </header>

      <form data-away-form class="form">
        <div class="field">
          <label>
            <input type="checkbox" name="enabled" ${d.enabled ? "checked" : ""} />
            Reply on My Behalf
          </label>
          <div class="field-hint">While this is on, Dungeon Keeper answers anyone who
            mentions you with the message below, so nobody is left waiting.</div>
        </div>
        <div class="field">
          <label for="w-away-msg">Message <span data-charcount class="w-charcount">${(d.message || "").length}/${d.max_len}</span></label>
          <textarea name="message" id="w-away-msg" rows="4" maxlength="${d.max_len}" placeholder="e.g. Stepping back today…">${esc(d.message || "")}</textarea>
        </div>
        <div class="w-preview" data-preview>
          <div class="w-preview-label">Preview</div>
          <div data-preview-text>${esc(d.message || "No message set yet.")}</div>
        </div>
        <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
      </form>
    `;

    const form = guardForm(container.querySelector("[data-away-form]"));
    const status = container.querySelector("[data-status]");
    const textarea = form.querySelector("textarea");
    const charcount = container.querySelector("[data-charcount]");
    const previewText = container.querySelector("[data-preview-text]");

    // Live preview
    textarea.addEventListener("input", () => {
      charcount.textContent = `${textarea.value.length}/${d.max_len}`;
      previewText.textContent = textarea.value || "No message set yet.";
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await wPost("/api/wellness/away", {
          enabled: form.querySelector("[name=enabled]").checked,
          message: textarea.value,
        });
        showStatus(status, true);
      } catch (err) { showStatus(status, false, `Couldn’t save — ${err.message}`); }
    });
  })();
}
