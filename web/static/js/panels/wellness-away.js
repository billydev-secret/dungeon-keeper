import { wGet, wPost, esc, showStatus } from "../wellness-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading away settings...</div></div>`;

  (async () => {
    let d;
    try { d = await wGet("/api/wellness/away"); } catch (e) {
      container.querySelector(".panel").innerHTML = `<div class="error">${e.message}</div>`;
      return;
    }

    if (!d.opted_in) {
      container.querySelector(".panel").innerHTML = `
        <header><h2>Away Message</h2></header>
        <div class="w-notice"><p>You must opt in via <code>/wellness setup</code> first.</p></div>`;
      return;
    }

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Away Message</h2>
        <div class="subtitle">When enabled, the bot replies to mentions on your behalf</div>
      </header>

      <form data-away-form class="w-form">
        <div class="field">
          <label>
            <input type="checkbox" name="enabled" ${d.enabled ? "checked" : ""} />
            Enable away message
          </label>
        </div>
        <div class="field">
          <label>Message <span data-charcount class="w-charcount">${(d.message || "").length}/${d.max_len}</span></label>
          <textarea name="message" rows="4" maxlength="${d.max_len}" placeholder="e.g. Stepping back today...">${esc(d.message || "")}</textarea>
        </div>
        <div class="w-preview" data-preview>
          <div class="w-preview-label">Preview</div>
          <div data-preview-text>${esc(d.message || "(no message set)")}</div>
        </div>
        <div><button type="submit">Save</button><span data-status></span></div>
      </form>
    `;

    const form = container.querySelector("[data-away-form]");
    const status = container.querySelector("[data-status]");
    const textarea = form.querySelector("textarea");
    const charcount = container.querySelector("[data-charcount]");
    const previewText = container.querySelector("[data-preview-text]");

    // Live preview
    textarea.addEventListener("input", () => {
      charcount.textContent = `${textarea.value.length}/${d.max_len}`;
      previewText.textContent = textarea.value || "(no message set)";
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await wPost("/api/wellness/away", {
          enabled: form.querySelector("[name=enabled]").checked,
          message: textarea.value,
        });
        showStatus(status, true);
      } catch (err) { showStatus(status, false, err.message); }
    });
  })();
}
