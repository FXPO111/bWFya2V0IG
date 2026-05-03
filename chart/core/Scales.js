export class TimeScale {
  constructor() {
    this.range = [0, 1];
    this.domain = [0, 100];

    this.minSpan = 1;
    this.maxSpan = 900;

    this.leftOffset  = 50;
    this.rightOffset = 50;

    this.firstIndex = 0;
    this.lastIndex  = 0;

    this.userControlled = false;
  }

  setDomain(d) { this.domain = d; }
  setRange(r)  { this.range  = r; }

  setDataExtent(first, last) {
    this.firstIndex = first;
    this.lastIndex  = last;
  }

  _bounds() {
    return [this.firstIndex - this.leftOffset, this.lastIndex + this.rightOffset];
  }

  toX(t) {
    const [d0, d1] = this.domain;
    const [r0, r1] = this.range;
    return r0 + (t - d0) / Math.max(1e-9, d1 - d0) * (r1 - r0);
  }

  fromX(x) {
    const [d0, d1] = this.domain;
    const [r0, r1] = this.range;
    return d0 + (x - r0) / Math.max(1e-9, r1 - r0) * (d1 - d0);
  }

  clampDomain(d0, d1) {
    let span = d1 - d0;

    // ограничение по масштабу
    span = Math.max(this.minSpan, Math.min(span, this.maxSpan));

    // минимально допустимое смещение (левая граница)
    const leftLimit  = this.firstIndex - (span - 1);
    // максимально допустимое смещение (правая граница)
    const rightLimit = this.lastIndex  + (span - 1);

    // влево
    if (d0 < leftLimit) {
      d0 = leftLimit;
      d1 = d0 + span;
    }

    // вправо
    if (d1 > rightLimit) {
      d1 = rightLimit;
      d0 = d1 - span;
    }

    this.domain = [d0, d1];
  }

  scroll(dxPx) {
    const spanD = this.domain[1] - this.domain[0];   // сколько баров помещается сейчас
    const spanR = this.range[1] - this.range[0];     // сколько пикселей ширина канвы
    if (spanR <= 0) return;

    // сколько баров соответствует одному пикселю
    const barsPerPx = spanD / spanR;
    const k = 0.7;
    const shift = dxPx * barsPerPx * k;

    let d0 = this.domain[0] - shift;
    let d1 = this.domain[1] - shift;

    this.clampDomain(d0, d1);
    this.userControlled = true;
  }

  zoom(factor, centerPx) {
    const c = this.fromX(centerPx);
    const [d0, d1] = this.domain;

    let left  = c - (c - d0) / factor;
    let right = c + (d1 - c) / factor;
    let span  = right - left;

    if (span < this.minSpan) {
      const k = this.minSpan / span;
      left  = c - (c - left) * k;
      right = c + (right - c) * k;
      span  = this.minSpan;
    }
    if (span > this.maxSpan) {
      const k = this.maxSpan / span;
      left  = c - (c - left) * k;
      right = c + (right - c) * k;
      span  = this.maxSpan;
    }

    this.clampDomain(left, right);
    this.userControlled = true;
  }

  // timestamp → X (для EMA)
  toXFromTs(ts, candleSeries) {
    const bars = candleSeries?.bars;
    if (!bars || !bars.length) return 0;

    if (ts <= bars[0].ts) return this.toX(0);
    if (ts >= bars[bars.length - 1].ts) return this.toX(bars.length - 1);

    // бинарный поиск по времени → дробный индекс
    let lo = 0, hi = bars.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (bars[mid].ts < ts) lo = mid + 1; else hi = mid;
    }
    const i = lo;
    const a = bars[i - 1].ts, b = bars[i].ts;
    const frac = (ts - a) / Math.max(1e-9, b - a);
    const t = (i - 1) + Math.max(0, Math.min(1, frac));

    return this.toX(t);
  }
}

export class PriceScale {
  constructor() {
    this.range  = [0, 1];
    this.domain = [0, 1];
    this.minSpan = 0.0001;
    this.maxSpan = 1000;

    this.softMargin = 2;
    this.dataMin = null;
    this.dataMax = null;
  }

  setDomain(d){ this.domain = d; }
  setRange(r) { this.range  = r; }

  toY(p) {
    const [d0, d1] = this.domain;
    const [r0, r1] = this.range;
    return r1 - (p - d0) / Math.max(1e-9, d1 - d0) * (r1 - r0);
  }

  fromY(y) {
    const [d0, d1] = this.domain;
    const [r0, r1] = this.range;
    return d0 + (r1 - y) / Math.max(1e-9, r1 - r0) * (d1 - d0);
  }

  scroll(dyPx) {
    const spanR = this.range[1] - this.range[0];
    if (spanR <= 0) return;

    const pricePerPx = (this.domain[1] - this.domain[0]) / spanR;
    const shift = -dyPx * pricePerPx;

    let d0 = this.domain[0] + shift;
    let d1 = this.domain[1] + shift;

    this.clampDomain(d0, d1);
    this.userControlled = true;
  }

  zoom(factor, centerPx) {
    const c = this.fromY(centerPx);
    const [d0, d1] = this.domain;

    let low  = c - (c - d0) / factor;
    let high = c + (d1 - c) / factor;
    let span = high - low;

    if (span < this.minSpan) {
      const k = this.minSpan / span;
      low  = c - (c - low) * k;
      high = c + (high - c) * k;
      span = this.minSpan;
    }
    if (span > this.maxSpan) {
      const k = this.maxSpan / span;
      low  = c - (c - low) * k;
      high = c + (high - c) * k;
      span = this.maxSpan;
    }

    this.clampDomain(low, high);
    this.userControlled = true;
  }

  clampDomain(d0, d1) {
    let span = d1 - d0;

    // ограничение по масштабу
    span = Math.max(this.minSpan, Math.min(span, this.maxSpan));

    if (!this.userControlled && this.dataMin != null && this.dataMax != null) {
      const margin = span * this.softMargin;
      const minAllowed = this.dataMin - margin;
      const maxAllowed = this.dataMax + margin;

      if (d0 < minAllowed) {
        d0 = minAllowed;
        d1 = d0 + span;
      }
      if (d1 > maxAllowed) {
        d1 = maxAllowed;
        d0 = d1 - span;
      }
    }

    this.domain = [d0, d1];
  }
}
