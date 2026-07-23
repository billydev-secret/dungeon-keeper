/**
 * Dual-handle time-window slider for charts.
 *
 * Usage:
 *   import { mountTimeSlider } from "../slider.js";
 *
 *   const slider = mountTimeSlider(container, {
 *     totalPoints: data.labels.length,
 *     labels: data.labels,          // shown at slider edges
 *     onChange(start, end) {         // 0-based indices, inclusive
 *       // re-slice chart data and update
 *     },
 *   });
 *   // slider.setRange(0, n-1)  — programmatically reset
 *   // slider.destroy()         — remove DOM
 */

export function mountTimeSlider(parent, { totalPoints, labels, onChange }) {
  if (totalPoints < 3) return { setRange() {}, destroy() {} };

  const el = document.createElement("div");
  el.className = "time-slider";
  el.innerHTML = `
    <div class="time-slider-track">
      <div class="time-slider-fill"></div>
      <input type="range" class="time-slider-lo" min="0" max="${totalPoints - 1}" value="0" aria-label="Range start" />
      <input type="range" class="time-slider-hi" min="0" max="${totalPoints - 1}" value="${totalPoints - 1}" aria-label="Range end" />
    </div>
    <div class="time-slider-labels">
      <span class="time-slider-lo-label"></span>
      <span class="time-slider-hi-label"></span>
    </div>
  `;
  parent.appendChild(el);

  const loInput = el.querySelector(".time-slider-lo");
  const hiInput = el.querySelector(".time-slider-hi");
  const fill    = el.querySelector(".time-slider-fill");
  const loLabel = el.querySelector(".time-slider-lo-label");
  const hiLabel = el.querySelector(".time-slider-hi-label");

  function updateFill() {
    const lo = parseInt(loInput.value);
    const hi = parseInt(hiInput.value);
    const pctLo = (lo / (totalPoints - 1)) * 100;
    const pctHi = (hi / (totalPoints - 1)) * 100;
    fill.style.left  = `${pctLo}%`;
    fill.style.width = `${pctHi - pctLo}%`;
    loLabel.textContent = labels[lo] || lo;
    hiLabel.textContent = labels[hi] || hi;
    // Announce the human-readable window edge, not the raw index.
    loInput.setAttribute("aria-valuetext", String(labels[lo] || lo));
    hiInput.setAttribute("aria-valuetext", String(labels[hi] || hi));
  }

  function clampAndEmit() {
    let lo = parseInt(loInput.value);
    let hi = parseInt(hiInput.value);
    // Ensure lo <= hi with at least 1 point gap
    if (lo >= hi) {
      if (this === loInput) { lo = Math.max(0, hi - 1); loInput.value = lo; }
      else { hi = Math.min(totalPoints - 1, lo + 1); hiInput.value = hi; }
    }
    updateFill();
    onChange(lo, hi);
  }

  loInput.addEventListener("input", clampAndEmit);
  hiInput.addEventListener("input", clampAndEmit);

  // The range inputs are pointer-events:none except their thumbs (so the two
  // overlaid sliders don't fight), which left only the 14px thumbs grabbable.
  // Make the rest of the track clickable: jump the nearest thumb to the
  // clicked position (W-A6).
  const track = el.querySelector(".time-slider-track");
  track.addEventListener("pointerdown", (e) => {
    if (e.target === loInput || e.target === hiInput) return; // thumb drag
    const r = track.getBoundingClientRect();
    if (!r.width) return;
    const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    const idx = Math.round(frac * (totalPoints - 1));
    const lo = parseInt(loInput.value);
    const hi = parseInt(hiInput.value);
    const target = Math.abs(idx - lo) <= Math.abs(idx - hi) ? loInput : hiInput;
    target.value = idx;
    clampAndEmit.call(target);
    target.focus();
  });

  updateFill();

  return {
    setRange(lo, hi) {
      loInput.value = lo;
      hiInput.value = hi;
      updateFill();
    },
    destroy() { el.remove(); },
  };
}
