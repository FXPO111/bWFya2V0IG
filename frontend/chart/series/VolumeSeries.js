const PIXI = window.PIXI;

/**
 * VolumeSeries — колонки объёмов.
 * Цвет совпадает с направлением свечи (зелёный/красный).
 */
export class VolumeSeries {
  constructor(candleSeries) {
    this.candleSeries = candleSeries; // чтобы знать up/down по свечам
    this.vols = [];

    this.gfx = new PIXI.Graphics();
  }

  setData(candles) {
    this.vols = (candles || [])
      .filter(c => c.timestamp != null && c.volume != null)
      .map(c => ({
        ts: +c.timestamp,
        volume: +c.volume,
        open: +c.open,
        close:+c.close
      }))
      .sort((a,b)=>a.ts-b.ts);
  }

  update(c) {
    if (!c || c.timestamp == null) return;
    const rec = { ts:+c.timestamp, volume:+c.volume, open:+c.open, close:+c.close };
    const n = this.vols.length;
    if (!n) { this.vols.push(rec); return; }
    const last = this.vols[n-1];
    if (last.ts === rec.ts) Object.assign(last, rec);
    else if (rec.ts > last.ts) this.vols.push(rec);
  }

  count(){ return this.vols.length; }

  draw(timeScale, priceScale){
    this.gfx.clear();
    const n = this.vols.length;
    if (!n) return;

    const [d0,d1] = timeScale.domain;
    const i0 = Math.max(0, Math.floor(d0));
    const i1 = Math.min(n-1, Math.ceil(d1));

    const unit = Math.abs(timeScale.toX(Math.min(i0+1,n-1)) - timeScale.toX(i0)) || 1;
    const w = Math.max(2, Math.floor(unit*0.7));
    const half = Math.floor(w/2);

    // нормировка высоты объёмов по максимуму в видимой области
    let maxV = 0;
    for (let i=i0;i<=i1;i++){ if (this.vols[i].volume>maxV) maxV=this.vols[i].volume; }
    if (!maxV) return;

    const h = priceScale.range[1]; // низ канвы (px)

    for (let i=i0;i<=i1;i++){
      const v = this.vols[i];
      const x = Math.round(timeScale.toX(i));
      const barH = Math.max(1, Math.round((v.volume / maxV) * 50)); // высота до 50px
      const y = h - barH;

      const up = v.close >= v.open;
      const fill = up ? 0x3fb950 : 0xf85149;

      this.gfx.beginFill(fill, 0.6);
      this.gfx.drawRect(x - half, y, w, barH);
      this.gfx.endFill();
    }
  }
}
