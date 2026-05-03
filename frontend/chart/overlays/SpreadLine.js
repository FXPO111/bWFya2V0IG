const PIXI = window.PIXI;

export class SpreadLine {
  constructor(price = null, color = 0xc9d1d9, thickness = 1) {
    this.price = price;
    this.color = color;
    this.thickness = thickness;

    this.gfx = new PIXI.Graphics();
  }

  setPrice(price, color = 0xc9d1d9) {
    if (price == null || isNaN(price)) return;
    this.price = price;
    this.color = color;
  }

  draw(priceScale, timeScale) {
    if (this.price == null || isNaN(this.price)) return;

    const y = Math.round(priceScale.toY(this.price));
    const [x0, x1] = timeScale.range;

    const dash = 2, gap = 1;
    const t = Math.max(1, this.thickness | 0);
    const y0 = Math.round(y - t / 2);

    this.gfx.clear();
    this.gfx.beginFill(this.color, 0.8);
    for (let x = x0; x < x1; x += dash + gap) {
      const w = Math.min(dash, x1 - x);
      this.gfx.drawRect(x, y0, w, t);
    }
    this.gfx.endFill();
  }
}
