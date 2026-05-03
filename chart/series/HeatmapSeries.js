// HeatmapSeries.js
// Bookmap-like heatmap with:
// - Fixed price anchor (stable horizontal bands)
// - Two history modes:
//    * 'accum' : time-integral inside candle: acc += currDepth * dt
//    * 'curr'  : snapshot of instant book (_curr) each tick (recommended for "bands")
// - Render mode "bandsToRight":
//    * NO OVERLAP: slices are drawn as non-overlapping segments [x_i .. x_{i+1})
// - Timestamp-based X mapping (prevents desync when time domain resets / candles are reloaded)
// - Percentile + EMA normalization (stable colors)
// - Hard thresholds: cold/hot bands
// - dt clamp + gap reset + re-anchor (prevents lag stripes and post-lag desync)
// - Live overlay column (shows real-time activity on the right edge)

const PIXI = window.PIXI;

function clamp01(x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }
function clamp(x, a, b) { return x < a ? a : (x > b ? b : x); }

function quantSteps(price, tick) {
  return Math.round(price / tick);
}

function heatColorRGB(t) {
  t = clamp01(t);
  const stops = [
    { t: 0.00, c: [  5,  10,  25] },
    { t: 0.20, c: [  0,  60, 160] },
    { t: 0.40, c: [  0, 190, 255] },
    { t: 0.60, c: [255, 235,   0] },
    { t: 0.78, c: [255, 120,   0] },
    { t: 0.92, c: [255,   0,   0] },
    { t: 1.00, c: [255, 255, 255] }, // white peak (we cap u<=0.92 so we never hit this)
  ];
  for (let i = 1; i < stops.length; i++) {
    const a = stops[i - 1], b = stops[i];
    if (t <= b.t) {
      const k = (t - a.t) / Math.max(1e-9, (b.t - a.t));
      const r = Math.round(a.c[0] + (b.c[0] - a.c[0]) * k);
      const g = Math.round(a.c[1] + (b.c[1] - a.c[1]) * k);
      const bl = Math.round(a.c[2] + (b.c[2] - a.c[2]) * k);
      return [r, g, bl];
    }
  }
  return stops[stops.length - 1].c;
}

function nowMs() { return (performance && performance.now) ? performance.now() : Date.now(); }

function getBarTs(bar) {
  if (!bar) return NaN;
  if (typeof bar.timestamp === 'number') return bar.timestamp;
  if (typeof bar.ts === 'number') return bar.ts;
  if (typeof bar.time === 'number') return bar.time;
  return NaN;
}

export class HeatmapSeries {
  constructor(opts = {}) {
    // references
    this._candleSeries = opts.candleSeries ?? null;
    this._tfSec = Math.max(1, Number(opts.tfSec ?? 60));

    // grid/window
    this.levelsPerSide = Math.max(10, opts.levelsPerSide ?? 200);
    this.tickSize      = Math.max(1e-9, opts.tickSize ?? 0.001);
    this.maxColumns    = Math.max(50, opts.maxColumns ?? 350);
    this.totalLevels   = this.levelsPerSide * 2;

    // visuals (history)  -> по дефолту ТУСКЛО, не выедает глаза
    this.alpha       = clamp01(opts.alpha ?? 0.35);
    this.useLog      = (opts.useLog ?? true) === true;
    this.minAlphaCut = clamp01(opts.minAlphaCut ?? 0.03);
    this.emaAlphaRef = clamp01(opts.emaAlphaRef ?? 0.08);
    this.pctl        = clamp(opts.percentile ?? 0.97, 0.80, 0.999);

    // dt control (for accum)
    this.maxDt      = Number(opts.maxDt ?? 0.35);
    this.resetGapDt = Number(opts.resetGapDt ?? 1.2);

    // hard thresholds (history default)
    this.coldThreshold = Number(opts.coldThreshold ?? 2000);
    this.hotThreshold  = Number(opts.hotThreshold  ?? 10000);

    // history behavior
    this.historyMode = (opts.historyMode === 'curr' || opts.historyMode === 'accum')
      ? opts.historyMode
      : 'accum';

    // render behavior
    this.bandsToRight = (opts.bandsToRight ?? true) === true;

    // snapshot throttle
    this.snapshotMaxHz = Math.max(0, Number(opts.snapshotMaxHz ?? 60));
    this._snapMinMs = this.snapshotMaxHz > 0 ? (1000 / this.snapshotMaxHz) : 0;
    this._lastSnapMs = null;

    // optional override thresholds for history
    this.historyColdThreshold = Number.isFinite(opts.historyColdThreshold) ? Number(opts.historyColdThreshold) : NaN;
    this.historyHotThreshold  = Number.isFinite(opts.historyHotThreshold)  ? Number(opts.historyHotThreshold)  : NaN;

    // live overlay (activity) -> тоже тускло
    this.liveAlpha       = clamp01(opts.liveAlpha ?? 0.20);
    this.liveUseAccum    = !!(opts.liveUseAccum ?? false);
    this.liveMinAlphaCut = clamp01(opts.liveMinAlphaCut ?? 0.01);

    this.liveColdThreshold = Number(opts.liveColdThreshold ?? 200);
    this.liveHotThreshold  = Number(opts.liveHotThreshold  ?? 1000);

    this.livePercentile  = clamp(opts.livePercentile ?? 0.95, 0.80, 0.999);
    this.liveEmaAlphaRef = clamp01(opts.liveEmaAlphaRef ?? 0.15);

    // center "core" inside each PRICE band (horizontal): thicker band with brighter center
    this.centerCoreEnabled  = (opts.centerCoreEnabled ?? true) === true;

    // how many texture pixels per one price level (band thickness)
    this.bandPx = Math.max(1, (opts.bandPx ?? 3) | 0);

    // how many pixels inside band are "core" (must be <= bandPx)
    this.corePx = Math.max(0, Math.min(this.bandPx, (opts.corePx ?? 1) | 0));

    // core brightness (alpha multiplier) + edge dimming
    this.centerCoreAlphaMul = clamp(opts.centerCoreAlphaMul ?? 1.35, 1.0, 3.0);
    this.edgeAlphaMul       = clamp(opts.edgeAlphaMul ?? 0.70, 0.05, 1.0);

    // anchor
    this._anchorSteps = null;
    this._anchorMid   = null;

    // book buffers
    this._curr = new Float32Array(this.totalLevels);
    this._acc  = new Float32Array(this.totalLevels);
    this._lastUpdateMs = null;

    // refs
    this._refEma = 1.0;
    this._liveRefEma = 1.0;

    this.lockAnchor = !!opts.lockAnchor;

    // columns (history)
    this._cols = new Array(this.maxColumns);
    this._head = 0;
    this._size = 0;

    this.gfx = new PIXI.Container();
    this.gfx.sortableChildren = this.bandsToRight;


    // last L2
    this._live = { bids: [], asks: [], mid: null };

    // live column sprite (right edge)
    this._liveCol = this._makeColumnSprite(this.liveAlpha);
    this._liveCol.sprite.zIndex = 1e9;
    this.gfx.addChild(this._liveCol.sprite);
  }

  setCandleSeries(series) { this._candleSeries = series; }
  setTfSec(tfSec) { this._tfSec = Math.max(1, Number(tfSec || 1)); }
  setLockAnchor(v) { this.lockAnchor = !!v; }

  _makeColumnSprite(alpha = 1.0) {
    const canvas = document.createElement('canvas');
    canvas.width = 1;

    const H = this.totalLevels * this.bandPx;
    canvas.height = H;

    const ctx = canvas.getContext('2d', { willReadFrequently: false });
    const imgData = ctx.createImageData(1, H);

    const tex = PIXI.Texture.from(canvas);
    tex.baseTexture.scaleMode = PIXI.SCALE_MODES.LINEAR;

    const spr = new PIXI.Sprite(tex);
    spr.alpha = alpha;
    spr.roundPixels = true;
    spr.visible = false;
    spr.tint = 0xffffff;

    return { canvas, ctx, imgData, tex, sprite: spr, barIndex: null, ts: null, anchorSteps: null };
  }

  setAnchor(mid) {
    const m = Number(mid);
    if (!isFinite(m)) return;

    this._anchorMid = m;
    this._anchorSteps = quantSteps(m, this.tickSize);
  }

  getPriceWindow() {
    if (this._anchorMid == null) return null;
    const anchor = this._anchorMid;
    const span = this.levelsPerSide * this.tickSize;
    return [anchor - span, anchor + span];
  }

  heatmapSuggestedDomain(midMaybe = null) {
    // если якоря ещё нет, но пришёл mid — ставим якорь
    if ((this._anchorMid == null || !isFinite(this._anchorMid)) && midMaybe != null) {
      this.setAnchor(midMaybe);
    }
    return this.getPriceWindow();
  }

  clear() {
    for (let i = 0; i < this.maxColumns; i++) {
      const c = this._cols[i];
      if (c?.sprite) this.gfx.removeChild(c.sprite);
      if (c?.tex) c.tex.destroy(true);
      this._cols[i] = null;
    }
    this._head = 0;
    this._size = 0;

    this._curr.fill(0);
    this._acc.fill(0);
    this._lastUpdateMs = null;

    this._refEma = 1.0;
    this._liveRefEma = 1.0;

    // keep live sprite, just clear it
    if (this._liveCol?.imgData) {
      this._liveCol.imgData.data.fill(0);
      this._liveCol.ctx.putImageData(this._liveCol.imgData, 0, 0);
      this._liveCol.tex.update();
    }
    if (this._liveCol?.sprite) this._liveCol.sprite.visible = false;
  }

  // === IMPORTANT: shift history by PRICE STEPS, but textures are in PIXELS (bandPx per level) ===
  // === IMPORTANT: shift buffers by PRICE STEPS (LEVELS). HISTORY TEXTURES MUST NOT BE SHIFTED ===
  _shiftHistoryBySteps(deltaSteps, adjustAnchor = true) {
    const ds = deltaSteps | 0;
    if (!ds) return;

    // ---- 1) shift numeric arrays in LEVELS ----
    const shiftArr = (arr) => {
      const n = arr.length;
      if (Math.abs(ds) >= n) { arr.fill(0); return; }

      if (ds > 0) {
        for (let i = n - 1; i >= ds; i--) arr[i] = arr[i - ds];
        for (let i = 0; i < ds; i++) arr[i] = 0;
      } else {
        const k = -ds;
        for (let i = 0; i < n - k; i++) arr[i] = arr[i + k];
        for (let i = n - k; i < n; i++) arr[i] = 0;
      }
    };

    shiftArr(this._curr);
    shiftArr(this._acc);

    // ---- 2) DO NOT SHIFT HISTORY TEXTURES ----
    // History is immutable. Columns will be visually aligned in draw() using per-column anchorSteps.

    // ---- 3) optionally adjust anchor ----
    if (adjustAnchor && this._anchorSteps != null) {
      this._anchorSteps += ds;
      this._anchorMid = this._anchorSteps * this.tickSize;
    }
  }



  _clearHistoryOnly() {
    // reset history without destroying textures (cheap re-sync on anchor jumps)
    for (let i = 0; i < this.maxColumns; i++) {
      const c = this._cols[i];
      if (!c) continue;
      c.barIndex = null;
      c.ts = null;
      if (c.imgData?.data) c.imgData.data.fill(0);
      if (c.ctx && c.imgData) c.ctx.putImageData(c.imgData, 0, 0);
      if (c.tex) c.tex.update();
      if (c.sprite) c.sprite.visible = false;
    }
    this._head = 0;
    this._size = 0;
  }

  updateOrderbook(bids, asks, mid) {
    this._live.bids = Array.isArray(bids) ? bids : [];
    this._live.asks = Array.isArray(asks) ? asks : [];
    const m = Number(mid);
    if (isFinite(m)) this._live.mid = m;

    if (this._anchorSteps === null && isFinite(m)) {
      this.setAnchor(m);
    }

    // lockAnchor = "history pinned": anchor may follow mid, but we compensate by shifting history in opposite direction
    if (this._anchorSteps !== null && isFinite(m)) {
      const ms = quantSteps(m, this.tickSize);
      const delta = ms - this._anchorSteps;

      const hardThr = Math.floor(this.levelsPerSide * 0.70);
      const softThr = 2;

      if (Math.abs(delta) >= hardThr) {
        // huge jump: reset history + re-anchor
        this._clearHistoryOnly();
        this.setAnchor(m);
      } else if (!this.lockAnchor && Math.abs(delta) >= softThr) {
        // non-pinned: recenter smoothly
        this._shiftHistoryBySteps(delta, true);
      } else {
        // pinned OR small delta: keep current anchorSteps (and therefore fixed vertical grid)
        this._anchorMid = this._anchorSteps * this.tickSize;
      }
    }


    // integrate dt into _acc BEFORE rebuilding _curr (only when needed)
    const t = nowMs();
    if (this._lastUpdateMs != null) {
      let dt = Math.max(0, (t - this._lastUpdateMs) / 1000.0);

      if (dt >= this.resetGapDt) {
        this._acc.fill(0);

        // PINNED: do not touch anchor on gaps.
        // Non-pinned: you may resync to mid if you want.
        if (!this.lockAnchor) {
          const mm = isFinite(m) ? m : (isFinite(this._live.mid) ? this._live.mid : NaN);
          if (isFinite(mm)) this.setAnchor(mm);
        }

        dt = 0;
      } else {


        dt = Math.min(dt, this.maxDt);

        if (dt > 0 && (this.historyMode === 'accum' || this.liveUseAccum)) {
          for (let i = 0; i < this.totalLevels; i++) {
            this._acc[i] += this._curr[i] * dt;
          }
        }
      }
    }
    this._lastUpdateMs = t;

    // rebuild current depth on fixed grid (by anchorSteps)
    this._rebuildCurrFromBook(this._live.bids, this._live.asks);

    // repaint live overlay (right edge activity)
    if (this._liveCol) {
      let arr, ref, coldThr, hotThr, cut;

      if (this.liveUseAccum) {
        arr = this._acc;
        ref = Math.max(this._refEma, 1e-9);
        coldThr = this.coldThreshold;
        hotThr  = this.hotThreshold;
        cut     = this.minAlphaCut;
      } else {
        arr = this._curr;
        ref = this._computeRefFromCurrLive();
        coldThr = this.liveColdThreshold;
        hotThr  = this.liveHotThreshold;
        cut     = this.liveMinAlphaCut;
      }

      this._paintColumn(this._liveCol, arr, ref, coldThr, hotThr, cut, this.livePercentile, this.liveEmaAlphaRef);
      this._liveCol.sprite.alpha = this.liveAlpha;
    }
  }


  _rebuildCurrFromBook(bids, asks) {
    this._curr.fill(0);

    if (this._anchorSteps === null) return;
    const anchorSteps = this._anchorSteps;

    // bids
    for (let i = 0; i < bids.length; i++) {
      const p = Number(bids[i].price);
      const s = Number(bids[i].size ?? bids[i].volume ?? bids[i].qty ?? bids[i].amount);
      if (!isFinite(p) || !isFinite(s) || s <= 0) continue;
      const ps = quantSteps(p, this.tickSize);
      const delta = ps - anchorSteps;
      const idx = this.levelsPerSide + delta;
      if (idx >= 0 && idx < this.totalLevels) this._curr[idx] += s;
    }

    // asks
    for (let i = 0; i < asks.length; i++) {
      const p = Number(asks[i].price);
      const s = Number(asks[i].size ?? asks[i].volume ?? asks[i].qty ?? asks[i].amount);
      if (!isFinite(p) || !isFinite(s) || s <= 0) continue;
      const ps = quantSteps(p, this.tickSize);
      const delta = ps - anchorSteps;
      const idx = this.levelsPerSide + delta;
      if (idx >= 0 && idx < this.totalLevels) this._curr[idx] += s;
    }
  }

  _computeRefFromAccumHist() {
    const arr = this._acc;
    const n = arr.length;

    let maxV = 0;
    for (let i = 0; i < n; i++) maxV = Math.max(maxV, arr[i]);
    if (maxV <= 0) return Math.max(this._refEma, 1e-9);

    const tmp = new Float32Array(n);
    let m = 0;
    for (let i = 0; i < n; i++) {
      const v = arr[i];
      if (v > 0) tmp[m++] = v;
    }
    if (m <= 0) return Math.max(this._refEma, 1e-9);

    const values = Array.from(tmp.subarray(0, m)).sort((a, b) => a - b);
    const idx = Math.max(0, Math.min(values.length - 1, Math.floor(values.length * this.pctl)));
    const pctlV = values[idx];

    const target = Math.max(1e-9, pctlV);
    const a = this.emaAlphaRef;
    this._refEma = (1 - a) * this._refEma + a * target;
    return Math.max(this._refEma, 1e-9);
  }

  _computeRefFromCurrHist() {
    const arr = this._curr;
    const n = arr.length;

    let maxV = 0;
    for (let i = 0; i < n; i++) maxV = Math.max(maxV, arr[i]);
    if (maxV <= 0) return Math.max(this._refEma, 1e-9);

    const tmp = new Float32Array(n);
    let m = 0;
    for (let i = 0; i < n; i++) {
      const v = arr[i];
      if (v > 0) tmp[m++] = v;
    }
    if (m <= 0) return Math.max(this._refEma, 1e-9);

    const values = Array.from(tmp.subarray(0, m)).sort((a, b) => a - b);
    const idx = Math.max(0, Math.min(values.length - 1, Math.floor(values.length * this.pctl)));
    const pctlV = values[idx];

    const target = Math.max(1e-9, pctlV);
    const a = this.emaAlphaRef;
    this._refEma = (1 - a) * this._refEma + a * target;
    return Math.max(this._refEma, 1e-9);
  }

  _computeRefFromCurrLive() {
    const arr = this._curr;
    const n = arr.length;

    let maxV = 0;
    for (let i = 0; i < n; i++) maxV = Math.max(maxV, arr[i]);
    if (maxV <= 0) return Math.max(this._liveRefEma, 1e-9);

    const tmp = new Float32Array(n);
    let m = 0;
    for (let i = 0; i < n; i++) {
      const v = arr[i];
      if (v > 0) tmp[m++] = v;
    }
    if (m <= 0) return Math.max(this._liveRefEma, 1e-9);

    const values = Array.from(tmp.subarray(0, m)).sort((a, b) => a - b);
    const idx = Math.max(0, Math.min(values.length - 1, Math.floor(values.length * this.livePercentile)));
    const pctlV = values[idx];

    const target = Math.max(1e-9, pctlV);
    const a = this.liveEmaAlphaRef;
    this._liveRefEma = (1 - a) * this._liveRefEma + a * target;
    return Math.max(this._liveRefEma, 1e-9);
  }

  // === Pixel painter (dim, non-eye-burning) ===
  _paintColumn(col, arr, ref, coldThr, hotThr, cut) {
    const img = col.imgData;
    const data = img.data;

    data.fill(0);

    const bandPx = this.bandPx | 0;
    const corePx = Math.max(0, Math.min(bandPx, (this.corePx | 0)));
    const H = this.totalLevels * bandPx;

    const den = this.useLog ? Math.log1p(ref) : ref;

    const cold = Math.max(0, Number(coldThr));
    const hot  = Math.max(cold + 1e-9, Number(hotThr));

    const MAX_A = 160;
    const GAMMA = 1.55;

    const CORE_MUL  = 1.00;
    const EDGE1_MUL = 0.38;
    const EDGE2_MUL = 0.14;

    const writeMaxPx = (py, r, g, b, a) => {
      if (py < 0 || py >= H) return;
      const o = py * 4;
      if (a > data[o + 3]) {
        data[o + 0] = r;
        data[o + 1] = g;
        data[o + 2] = b;
        data[o + 3] = a;
      }
    };

    const coreStart = (corePx > 0 && bandPx > corePx) ? ((bandPx - corePx) >> 1) : 0;
    const coreEnd   = coreStart + Math.max(1, corePx) - 1;

    for (let idx = 0; idx < this.totalLevels; idx++) {
      const v0 = arr[idx];
      if (!(v0 > 0)) continue;

      let t = this.useLog
        ? (Math.log1p(v0) / Math.max(1e-9, den))
        : (v0 / Math.max(1e-9, den));

      t = clamp01(t);
      t = Math.pow(t, GAMMA);
      if (t <= cut) continue;

      let aCurve = (t - cut) / Math.max(1e-9, (1 - cut));
      aCurve = clamp01(aCurve);
      aCurve = Math.pow(aCurve, 1.25);

      let u, aMul;
      if (v0 < cold) {
        const k = v0 / Math.max(1e-9, cold);
        u = 0.07 + 0.14 * clamp01(k);
        aMul = 0.22;
      } else if (v0 >= hot) {
        u = 0.78 + 0.14 * t;
        aMul = 1.05;
      } else {
        const k = (v0 - cold) / Math.max(1e-9, (hot - cold));
        u = 0.30 + 0.48 * clamp01(k);
        aMul = 0.40 + 0.55 * clamp01(k);
      }

      u = Math.min(0.92, Math.max(0, u));
      const [r, g, b] = heatColorRGB(u);

      let a = Math.round(MAX_A * aCurve * aMul);

      if (v0 >= hot) {
        const hotMin = Math.round(MAX_A * 0.42);
        if (a < hotMin) a = hotMin;
      }

      const aCutPx = Math.round(MAX_A * cut);
      if (a < aCutPx) continue;

      // IMPORTANT: map LEVEL idx -> pixel band (bandPx per level)
      const bandTop = (this.totalLevels - 1 - idx) * bandPx;

      for (let py = 0; py < bandPx; py++) {
        let mul;

        if (corePx <= 0 || bandPx <= 1) {
          mul = CORE_MUL;
        } else if (py >= coreStart && py <= coreEnd) {
          mul = CORE_MUL;
        } else {
          const dist = (py < coreStart) ? (coreStart - py) : (py - coreEnd);
          mul = (dist === 1) ? EDGE1_MUL : EDGE2_MUL;
        }

        writeMaxPx(bandTop + py, r, g, b, Math.round(a * mul));
      }
    }

    col.ctx.putImageData(img, 0, 0);
    col.tex.update();
  }

  snapshot(barIndex, midMaybe, tsSec = null) {
    const bi = barIndex | 0;
    let ts = Number(tsSec);

    if (!isFinite(ts)) {
      const bars = this._candleSeries?.bars;
      if (bars && bars[bi]) {
        const bts = getBarTs(bars[bi]);
        if (isFinite(bts)) ts = bts;
      }
    }
    if (!isFinite(ts)) ts = Math.floor(Date.now() / 1000);

    const resetAfter = (this.historyMode === 'accum');
    this._snapshotInternal(bi, ts, midMaybe, resetAfter);
  }

  // snapshot each tick (timestamp in seconds)
  snapshotTick(tsSec, midMaybe) {
    const tms = nowMs();
    if (this._snapMinMs > 0 && this._lastSnapMs != null && (tms - this._lastSnapMs) < this._snapMinMs) return;
    this._lastSnapMs = tms;

    const ts = Number(tsSec);
    if (!isFinite(ts)) return;

    const bi = this._approxBarIndexFromTs(ts);
    this._snapshotInternal(bi, ts, midMaybe, false);
  }

  _approxBarIndexFromTs(ts) {
    const bars = this._candleSeries?.bars;
    if (!bars || !bars.length) return 0;

    const firstTs = getBarTs(bars[0]);
    const lastTs  = getBarTs(bars[bars.length - 1]);
    if (!isFinite(firstTs) || !isFinite(lastTs)) return bars.length - 1;

    if (ts <= firstTs) return 0;
    if (ts >= lastTs) return bars.length - 1;

    let lo = 0, hi = bars.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const mts = getBarTs(bars[mid]);
      if (mts < ts) lo = mid + 1; else hi = mid;
    }
    return Math.max(0, lo - 1);
  }

  _snapshotInternal(barIndex, tsSec, midMaybe, resetAfterSnapshot) {
    const bi = barIndex | 0;
    if (bi < 0) return;

    if (this._anchorSteps === null) {
      const m = Number(midMaybe);
      if (isFinite(m)) this.setAnchor(m);
      else if (isFinite(this._live.mid)) this.setAnchor(this._live.mid);
      else return;
    }

    // integrate tiny remaining dt before snapshot (accum only)
    const t = nowMs();
    if (this._lastUpdateMs != null) {
      let dt = Math.max(0, (t - this._lastUpdateMs) / 1000.0);
      if (dt >= this.resetGapDt) {
        this._acc.fill(0);

        if (!this.lockAnchor) {
          const mm = isFinite(Number(midMaybe)) ? Number(midMaybe) : (isFinite(this._live.mid) ? this._live.mid : NaN);
          if (isFinite(mm)) this.setAnchor(mm);
        }

        dt = 0;
      } else {
        dt = Math.min(dt, this.maxDt);
      }

      if (dt > 0 && (this.historyMode === 'accum' || this.liveUseAccum)) {
        for (let i = 0; i < this.totalLevels; i++) {
          this._acc[i] += this._curr[i] * dt;
        }
      }
    }
    this._lastUpdateMs = t;

    const useAccum = (this.historyMode === 'accum');
    const arr = useAccum ? this._acc : this._curr;
    const ref = useAccum ? this._computeRefFromAccumHist() : this._computeRefFromCurrHist();

    const coldThr = isFinite(this.historyColdThreshold)
      ? this.historyColdThreshold
      : (useAccum ? this.coldThreshold : this.liveColdThreshold);

    const hotThr = isFinite(this.historyHotThreshold)
      ? this.historyHotThreshold
      : (useAccum ? this.hotThreshold : this.liveHotThreshold);

    const slot = this._head;
    this._head = (this._head + 1) % this.maxColumns;
    if (this._size < this.maxColumns) this._size++;

    let col = this._cols[slot];
    if (!col) {
      col = this._makeColumnSprite(this.alpha);
      this.gfx.addChild(col.sprite);
      this._cols[slot] = col;
    } else {
      col.sprite.alpha = this.alpha;
      if (col.sprite && col.sprite.parent !== this.gfx) this.gfx.addChild(col.sprite);
    }

    col.barIndex = bi;
    col.ts = tsSec;

    col.anchorSteps = this._anchorSteps;

    this._paintColumn(col, arr, ref, coldThr, hotThr, this.minAlphaCut, this.pctl, this.emaAlphaRef);

    if (resetAfterSnapshot) {
      this._acc.fill(0);
    }
  }

  // === X mapping (timestamp-based, with last-candle fractional extension) ===
  _xFromTs(ts, timeScale) {
    const bars = this._candleSeries?.bars;
    if (!bars || !bars.length) return null;

    const firstTs = getBarTs(bars[0]);
    const lastTs  = getBarTs(bars[bars.length - 1]);
    if (!isFinite(firstTs) || !isFinite(lastTs)) return null;

    if (ts <= firstTs) return timeScale.toX(0);

    if (ts >= lastTs) {
      const frac = (ts - lastTs) / Math.max(1e-9, this._tfSec);
      const t = (bars.length - 1) + clamp(frac, 0, 1);
      return timeScale.toX(t);
    }

    let lo = 0, hi = bars.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const mts = getBarTs(bars[mid]);
      if (mts < ts) lo = mid + 1; else hi = mid;
    }
    const i = lo;
    const a = getBarTs(bars[i - 1]);
    const b = getBarTs(bars[i]);
    const frac = (ts - a) / Math.max(1e-9, b - a);
    const t = (i - 1) + clamp(frac, 0, 1);
    return timeScale.toX(t);
  }

  draw(timeScale, priceScale) {
    if (this._anchorMid == null) return;

    const [r0, r1] = timeScale.range;
    const leftPx  = Math.min(r0, r1);
    const rightPx = Math.max(r0, r1);

    const [d0, d1] = timeScale.domain;
    const i0 = Math.floor(d0);
    const i1 = Math.ceil(d1);
    const unit = Math.abs(timeScale.toX(Math.min(i0 + 1, i1)) - timeScale.toX(i0)) || 1;
    const cellW = Math.max(1, Math.ceil(unit));

    // fixed vertical window around anchorMid
    const span = this.levelsPerSide * this.tickSize;
    const topP = this._anchorMid + span;
    const botP = this._anchorMid - span;

    const yTop = priceScale.toY(topP);
    const yBot = priceScale.toY(botP);

    const y0 = Math.floor(Math.min(yTop, yBot));
    const h0 = Math.max(1, Math.round(Math.abs(yBot - yTop)));
    const stepPx = h0 / this.totalLevels;

    const active = [];
    for (let k = 0; k < this.maxColumns; k++) {
        const col = this._cols[k];
        if (!col || !col.sprite) continue;

        let xCenter = null;
        if (col.ts != null && isFinite(col.ts)) xCenter = this._xFromTs(col.ts, timeScale);
        if (xCenter == null) xCenter = timeScale.toX(col.barIndex | 0);

        active.push({ col, xCenter });
    }

    // hide everything first (prevents tails)
    for (let k = 0; k < active.length; k++) {
      const c = active[k].col;
      if (c?.sprite) c.sprite.visible = false;
    }
    if (this._liveCol?.sprite) this._liveCol.sprite.visible = false;

    if (this.bandsToRight) {
      // segments [x_i .. x_{i+1})
      active.sort((a, b) => a.xCenter - b.xCenter);

      const L = Math.floor(leftPx);
      const R = Math.ceil(rightPx);

      const slices = [];
      for (let k = 0; k < active.length; k++) {
        const it = active[k];
        if (it.xCenter == null || !isFinite(it.xCenter)) continue;

        const xStart = clamp(Math.floor(it.xCenter), L, R);
        let xEnd = (k + 1 < active.length)
          ? clamp(Math.floor(active[k + 1].xCenter), L, R + 1)
          : (R + 1);
        if (xEnd < xStart) xEnd = xStart;

        // collapse duplicates (same xStart): keep newest snapshot
        if (slices.length && slices[slices.length - 1].xStart === xStart) {
          slices[slices.length - 1].col = it.col;
          slices[slices.length - 1].xEnd = xEnd;
        } else {
          slices.push({ col: it.col, xStart, xEnd });
        }
      }

      // close last slice
      if (slices.length) slices[slices.length - 1].xEnd = R + 1;

      let z = 0;
      for (let si = 0; si < slices.length; si++) {
        const s = slices[si];
        const col = s.col;
        if (!col || !col.sprite) continue;

        const w = Math.max(1, Math.floor(s.xEnd - s.xStart));
        if (w <= 0) { col.sprite.visible = false; continue; }

        // per-column anchor compensation (history immutable)
        const aCol = (col.anchorSteps != null) ? col.anchorSteps : this._anchorSteps;
        const dSteps = (this._anchorSteps - aCol);
        const yShift = dSteps * stepPx;

        col.sprite.x = s.xStart;
        col.sprite.y = Math.round(y0 + yShift);
        col.sprite.width  = w;
        col.sprite.height = h0;
        col.sprite.visible = true;
        col.sprite.zIndex = z++;
      }

      // live overlay pinned to the right edge
      if (this._liveCol?.sprite) {
        const spr = this._liveCol.sprite;
        spr.x = Math.floor(R - cellW);
        spr.width = Math.max(1, cellW);
        spr.y = y0;
        spr.height = h0;
        spr.visible = true;
        spr.zIndex = 1e9;
      }

      if (this.gfx.sortableChildren) this.gfx.sortChildren();
    } else {
      // classic column mode: one sprite per candle column
      for (let k = 0; k < active.length; k++) {
        const { col, xCenter } = active[k];
        if (!col?.sprite || xCenter == null) continue;
        if (xCenter < leftPx - cellW) continue;
        if (xCenter > rightPx + cellW) continue;

        const spr = col.sprite;
        spr.x = Math.floor(xCenter - cellW / 2);
        spr.width = cellW;
        spr.y = y0;
        spr.height = h0;
        spr.visible = true;
      }

      if (this._liveCol?.sprite) {
        const spr = this._liveCol.sprite;
        spr.x = Math.floor(rightPx - cellW);
        spr.width = Math.max(1, cellW);
        spr.y = y0;
        spr.height = h0;
        spr.visible = true;
        spr.zIndex = 1e9;
      }
    }
  }
}
