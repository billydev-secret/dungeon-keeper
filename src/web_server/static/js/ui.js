let _toastWrap = null;
function getToastWrap() {
  if (!_toastWrap) {
    _toastWrap = document.createElement("div");
    _toastWrap.className = "toast-wrap";
    document.body.appendChild(_toastWrap);
  }
  return _toastWrap;
}

export function toast(message, type) {
  const el = document.createElement("div");
  el.className = "toast" + (type === "error" ? " toast-error" : type === "info" ? " toast-info" : "");
  el.textContent = message;
  el.addEventListener("click", () => dismiss(el));
  getToastWrap().appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  const t = setTimeout(() => dismiss(el), type === "error" ? 6000 : 3500);
  el.addEventListener("click", () => clearTimeout(t), { once: true });
}

function dismiss(el) {
  el.classList.remove("show");
  el.addEventListener("transitionend", () => el.remove(), { once: true });
}

export function confirmDialog(message, opts = {}) {
  const { danger = false, confirmLabel = "Confirm" } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    overlay.innerHTML = `
      <div class="confirm-box">
        <p></p>
        <div class="confirm-actions">
          <button class="btn btn-ghost" data-cancel>Cancel</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-confirm></button>
        </div>
      </div>`;
    overlay.querySelector("p").textContent = message;
    overlay.querySelector("[data-confirm]").textContent = confirmLabel;
    const finish = val => { overlay.remove(); resolve(val); };
    overlay.querySelector("[data-cancel]").addEventListener("click", () => finish(false));
    overlay.querySelector("[data-confirm]").addEventListener("click", () => finish(true));
    overlay.addEventListener("click", e => { if (e.target === overlay) finish(false); });
    document.body.appendChild(overlay);
    overlay.querySelector("[data-confirm]").focus();
  });
}

export function promptDialog(message, opts = {}) {
  const { title, confirmLabel = "OK", required = false, danger = false, value = "" } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    overlay.innerHTML = `
      <div class="confirm-box">
        ${title ? `<h3></h3>` : ""}
        <p></p>
        <div class="field" style="margin-bottom:14px;">
          <input type="text" style="width:100%;box-sizing:border-box;">
        </div>
        <div class="confirm-actions">
          <button class="btn btn-ghost" data-cancel>Cancel</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-confirm></button>
        </div>
      </div>`;
    if (title) overlay.querySelector("h3").textContent = title;
    overlay.querySelector("p").textContent = message;
    const input = overlay.querySelector("input");
    input.value = value;
    overlay.querySelector("[data-confirm]").textContent = confirmLabel;
    const finish = val => { overlay.remove(); resolve(val); };
    overlay.querySelector("[data-cancel]").addEventListener("click", () => finish(null));
    overlay.querySelector("[data-confirm]").addEventListener("click", () => {
      if (required && !input.value.trim()) { input.focus(); return; }
      finish(input.value);
    });
    overlay.addEventListener("click", e => { if (e.target === overlay) finish(null); });
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") { if (required && !input.value.trim()) return; finish(input.value); }
      if (e.key === "Escape") finish(null);
    });
    document.body.appendChild(overlay);
    input.focus();
  });
}
