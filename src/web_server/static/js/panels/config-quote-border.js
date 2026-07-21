import { api, apiPost, apiDelete } from "../api.js";
import { showStatus } from "../config-helpers.js";

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

        <section class="form">
          <h3 style="margin:0 0 1rem">Quote Card Border</h3>
          <div class="field-hint" style="margin-bottom:1rem">
            Upload a frame that's composited over every quote card. It becomes this
            server's default border (members can still pick a bundled frame per quote).
            The quote text and avatar are fit <strong>inside the frame's opening</strong> —
            any shape works (rectangle, oval, arch), and the text auto-shrinks to fit.
            Requirements: a <strong>PNG or WEBP with a see-through center</strong> — a frame
            that leaves no clear opening is rejected. If the opening is too small for an
            avatar, the card falls back to a centered layout (avatar as background, name as
            a header). Cards render at about <strong>900×500</strong> (1.8:1); match that
            aspect for best results.
          </div>

          <div data-preview-wrap style="margin-bottom:1rem;display:none">
            <div class="field-hint" style="margin-bottom:.5rem">Current border</div>
            <img data-preview alt="Current quote border"
              style="max-width:100%;width:450px;border-radius:8px;background:
              repeating-conic-gradient(#808080 0% 25%, #a0a0a0 0% 50%) 50% / 20px 20px" />
            <div data-dims class="field-hint" style="margin-top:.35rem"></div>
          </div>
          <div data-empty class="field-hint" style="margin-bottom:1rem;display:none">
            No custom border set — the bundled <em>Golden Poppy</em> frame is used.
          </div>

          <div class="field">
            <label>Upload border</label>
            <input type="file" data-border-file accept="image/png,image/webp" />
          </div>
          <div style="display:flex;align-items:center;gap:12px">
            <button type="button" class="btn btn-primary" data-upload>Upload</button>
            <button type="button" class="btn btn-danger" data-remove
              style="display:none">Remove</button>
            <span data-status></span>
          </div>
        </section>

        <section class="form" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--border,#333)">
          <h3 style="margin:0 0 .5rem">Don't have a border? Generate one</h3>
          <div class="field-hint" style="margin-bottom:1rem">
            Paste this into an image AI (ChatGPT/DALL·E, Midjourney, Stable Diffusion…),
            swap the <em>"golden art-nouveau florals"</em> part for any style you like, then
            upload the result above. Export it as a <strong>PNG with a transparent
            background</strong> and keep the <strong>center clear</strong> — that's where the
            quote goes.
          </div>
          <div class="field">
            <label>Prompt</label>
            <textarea data-gen-prompt readonly rows="5"
              style="width:100%;box-sizing:border-box;resize:vertical;font-family:inherit;line-height:1.45"></textarea>
          </div>
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:1rem">
            <button type="button" class="btn btn-secondary" data-copy-prompt>Copy Prompt</button>
          </div>
          <div class="field">
            <label>Negative prompt <span style="font-weight:400;opacity:.6">(optional — for Stable Diffusion &amp; similar)</span></label>
            <textarea data-gen-negative readonly rows="2"
              style="width:100%;box-sizing:border-box;resize:vertical;font-family:inherit;line-height:1.45"></textarea>
          </div>
          <div style="display:flex;align-items:center;gap:12px">
            <button type="button" class="btn btn-secondary" data-copy-negative>Copy Negative</button>
          </div>
        </section>
      </div>
    `;

    const previewWrap = container.querySelector("[data-preview-wrap]");
    const preview = container.querySelector("[data-preview]");
    const dims = container.querySelector("[data-dims]");
    const emptyMsg = container.querySelector("[data-empty]");
    const fileInput = container.querySelector("[data-border-file]");
    const uploadBtn = container.querySelector("[data-upload]");
    const removeBtn = container.querySelector("[data-remove]");
    const status = container.querySelector("[data-status]");

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

    uploadBtn.addEventListener("click", async () => {
      if (!fileInput.files.length) {
        showStatus(status, false, "Pick a file");
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
