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
      <input type="range" class="time-slider-lo" min="0" max="${totalPoints - 1}" value="0" />
      <input type="range" class="time-slider-hi" min="0" max="${totalPoints - 1}" value="${totalPoints - 1}" />
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
