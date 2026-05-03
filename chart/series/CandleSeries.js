const PIXI = window.PIXI;

/**
 * CandleSeries — классическая свечная серия.
 * - получает готовые бары от сервера (в выбранном TF);
 * - рисует хвосты (виксы high-low) и тело (open-close);
 * - зелёные свечи = close >= open, красные = close < open.
 */
export class CandleSeries {
  constructor() {
    this.bars = [];

    this.gfx    = new PIXI.Container();
    this.wicks  = new PIXI.Graphics();  // сначала тени
    this.bodies = new PIXI.Graphics();  // потом тела поверх
    this.gfx.addChild(this.wicks);
    this.gfx.addChild(this.bodies);
  }

  setData(candles) {
    this.bars = (candles || [])
      .filter(c => c.timestamp != null && !isNaN(+c.timestamp))
      .map(c => ({
        ts: +c.timestamp,
        open: +c.open,
        high: +c.high,
        low:  +c.low,
        close:+c.close
      }))
      .sort((a,b)=>a.ts-b.ts);
  }

  update(c) {
    if (!c || c.timestamp == null || isNaN(+c.timestamp)) return;
    const rec = { ts:+c.timestamp, open:+c.open, high:+c.high, low:+c.low, close:+c.close };
    const n = this.bars.length;
    if (!n) { this.bars.push(rec); return; }
    const last = this.bars[n-1];
    if (last.ts === rec.ts) Object.assign(last, rec);
    else if (rec.ts > last.ts) this.bars.push(rec);
  }

  count(){ return this.bars.length; }

  getExtent(){
    if (!this.bars.length) return null;
    let p0=Infinity,p1=-Infinity;
    for (const b of this.bars){ if (b.low<p0) p0=b.low; if (b.high>p1) p1=b.high; }
    return {t0:0,t1:this.bars.length-1,p0,p1};
  }

  getExtentInRange(i0,i1){
    if (!this.bars.length) return null;
    i0=Math.max(0,i0|0); i1=Math.min(this.bars.length-1,i1|0);
    if (i1<i0) return null;
    let p0=Infinity,p1=-Infinity;
    for (let i=i0;i<=i1;i++){ const b=this.bars[i]; if (b.low<p0) p0=b.low; if (b.high>p1) p1=b.high; }
    return {p0,p1};
  }

  draw(timeScale, priceScale){
    this.wicks.clear();
    this.bodies.clear();
    const n = this.bars.length;
    if (!n) return;

    const [d0,d1] = timeScale.domain;
    const i0 = Math.max(0, Math.floor(d0));
    const i1 = Math.min(n-1, Math.ceil(d1));

    const unit = Math.abs(timeScale.toX(Math.min(i0+1,n-1)) - timeScale.toX(i0)) || 1;
    const spacing = 0.2; // оставим 20% зазора
    const w = Math.max(1, Math.floor(unit * (1 - spacing)));

    const half = Math.max(1, Math.floor(w/2));
    const wickWidth = 1;

    for (let i=i0;i<=i1;i++){
      const c = this.bars[i];
      const x = Math.round(timeScale.toX(i));
      const yO = Math.round(priceScale.toY(c.open));
      const yC = Math.round(priceScale.toY(c.close));
      const yH = Math.round(priceScale.toY(c.high));
      const yL = Math.round(priceScale.toY(c.low));

      const up = c.close >= c.open;
      const fill = up ? 0x3fb950 : 0xf85149;
      const wickColor = fill;

      // верхняя тень
      const topWickH = Math.min(yO, yC) - yH;
      if (topWickH > 0) {
        this.wicks.beginFill(wickColor, 1);
        this.wicks.drawRect(x - Math.floor(wickWidth/2), yH, wickWidth, topWickH);
        this.wicks.endFill();
      }

      // нижняя тень
      const botWickH = yL - Math.max(yO, yC);
      if (botWickH > 0) {
        this.wicks.beginFill(wickColor, 1);
        this.wicks.drawRect(x - Math.floor(wickWidth/2), Math.max(yO, yC), wickWidth, botWickH);
        this.wicks.endFill();
      }

      // тело (минимум 1 пиксель высоты)
      const top = Math.min(yO, yC);
      const h   = Math.max(1, Math.abs(yC - yO));
      this.bodies.beginFill(fill, 1);
      this.bodies.drawRect(x - half, top, w, h);
      this.bodies.endFill();
    }
  }
}
