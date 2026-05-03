// series/MidSpreadSeries.js
const PIXI = window.PIXI;

function isNum(v) { return Number.isFinite(v); }

export class MidSpreadSeries {
  constructor(opts = {}) {
    this._candleSeries = opts.candleSeries;
    this.tickSize = Number(opts.tickSize ?? 0.001);

    // MID line style
    this.midColor = (opts.midColor ?? 0xffffff) >>> 0;
    this.midAlpha = Number(opts.midAlpha ?? 0.90);
    this.midWidth = Number(opts.midWidth ?? 1);

    // Spread-side line: ask if up, bid if down
    this.spreadColor = (opts.spreadColor ?? 0xffffff) >>> 0;
    this.spreadAlpha = Number(opts.spreadAlpha ?? 0.55);
    this.spreadWidth = Number(opts.spreadWidth ?? 1);

    this.gfx = new PIXI.Graphics();
    this.gfx.roundPixels = true;

    // чтобы линия не выглядела “толстой/криповой”
    this.cap = opts.cap ?? 'butt';
    this.join = opts.join ?? 'miter';
    this.alignment = Number(opts.alignment ?? 0.5);

    this._mid = [];
    this._bid = [];
    this._ask = [];
    this._spr = [];

    // fallback: рисуем “последнюю цену” даже когда домен справа/слева от данных
    this._lastMid = NaN;
    this._lastSpr = NaN;
  }

  count() {
    const bars = this._candleSeries?.bars;
    return bars?.length ? bars.length : 0;
  }

  clear() {
    this._mid.length = 0;
    this._bid.length = 0;
    this._ask.length = 0;
    this._spr.length = 0;
    this._lastMid = NaN;
    this._lastSpr = NaN;
    this.gfx.clear();
  }

  seedFromCandles() {
    const bars = this._candleSeries?.bars || [];
    const n = bars.length;
    if (!n) return;

    const half = this.tickSize * 0.5;
    this._mid.length = n;
    this._bid.length = n;
    this._ask.length = n;
    this._spr.length = n;

    for (let i = 0; i < n; i++) {
      const c = Number(bars[i]?.close);
      if (!isNum(c)) continue;
      this._mid[i] = c;
      this._bid[i] = c - half;
      this._ask[i] = c + half;
      this._spr[i] = c;
    }

    const last = n - 1;
    if (isNum(this._mid[last])) this._lastMid = this._mid[last];
    if (isNum(this._spr[last])) this._lastSpr = this._spr[last];
  }

  // update LAST bar (bucketTs не нужен, оставлен для совместимости)
  updateLast(bucketTs, bid, ask, mid, isUp) {
    const bars = this._candleSeries?.bars || [];
    const n = bars.length;
    if (!n) return;

    const i = n - 1;
    if (this._mid.length !== n) {
      this._mid.length = n;
      this._bid.length = n;
      this._ask.length = n;
      this._spr.length = n;
    }

    const b = Number(bid), a = Number(ask), m = Number(mid);

    if (isNum(m)) this._mid[i] = m;
    if (isNum(b)) this._bid[i] = b;
    if (isNum(a)) this._ask[i] = a;

    const up = !!isUp;
    let spr = NaN;
    if (isNum(b) && isNum(a)) spr = up ? a : b;
    else if (isNum(m)) spr = m;

    if (isNum(m)) this._lastMid = m;
    if (isNum(spr)) this._lastSpr = spr;

    if (isNum(spr)) this._spr[i] = spr;
    else if (isNum(m)) this._spr[i] = m;
  }

  draw(timeScale, priceScale) {
    const bars = this._candleSeries?.bars || [];
    const n = bars.length;
    if (!n) { this.gfx.clear(); return; }

    const [d0, d1] = timeScale.domain;

    // ВАЖНО: НЕ clamp к [0..n-1] — иначе при правом паддинге линия исчезает
    const i0 = Math.floor(d0);
    const i1 = Math.ceil(d1);

    if (this._mid.length !== n) {
      this._mid.length = n;
      this._bid.length = n;
      this._ask.length = n;
      this._spr.length = n;
    }

    const gfx = this.gfx;
    gfx.clear();

    // crisp 1px
    const snap1 = (v) => (Math.round(v) + 0.5);

    const applyStroke = (st) => {
      const width = Number(st?.width ?? 1);
      const color = (st?.color ?? 0xffffff) >>> 0;
      const alpha = Number(st?.alpha ?? 1);
      const alignment = Number(st?.alignment ?? 0.5);

      // PIXI v8+
      if (typeof gfx.setStrokeStyle === 'function') {
        gfx.setStrokeStyle({
          width,
          color,
          alpha,
          alignment,
          cap: st?.cap ?? 'butt',
          join: st?.join ?? 'miter',
          miterLimit: st?.miterLimit ?? 2,
        });
        return;
      }

      // PIXI v6/v7
      if (typeof gfx.lineStyle === 'function') {
        gfx.lineStyle(width, color, alpha, alignment);
      }
    };

const drawStep = (arr, lastFallback, style) => {
  applyStroke(style);

      // ищем первый валидный индекс ВНУТРИ данных
      let k = Math.max(0, i0);
      const kEnd = Math.min(n - 1, i1);

      while (k <= kEnd && !isNum(arr[k])) k++;

      // если в окне нет валидных точек, но есть lastFallback — рисуем горизонталь на весь видимый X
      if (k > kEnd) {
        if (!isNum(lastFallback)) { if (typeof gfx.stroke === 'function') gfx.stroke(); return; }

        const xA = snap1(timeScale.toX(i0));
        const xB = snap1(timeScale.toX(i1));
        const y  = snap1(priceScale.toY(lastFallback));
        gfx.moveTo(xA, y);
        gfx.lineTo(xB, y);
        if (typeof gfx.stroke === 'function') gfx.stroke();
        return;
      }

      // стартуем с найденного k
      let x0 = snap1(timeScale.toX(k));
      let y0 = snap1(priceScale.toY(arr[k]));
      gfx.moveTo(x0, y0);

      // рисуем ступеньками пока есть данные
      for (let i = k; i < kEnd; i++) {
        const v0 = arr[i];
        const v1 = arr[i + 1];
        if (!isNum(v0)) continue;

        const x1 = snap1(timeScale.toX(i + 1));
        const yy0 = snap1(priceScale.toY(v0));
        gfx.lineTo(x1, yy0);     // горизонталь
        if (isNum(v1)) {
          const yy1 = snap1(priceScale.toY(v1));
          gfx.lineTo(x1, yy1);   // вертикальный шаг
        }
      }

      // если домен уезжает вправо — дотягиваем последним значением до i1
      const lastIdx = Math.min(n - 1, kEnd);
      const vLast = isNum(arr[lastIdx]) ? arr[lastIdx] : lastFallback;
      if (isNum(vLast) && i1 > lastIdx) {
        const xB = snap1(timeScale.toX(i1));
        const yB = snap1(priceScale.toY(vLast));
        gfx.lineTo(xB, yB);
      }

      if (typeof gfx.stroke === 'function') gfx.stroke();
    };

    drawStep(this._mid, this._lastMid, {
      width: this.midWidth,
      color: this.midColor,
      alpha: this.midAlpha,
      alignment: this.alignment,
      cap: this.cap,
      join: this.join,
      miterLimit: 2,
    });

    drawStep(this._spr, this._lastSpr, {
      width: this.spreadWidth,
      color: this.spreadColor,
      alpha: this.spreadAlpha,
      alignment: this.alignment,
      cap: this.cap,
      join: this.join,
      miterLimit: 2,
    });
  }
}
