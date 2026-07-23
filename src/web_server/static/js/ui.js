let _toastWrap = null;
function getToastWrap() {
  if (!_toastWrap) {
    _toastWrap = document.createElement("div");
    _toastWrap.className = "toast-wrap";
    // Announce toasts to assistive tech. Polite by default so it doesn't
    // interrupt; error toasts escalate to role="alert" (assertive) below.
    _toastWrap.setAttribute("role", "status");
    _toastWrap.setAttribute("aria-live", "polite");
    _toastWrap.setAttribute("aria-atomic", "false");
    document.body.appendChild(_toastWrap);
  }
  return _toastWrap;
}

export function toast(message, type) {
  const el = document.createElement("div");
  el.className = "toast" + (type === "error" ? " toast-error" : type === "info" ? " toast-info" : "");
  if (type === "error") el.setAttribute("role", "alert");
  const msg = document.createElement("span");
  msg.textContent = message;
  el.appendChild(msg);
  // Visually-quiet, keyboard-focusable dismiss control (clicking anywhere on
  // the toast still dismisses; this gives keyboard/AT users a target).
  const x = document.createElement("button");
  x.type = "button";
  x.className = "toast-x";
  x.setAttribute("aria-label", "Dismiss");
  x.textContent = "×";
  x.style.cssText = "background:none;border:0;color:inherit;font:inherit;cursor:pointer;margin-left:10px;padding:0 2px;opacity:.7;";
  el.appendChild(x);
  const t = setTimeout(() => dismiss(el), type === "error" ? 6000 : 3500);
  el.addEventListener("click", () => { clearTimeout(t); dismiss(el); });
  getToastWrap().appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
}

function dismiss(el) {
  el.classList.remove("show");
  el.addEventListener("transitionend", () => el.remove(), { once: true });
}

let _dialogSeq = 0;

// Shared modal plumbing: focus trap (Tab/Shift+Tab cycle within the box),
// Escape to dismiss, and focus restore to the trigger on close. Returns a
// restoreFocus() the caller invokes when the dialog closes.
function _mountModal(overlay, box, { initialFocus, onEscape }) {
  const prevFocus = document.activeElement;
  overlay.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      e.preventDefault();
      onEscape();
      return;
    }
    if (e.key !== "Tab") return;
    const items = Array.from(
      box.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter(el => !el.disabled && el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });
  document.body.appendChild(overlay);
  (initialFocus || box).focus();
  return () => {
    if (prevFocus && typeof prevFocus.focus === "function") prevFocus.focus();
  };
}

export function confirmDialog(message, opts = {}) {
  const { title, danger = false, confirmLabel = "Confirm" } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    const msgId = `dlg-msg-${++_dialogSeq}`;
    overlay.innerHTML = `
      <div class="confirm-box" role="dialog" aria-modal="true" aria-labelledby="${msgId}">
        ${title ? `<h3 id="${msgId}"></h3><p></p>` : `<p id="${msgId}"></p>`}
        <div class="confirm-actions">
          <button class="btn btn-ghost" data-cancel>Cancel</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-confirm></button>
        </div>
      </div>`;
    if (title) overlay.querySelector("h3").textContent = title;
    overlay.querySelector("p").textContent = message;
    overlay.querySelector("[data-confirm]").textContent = confirmLabel;
    const box = overlay.querySelector(".confirm-box");
    let restoreFocus = () => {};
    const finish = val => { restoreFocus(); overlay.remove(); resolve(val); };
    overlay.querySelector("[data-cancel]").addEventListener("click", () => finish(false));
    overlay.querySelector("[data-confirm]").addEventListener("click", () => finish(true));
    overlay.addEventListener("click", e => { if (e.target === overlay) finish(false); });
    restoreFocus = _mountModal(overlay, box, {
      initialFocus: overlay.querySelector("[data-confirm]"),
      onEscape: () => finish(false),
    });
  });
}

export function promptDialog(message, opts = {}) {
  const { title, confirmLabel = "OK", required = false, danger = false, value = "" } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    const labelId = `dlg-lbl-${++_dialogSeq}`;
    overlay.innerHTML = `
      <div class="confirm-box" role="dialog" aria-modal="true" aria-labelledby="${labelId}">
        ${title ? `<h3 id="${labelId}"></h3>` : `<p id="${labelId}"></p>`}
        ${title ? `<p></p>` : ""}
        <div class="field" style="margin-bottom:14px;">
          <input type="text" style="width:100%;box-sizing:border-box;">
        </div>
        <div class="confirm-actions">
          <button class="btn btn-ghost" data-cancel>Cancel</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-confirm></button>
        </div>
      </div>`;
    if (title) {
      overlay.querySelector("h3").textContent = title;
      overlay.querySelector(".confirm-box > p").textContent = message;
    } else {
      overlay.querySelector("p").textContent = message;
    }
    const input = overlay.querySelector("input");
    input.value = value;
    input.setAttribute("aria-label", title || message);
    overlay.querySelector("[data-confirm]").textContent = confirmLabel;
    const box = overlay.querySelector(".confirm-box");
    let restoreFocus = () => {};
    const finish = val => { restoreFocus(); overlay.remove(); resolve(val); };
    overlay.querySelector("[data-cancel]").addEventListener("click", () => finish(null));
    overlay.querySelector("[data-confirm]").addEventListener("click", () => {
      if (required && !input.value.trim()) { input.focus(); return; }
      finish(input.value);
    });
    overlay.addEventListener("click", e => { if (e.target === overlay) finish(null); });
    input.addEventListener("keydown", e => {
      // Escape is handled at the overlay level by _mountModal.
      if (e.key === "Enter") { if (required && !input.value.trim()) return; finish(input.value); }
    });
    restoreFocus = _mountModal(overlay, box, {
      initialFocus: input,
      onEscape: () => finish(null),
    });
  });
}
