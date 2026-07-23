import { api, apiPost, apiDelete } from "../api.js";
import { showStatus, guardForm } from "../config-helpers.js";
import { confirmDialog } from "../ui.js";

// Cache-buster so a freshly uploaded/removed border isn't served stale by the
// browser when we repoint the same <img>.
function imageUrl() {
  return `/api/config/quote-border/image?t=${Date.now()}`;
}

// Copy-paste starter prompt for generating a compatible border with any image
// AI (ChatGPT/DALL·E, Midjourney, Stable Diffusion…). The must-haves — edges-only
// decoration, a clear transparent center, wide 1.8:1 — are what the renderer needs
// to fit the quote inside the opening. The style phrase is meant to be swapped.
const GEN_PROMPT =
  "Ornate decorative border frame for a quote card, elegant golden art-nouveau " +
  "florals and filigree in the four corners and along the edges only, with a large " +
  "clear empty space in the center, on a fully transparent background (alpha), wide " +
  "landscape 1.8:1 rectangle about 900x500 pixels, symmetrical, high detail, clean " +
  "cutout, no text, no letters, no watermark, no solid background fill.";

// Negative prompt for tools that take one (Stable Diffusion et al.).
const GEN_NEGATIVE =
  "solid background, opaque center, filled middle, text, letters, words, " +
  "watermark, signature, photo, decoration covering the center.";

async function copyToClipboard(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    // Fallback for non-secure contexts.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (_) { /* give up */ }
    ta.remove();
  }
  const prev = btn.textContent;
  btn.textContent = "Copied!";
  setTimeout(() => { btn.textContent = prev; }, 1500);
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading quote tool…</div></div>`;

  (async () => {
    let meta = { exists: false };
    try {
      meta = await api("/api/config/quote-border");
    } catch (_) { /* fall through to the empty state */ }

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Quote Tool</h2>
          <div class="subtitle">The border framing this server's quote cards</div>
        </header>

        <form class="form card" data-border-form>
          <div class="section-label">Quote Card Border</div>
          <div class="field-hint" style="margin-bottom:1rem">
            Upload a frame that is drawn over every quote card. It becomes this server's
            default border — members can still choose one of the bundled frames for an
            individual quote. The quote text and avatar are fitted
            <strong>inside the frame's opening</strong>; any shape works (rectangle, oval,
            arch) and the text shrinks itself to fit.
            The file must be a <strong>PNG or WEBP with a see-through center</strong> —
            a frame with no clear opening is rejected. If the opening is too small for an
            avatar, the card falls back to a centered layout with the avatar as the
            background and the name as a header. Cards are rendered at about
            <strong>900 × 500 pixels</strong> (1.8:1), so match that shape for the best result.
          </div>

          <div data-preview-wrap style="margin-bottom:1rem;display:none">
            <div class="field-hint" style="margin-bottom:.5rem">Border in use right now</div>
            <img data-preview alt="The quote card border currently in use"
              style="max-width:100%;width:450px;border-radius:8px;background:
              repeating-conic-gradient(#808080 0% 25%, #a0a0a0 0% 50%) 50% / 20px 20px" />
            <div data-dims class="field-hint" style="margin-top:.35rem"></div>
          </div>
          <div data-empty class="field-hint" style="margin-bottom:1rem;display:none">
            No custom border yet — quote cards use the bundled <em>Golden Poppy</em> frame.
          </div>

          <div class="field">
            <label for="qb-file">Border Image File</label>
            <input type="file" id="qb-file" data-border-file accept="image/png,image/webp" />
            <div class="field-hint">Pick a PNG or WEBP from your device. Saving replaces the border on every new quote card immediately.</div>
          </div>
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
            <button type="submit" class="btn btn-primary">Save Border</button>
            <button type="button" class="btn btn-danger" data-remove
              style="display:none">Remove Border</button>
            <span data-status></span>
          </div>
        </form>

        <section class="form card" style="margin-top:12px;">
          <div class="section-label">No Border Yet? Generate One</div>
          <div class="field-hint" style="margin-bottom:1rem">
            Paste the prompt below into any image generator (ChatGPT, Midjourney, Stable
            Diffusion, and so on), swap <em>"golden art-nouveau florals"</em> for whatever
            style you want, then upload the result above. Export it as a
            <strong>PNG with a transparent background</strong> and keep the
            <strong>center clear</strong> — that space is where the quote goes.
          </div>
          <div class="field">
            <label for="qb-prompt">Prompt</label>
            <textarea id="qb-prompt" data-gen-prompt readonly rows="5"
              style="width:100%;box-sizing:border-box;resize:vertical;font-family:inherit;line-height:1.45"></textarea>
          </div>
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:1rem">
            <button type="button" class="btn btn-secondary" data-copy-prompt>Copy Prompt</button>
          </div>
          <div class="field">
            <label for="qb-negative">Things to Avoid <span style="font-weight:400;opacity:.6">(optional — for Stable Diffusion and similar tools)</span></label>
            <textarea id="qb-negative" data-gen-negative readonly rows="2"
              style="width:100%;box-sizing:border-box;resize:vertical;font-family:inherit;line-height:1.45"></textarea>
          </div>
          <div style="display:flex;align-items:center;gap:12px">
            <button type="button" class="btn btn-secondary" data-copy-negative>Copy List</button>
          </div>
        </section>
      </div>
    `;

    const previewWrap = container.querySelector("[data-preview-wrap]");
    const preview = container.querySelector("[data-preview]");
    const dims = container.querySelector("[data-dims]");
    const emptyMsg = container.querySelector("[data-empty]");
    const fileInput = container.querySelector("[data-border-file]");
    const borderForm = container.querySelector("[data-border-form]");
    const uploadBtn = borderForm.querySelector('button[type="submit"]');
    const removeBtn = container.querySelector("[data-remove]");
    const status = container.querySelector("[data-status]");
    guardForm(borderForm);

    // Generation-prompt helper. Set via .value (not innerHTML) so the text can't
    // break out of the textarea.
    const promptBox = container.querySelector("[data-gen-prompt]");
    const negativeBox = container.querySelector("[data-gen-negative]");
    promptBox.value = GEN_PROMPT;
    negativeBox.value = GEN_NEGATIVE;
    container.querySelector("[data-copy-prompt]").addEventListener("click", (e) => {
      copyToClipboard(GEN_PROMPT, e.currentTarget);
    });
    container.querySelector("[data-copy-negative]").addEventListener("click", (e) => {
      copyToClipboard(GEN_NEGATIVE, e.currentTarget);
    });

    function render(m) {
      if (m && m.exists) {
        preview.src = imageUrl();
        previewWrap.style.display = "block";
        emptyMsg.style.display = "none";
        removeBtn.style.display = "inline-block";
        dims.textContent =
          m.width && m.height ? `${m.width}×${m.height}px` : "";
      } else {
        previewWrap.style.display = "none";
        emptyMsg.style.display = "block";
        removeBtn.style.display = "none";
      }
    }
    render(meta);

    borderForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!fileInput.files.length) {
        showStatus(status, false, "Choose a border image file first.");
        fileInput.focus();
        return;
      }
      const fd = new FormData();
      fd.append("file", fileInput.files[0]);
      uploadBtn.disabled = true;
      showStatus(status, true, "Uploading…");
      try {
        const data = await apiPost("/api/config/quote-border", fd);
        fileInput.value = "";
        render(data);
        showStatus(status, true, "Saved");
      } catch (err) {
        showStatus(status, false, err.message);
      } finally {
        uploadBtn.disabled = false;
      }
    });

    removeBtn.addEventListener("click", async () => {
      const ok = await confirmDialog(
        "Delete this server's custom border? Quote cards go back to the bundled Golden Poppy frame, and you'll need the original file to restore it.",
        { title: "Remove Border", danger: true, confirmLabel: "Remove Border" },
      );
      if (!ok) return;
      removeBtn.disabled = true;
      showStatus(status, true, "Removing…");
      try {
        const data = await apiDelete("/api/config/quote-border");
        render(data);
        showStatus(status, true, "Removed");
      } catch (err) {
        showStatus(status, false, err.message);
      } finally {
        removeBtn.disabled = false;
      }
    });
  })();
}
