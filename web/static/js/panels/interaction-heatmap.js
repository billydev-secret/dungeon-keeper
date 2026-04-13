import { api } from "../api.js";

/* ── colour helpers ──────────────────────────────────────────────────── */

const STOPS = [
  [0.00, [47, 49, 54]],   // #2f3136  (dark bg)
  [0.33, [58, 61, 143]],  // #3a3d8f
  [0.66, [88, 101, 242]], // #5865f2  (blurple)
  [1.00, [235, 69, 158]], // #eb459e  (pink)
];

function lerpColor(t) {
  t = Math.max(0, Math.min(1, t));
  for (let i = 1; i < STOPS.length; i++) {
    const [t0, c0] = STOPS[i - 1];
    const [t1, c1] = STOPS[i];
    if (t <= t1) {
      const f = (t - t0) / (t1 - t0);
      return `rgb(${Math.round(c0[0] + (c1[0] - c0[0]) * f)},${Math.round(c0[1] + (c1[1] - c0[1]) * f)},${Math.round(c0[2] + (c1[2] - c0[2]) * f)})`;
    }
  }
  const c = STOPS[STOPS.length - 1][1];
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

/* ── panel ───────────────────────────────────────────────────────────── */

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Interaction Heatmap</h2>
        <div class="subtitle">Animated adjacency matrix — watch interaction patterns evolve over time</div>
      </header>
      <div class="controls">
        <label>Resolution
          <select data-control="resolution">
            <option value="week">Week</option>
            <option value="day">Day</option>
          </select>
        </label>
        <label>Lookback (days)
          <input type="number" data-control="days" min="7" max="365"
                 value="${initialParams.days || 90}" />
        </label>
        <label>Top users
          <input type="number" data-control="topn" min="5" max="40"
                 value="${initialParams.top_n || 20}" />
        </label>
      </div>

      <div class="ihm-canvas-wrap">
        <canvas data-heatmap></canvas>
      </div>

      <div class="ihm-transport">
        <button data-btn="play" title="Play / Pause">&#9654;</button>
        <input type="range" data-scrubber min="0" value="0" step="1" />
        <span data-frame-label class="ihm-frame-label"></span>
        <label>Speed
          <select data-speed>
            <option value="2000">Slow</option>
            <option value="1000" selected>Normal</option>
            <option value="400">Fast</option>
          </select>
        </label>
      </div>

      <div data-tooltip class="ihm-tooltip"></div>
    </div>
  `;

  const resEl    = container.querySelector('[data-control="resolution"]');
  const daysEl   = container.querySelector('[data-control="days"]');
  const topnEl   = container.querySelector('[data-control="topn"]');
  const canvas   = container.querySelector("[data-heatmap]");
  const playBtn  = container.querySelector('[data-btn="play"]');
  const scrubber = container.querySelector("[data-scrubber]");
  const frameLbl = container.querySelector("[data-frame-label]");
  const speedSel = container.querySelector("[data-speed]");
  const tooltip  = container.querySelector("[data-tooltip]");
  const ctx      = canvas.getContext("2d");

  resEl.value = initialParams.resolution || "week";

  let data = null;
  let frameIdx = 0;
  let playing = false;
  let timer = null;

  /* ── drawing ─────────────────────────────────────────────────────── */

  const LABEL_PAD = 8;      // px between label and grid
  const MIN_CELL  = 14;
  const MAX_CELL  = 32;
  const BAR_W     = 16;
  const BAR_GAP   = 12;

  function measureLabelWidth(names) {
    ctx.save();
    ctx.font = "11px monospace";
    let max = 0;
    for (const n of names) max = Math.max(max, ctx.measureText(n).width);
    ctx.restore();
    return Math.ceil(max) + LABEL_PAD;
  }

  function drawFrame(fi) {
    if (!data || !data.frames.length) return;
    const frame = data.frames[fi];
    const n = data.users.length;
    const names = data.users.map(u => u.user_name || u.user_id);
    const labelW = measureLabelWidth(names);
    const cell = Math.max(MIN_CELL, Math.min(MAX_CELL, Math.floor((Math.min(container.offsetWidth - 60, 900) - labelW - BAR_W - BAR_GAP) / n)));
    const gridPx = n * cell;

    const dpr = window.devicePixelRatio || 1;
    const cw = labelW + gridPx + BAR_GAP + BAR_W + 20;
    const ch = labelW + gridPx + 30;  // 30 for frame label
    canvas.width = cw * dpr;
    canvas.height = ch * dpr;
    canvas.style.width = cw + "px";
    canvas.style.height = ch + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // clear
    ctx.fillStyle = "#2b2d31";
    ctx.fillRect(0, 0, cw, ch);

    // left labels
    ctx.save();
    ctx.font = "11px monospace";
    ctx.fillStyle = "#dbdee1";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i < n; i++) {
      ctx.fillText(names[i], labelW - LABEL_PAD, labelW + i * cell + cell / 2);
    }
    ctx.restore();

    // top labels (rotated)
    ctx.save();
    ctx.font = "11px monospace";
    ctx.fillStyle = "#dbdee1";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let j = 0; j < n; j++) {
      const x = labelW + j * cell + cell / 2;
      const y = labelW - LABEL_PAD;
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(-Math.PI / 4);
      ctx.fillText(names[j], 0, 0);
      ctx.restore();
    }
    ctx.restore();

    // cells
    const gMax = data.global_max || 1;
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        const v = frame.matrix[i][j];
        const x = labelW + j * cell;
        const y = labelW + i * cell;
        ctx.fillStyle = i === j ? "#2b2d31" : lerpColor(v / gMax);
        ctx.fillRect(x, y, cell - 1, cell - 1);
        // annotate small matrices
        if (n <= 20 && v > 0 && i !== j) {
          ctx.save();
          ctx.font = `${Math.min(10, cell - 4)}px monospace`;
          ctx.fillStyle = v / gMax > 0.5 ? "#fff" : "#949ba4";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(String(v), x + (cell - 1) / 2, y + (cell - 1) / 2);
          ctx.restore();
        }
      }
    }

    // colour bar
    const barX = labelW + gridPx + BAR_GAP;
    const barH = gridPx;
    for (let py = 0; py < barH; py++) {
      ctx.fillStyle = lerpColor(1 - py / barH);
      ctx.fillRect(barX, labelW + py, BAR_W, 1);
    }
    ctx.save();
    ctx.font = "10px monospace";
    ctx.fillStyle = "#949ba4";
    ctx.textAlign = "left";
    ctx.fillText(String(gMax), barX + BAR_W + 4, labelW + 6);
    ctx.fillText("0", barX + BAR_W + 4, labelW + barH);
    ctx.restore();

    // frame label
    ctx.save();
    ctx.font = "bold 12px monospace";
    ctx.fillStyle = "#dbdee1";
    ctx.textAlign = "center";
    ctx.fillText(frame.label, labelW + gridPx / 2, labelW + gridPx + 20);
    ctx.restore();

    // store layout for hover hit-testing
    canvas._ihm = { labelW, cell, n, fi };
  }

  /* ── hover tooltip ───────────────────────────────────────────────── */

  canvas.addEventListener("mousemove", (e) => {
    if (!data || !canvas._ihm) { tooltip.style.display = "none"; return; }
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const { labelW, cell, n, fi } = canvas._ihm;
    const j = Math.floor((mx - labelW) / cell);
    const i = Math.floor((my - labelW) / cell);
    if (i < 0 || i >= n || j < 0 || j >= n || i === j) {
      tooltip.style.display = "none";
      return;
    }
    const names = data.users.map(u => u.user_name || u.user_id);
    const v = data.frames[fi].matrix[i][j];
    tooltip.textContent = `${names[i]} ↔ ${names[j]}: ${v}`;
    tooltip.style.display = "block";
    tooltip.style.left = (e.clientX - container.getBoundingClientRect().left + 12) + "px";
    tooltip.style.top = (e.clientY - container.getBoundingClientRect().top - 8) + "px";
  });
  canvas.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });

  /* ── animation ───────────────────────────────────────────────────── */

  function tick() {
    drawFrame(frameIdx);
    scrubber.value = frameIdx;
    frameLbl.textContent = data.frames[frameIdx]?.label || "";
    frameIdx = (frameIdx + 1) % data.frames.length;
    if (playing) timer = setTimeout(tick, parseInt(speedSel.value));
  }

  function stopPlayback() {
    playing = false;
    playBtn.innerHTML = "&#9654;";
    if (timer) { clearTimeout(timer); timer = null; }
  }

  playBtn.addEventListener("click", () => {
    if (!data || !data.frames.length) return;
    playing = !playing;
    playBtn.innerHTML = playing ? "&#9646;&#9646;" : "&#9654;";
    if (playing) tick();
    else { clearTimeout(timer); timer = null; }
  });

  scrubber.addEventListener("input", () => {
    if (!data) return;
    frameIdx = parseInt(scrubber.value);
    drawFrame(frameIdx);
    frameLbl.textContent = data.frames[frameIdx]?.label || "";
  });

  /* ── data fetch ──────────────────────────────────────────────────── */

  async function refresh() {
    stopPlayback();
    const params = {
      resolution: resEl.value,
      days: parseInt(daysEl.value) || 90,
      top_n: parseInt(topnEl.value) || 20,
    };

    const qs = new URLSearchParams(params);
    history.replaceState(null, "", `#/interaction-heatmap?${qs}`);

    try {
      data = await api("/api/reports/interaction-heatmap", params);
      if (!data.frames.length) {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        container.querySelector(".ihm-canvas-wrap").innerHTML =
          '<div class="empty">No interaction data for this window.</div>';
        return;
      }
      frameIdx = 0;
      scrubber.max = data.frames.length - 1;
      scrubber.value = 0;
      drawFrame(0);
      frameLbl.textContent = data.frames[0].label;
    } catch (err) {
      container.querySelector(".ihm-canvas-wrap").innerHTML =
        `<div class="error">${err.message}</div>`;
    }
  }

  resEl.addEventListener("change", refresh);
  daysEl.addEventListener("change", refresh);
  topnEl.addEventListener("change", refresh);
  refresh();

  return {
    unmount() {
      stopPlayback();
      data = null;
    },
  };
}
