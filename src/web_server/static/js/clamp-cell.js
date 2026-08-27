/**
 * Make a truncated audit-table cell readable.
 *
 * `.reason-cell` clamps to three lines in CSS, which is enough for every
 * reason, note, filename and details value stored in prod. The handful that
 * still overflow — a long confession, a policy description — get a "More"
 * button here.
 *
 * A native <button> on purpose: it is focusable and answers Enter and Space
 * without a `role`, a `tabindex` or a keydown handler, so this clears the
 * keyboard bar the queue rows and sortable headers set rather than
 * re-implementing it.
 *
 * No imports. audit-helpers.js is deliberately dependency-free and six of the
 * eight panels reach this through it.
 */

/**
 * Wrap every `.reason-cell` under `root` and add an expander where the text
 * is actually cut. Safe to call again after a re-render.
 */
export function initClampCells(root) {
  if (!root) return;
  for (const cell of root.querySelectorAll(".reason-cell")) {
    if (cell.querySelector(".clamp")) continue;   // already wrapped

    const clamp = document.createElement("div");
    clamp.className = "clamp";
    while (cell.firstChild) clamp.append(cell.firstChild);
    cell.append(clamp);

    // scrollHeight is only meaningful once the node is in the document and
    // laid out, which is why this runs after the table is inserted rather
    // than while it is being built.
    if (clamp.scrollHeight <= clamp.clientHeight + 1) continue;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "clamp-more";
    btn.textContent = "More";
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", (e) => {
      // LOAD-BEARING: mod-policy-tickets binds a delegated tbody click that
      // opens the transcript modal for anything inside the row. Without this,
      // reading a cell launches a modal over the table you were reading.
      e.stopPropagation();
      const open = cell.classList.toggle("is-open");
      btn.textContent = open ? "Less" : "More";
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    cell.append(btn);
  }
}
