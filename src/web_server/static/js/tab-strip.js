// Shared wiring for .ctrl-group button strips (filter strips and tab strips).
//
// Markup contract (unchanged from the adopting panels — keep their existing
// role="group"/aria-label attributes):
//   <div class="ctrl-group" role="group" aria-label="Filter jails" data-filter-group>
//     <button class="active" data-filter="active">Active</button>
//     <button data-filter="all">All</button>
//   </div>
//
// Wires one delegated click listener on groupEl, keeps exactly one button
// .active, and calls onChange(value) with the clicked button's attribute
// value ("" is a valid value — several strips use it for "All"). Clicking
// the already-active button fires onChange again; panels rely on that as a
// cheap refresh. Returns { setActive } for programmatic sync.
export function makeFilterStrip(groupEl, onChange, { attr = "data-filter" } = {}) {
  function setActive(value) {
    groupEl.querySelectorAll(`button[${attr}]`).forEach((b) => {
      const on = b.getAttribute(attr) === value;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  // Expose the initial selection to assistive tech, not just visually.
  groupEl.querySelectorAll(`button[${attr}]`).forEach((b) => {
    b.setAttribute("aria-pressed", b.classList.contains("active") ? "true" : "false");
  });
  groupEl.addEventListener("click", (e) => {
    const btn = e.target.closest(`button[${attr}]`);
    if (!btn || !groupEl.contains(btn)) return;
    setActive(btn.getAttribute(attr));
    onChange(btn.getAttribute(attr));
  });
  return { setActive };
}
