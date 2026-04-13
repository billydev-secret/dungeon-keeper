import { wGet, wPost, wPut, wDelete, esc, showStatus } from "../wellness-helpers.js";

const HOUR_LABELS = [
  "12a","1a","2a","3a","4a","5a","6a","7a",
  "8a","9a","10a","11a","12p","1p","2p","3p",
  "4p","5p","6p","7p","8p","9p","10p","11p",
];
const DAY_LABELS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading caps…</div></div>`;

  let chart = null;
  let currentMode = "daily";
  let currentDays = 30;
  let bucketAvgs = [];
  let sliderValues = [];
  let existingHistoCap = null; // cap row with bucket_limits for current mode

  async function load() {
    let caps, histo;
    try {
      [caps, histo] = await Promise.all([
        wGet("/api/wellness/caps"),
        wGet(`/api/wellness/xp-histogram?mode=${currentMode}&days=${currentDays}`),
      ]);
    } catch (e) {
      container.querySelector(".panel").innerHTML = `<div class="error">${e.message}</div>`;
      return;
    }

    bucketAvgs = histo.buckets.map(b => b.avg_xp);
    const labels = histo.buckets.map(b => b.label);
    const nBuckets = bucketAvgs.length;

    // Find existing histogram cap for this mode's window
    const histoWindow = currentMode === "daily" ? "daily" : "weekly";
    existingHistoCap = caps.caps.find(c => c.bucket_limits && c.window === histoWindow) || null;

    // Initialize sliders from existing cap or from averages (rounded up, min 1)
    if (existingHistoCap) {
      sliderValues = [...existingHistoCap.bucket_limits];
    } else {
      sliderValues = bucketAvgs.map(v => Math.max(1, Math.ceil(v)));
    }

    // Separate flat caps (no bucket_limits) for the legacy section
    const flatCaps = caps.caps.filter(c => !c.bucket_limits);

    const flatCapsHTML = flatCaps.length
      ? flatCaps.map(c => `
          <div class="w-row" data-cap-id="${c.id}">
            <div class="w-row-main">
              <strong>${esc(c.label)}</strong>
              <span class="w-chip">${c.scope}</span>
              <span class="w-chip">${c.window}</span>
              <span class="w-chip">${c.limit} msgs</span>
              ${c.exclude_exempt ? '<span class="w-chip w-chip-dim">excl. exempt</span>' : ""}
            </div>
            <div class="w-row-actions">
              <input type="number" min="1" value="${c.limit}" style="width:70px" data-edit-limit />
              <button data-save-cap="${c.id}">Save</button>
              <button class="btn-danger" data-del-cap="${c.id}">Remove</button>
              <span data-cap-status="${c.id}"></span>
            </div>
          </div>
        `).join("")
      : '<div class="w-empty">No manual caps.</div>';

    // Compute a good max for sliders
    const maxAvg = Math.max(...bucketAvgs, 1);
    const sliderMax = Math.max(Math.ceil(maxAvg * 3), 10);

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Activity Caps</h2>
        <div class="subtitle">Set message limits based on your real activity patterns</div>
      </header>

      <div class="w-histo-help">
        <p><strong>How it works:</strong> The gold bars show your average XP earned during each
        time period over the last <strong>${histo.days_covered} days</strong>.
        Use the sliders below to set a cap for each period &mdash; when your activity
        exceeds the cap, the wellness system will gently nudge you based on your
        enforcement level.</p>
        <p class="field-hint">${currentMode === "daily"
          ? "Each bar represents one hour of the day. Great for setting different limits for morning vs. evening — lower caps for late-night hours, higher caps during your normal active hours."
          : "Each bar represents one day of the week. Useful for allowing more activity on weekends vs. weekdays, or the reverse."
        }</p>
      </div>

      <div class="w-histo-controls">
        <div class="field">
          <label>Pattern</label>
          <select data-control="mode">
            <option value="daily" ${currentMode === "daily" ? "selected" : ""}>Daily (by hour)</option>
            <option value="weekly" ${currentMode === "weekly" ? "selected" : ""}>Weekly (by day)</option>
          </select>
        </div>
        <div class="field">
          <label>Lookback</label>
          <select data-control="days">
            <option value="14" ${currentDays === 14 ? "selected" : ""}>2 weeks</option>
            <option value="30" ${currentDays === 30 ? "selected" : ""}>30 days</option>
            <option value="90" ${currentDays === 90 ? "selected" : ""}>90 days</option>
          </select>
        </div>
      </div>

      <div class="w-histo-chart-wrap">
        <canvas data-histo-canvas></canvas>
      </div>

      <div class="w-histo-sliders" data-slider-row style="--bucket-count:${nBuckets}">
        ${sliderValues.map((v, i) => `
          <div class="w-histo-slider-cell">
            <input type="range" min="1" max="${sliderMax}" value="${v}"
                   orient="vertical" data-slider-idx="${i}" />
            <span class="w-histo-slider-val" data-val-idx="${i}">${v}</span>
            <span class="w-histo-slider-label">${labels[i]}</span>
          </div>
        `).join("")}
      </div>

      <div class="w-histo-actions">
        <button data-save-histo>Save Caps</button>
        <button data-reset-histo class="btn-secondary">Reset to Averages</button>
        <span data-histo-status></span>
      </div>

      <details class="w-section w-histo-legacy">
        <summary><h3>Manual Caps</h3></summary>
        <div class="w-list">${flatCapsHTML}</div>
        <section class="w-section">
          <h3>Add Manual Cap</h3>
          <form data-add-form class="w-form">
            <div class="field">
              <label>Label</label>
              <input type="text" name="label" required maxlength="40" placeholder="e.g. daily limit" />
            </div>
            <div class="w-form-row">
              <div class="field">
                <label>Scope</label>
                <select name="scope">${caps.scopes.map(s => `<option value="${s}">${s}</option>`).join("")}</select>
              </div>
              <div class="field">
                <label>Window</label>
                <select name="window">${caps.windows.map(w => `<option value="${w}">${w}</option>`).join("")}</select>
              </div>
              <div class="field">
                <label>Limit</label>
                <input type="number" name="limit" min="1" value="50" required />
              </div>
            </div>
            <div class="field">
              <label><input type="checkbox" name="exclude_exempt" checked /> Exclude exempt channels</label>
            </div>
            <div><button type="submit">Add Cap</button><span data-add-status></span></div>
          </form>
        </section>
      </details>
    `;

    // ── Build chart ──────────────────────────────────────────────
    if (chart) { chart.destroy(); chart = null; }
    const canvas = container.querySelector("[data-histo-canvas]");
    chart = new Chart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Avg XP",
            data: [...bucketAvgs],
            backgroundColor: "#E6B84C",
            barPercentage: 0.85,
            categoryPercentage: 0.9,
            order: 2,
          },
          {
            label: "Cap Limit",
            data: [...sliderValues],
            type: "line",
            borderColor: "#B36A92",
            backgroundColor: "#B36A9233",
            borderWidth: 2,
            pointRadius: 4,
            pointHoverRadius: 6,
            stepped: "middle",
            fill: false,
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          tooltip: { backgroundColor: "#18191c", borderColor: "#3f4147", borderWidth: 1 },
          legend: { position: "bottom", labels: { color: "#dbdee1" } },
        },
        scales: {
          x: { grid: { color: "#3f4147" }, ticks: { color: "#dbdee1", maxRotation: 45, minRotation: 0 } },
          y: { grid: { color: "#3f4147" }, ticks: { color: "#dbdee1", precision: 0 }, beginAtZero: true },
        },
      },
    });

    // ── Align slider row with chart area ─────────────────────────
    function alignSliders() {
      const area = chart.chartArea;
      if (!area) return;
      const row = container.querySelector("[data-slider-row]");
      row.style.paddingLeft = area.left + "px";
      row.style.paddingRight = (canvas.parentElement.offsetWidth - area.right) + "px";
    }
    // Align after initial render and on resize
    requestAnimationFrame(alignSliders);
    const ro = new ResizeObserver(alignSliders);
    ro.observe(canvas.parentElement);

    // ── Slider interaction ───────────────────────────────────────
    container.querySelectorAll("[data-slider-idx]").forEach(slider => {
      slider.addEventListener("input", () => {
        const idx = parseInt(slider.dataset.sliderIdx);
        const val = parseInt(slider.value);
        sliderValues[idx] = val;
        container.querySelector(`[data-val-idx="${idx}"]`).textContent = val;
        chart.data.datasets[1].data[idx] = val;
        chart.update("none");
      });
    });

    // ── Mode / lookback controls ─────────────────────────────────
    container.querySelector('[data-control="mode"]').addEventListener("change", (e) => {
      currentMode = e.target.value;
      load();
    });
    container.querySelector('[data-control="days"]').addEventListener("change", (e) => {
      currentDays = parseInt(e.target.value);
      load();
    });

    // ── Save histogram caps ──────────────────────────────────────
    const histoStatus = container.querySelector("[data-histo-status]");
    container.querySelector("[data-save-histo]").addEventListener("click", async () => {
      const window_ = currentMode === "daily" ? "daily" : "weekly";
      try {
        if (existingHistoCap) {
          await wPut(`/api/wellness/caps/${existingHistoCap.id}`, { bucket_limits: sliderValues });
        } else {
          const label = currentMode === "daily" ? "Daily Activity Cap" : "Weekly Activity Cap";
          await wPost("/api/wellness/caps", {
            label,
            scope: "global",
            window: window_,
            limit: Math.max(...sliderValues),
            exclude_exempt: true,
            bucket_limits: sliderValues,
          });
        }
        showStatus(histoStatus, true, "Caps saved");
        // Reload to update existingHistoCap reference
        load();
      } catch (e) {
        showStatus(histoStatus, false, e.message);
      }
    });

    // ── Reset to averages ────────────────────────────────────────
    container.querySelector("[data-reset-histo]").addEventListener("click", () => {
      sliderValues = bucketAvgs.map(v => Math.max(1, Math.ceil(v)));
      container.querySelectorAll("[data-slider-idx]").forEach(slider => {
        const idx = parseInt(slider.dataset.sliderIdx);
        slider.value = sliderValues[idx];
        container.querySelector(`[data-val-idx="${idx}"]`).textContent = sliderValues[idx];
      });
      chart.data.datasets[1].data = [...sliderValues];
      chart.update("none");
    });

    // ── Legacy flat caps: save/delete/add ─────────────────────────
    container.querySelectorAll("[data-save-cap]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.saveCap;
        const row = container.querySelector(`[data-cap-id="${id}"]`);
        const limit = parseInt(row.querySelector("[data-edit-limit]").value, 10);
        const st = container.querySelector(`[data-cap-status="${id}"]`);
        try { await wPut(`/api/wellness/caps/${id}`, { limit }); showStatus(st, true); }
        catch (e) { showStatus(st, false, e.message); }
      });
    });

    container.querySelectorAll("[data-del-cap]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!confirm("Remove this cap?")) return;
        try { await wDelete(`/api/wellness/caps/${btn.dataset.delCap}`); load(); }
        catch (e) { alert(e.message); }
      });
    });

    const form = container.querySelector("[data-add-form]");
    const addSt = container.querySelector("[data-add-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await wPost("/api/wellness/caps", {
          label: fd.get("label"),
          scope: fd.get("scope"),
          window: fd.get("window"),
          limit: parseInt(fd.get("limit"), 10),
          exclude_exempt: form.querySelector("[name=exclude_exempt]").checked,
        });
        load();
      } catch (err) { showStatus(addSt, false, err.message); }
    });
  }

  load();
}
