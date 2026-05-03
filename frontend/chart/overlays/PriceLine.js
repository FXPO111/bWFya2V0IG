const PIXI = window.PIXI;// ВЕРХ ФАЙЛА

export class PriceLine {
  constructor(price=null, color=0xffffff, width=1, dashed=true, leftLabelText='') {
    this.price = price;
    this.color = color|0;
    this.width = Math.max(1, Number(width)||1);
    this.dashed = !!dashed;

    this.gfx  = new PIXI.Container();
    this.line = new PIXI.Graphics();
    this.gfx.addChild(this.line);

    this.labelGfx = new PIXI.Container();
    this.bg = new PIXI.Graphics();
    this.label = new PIXI.Text('', { fontFamily:'Arial', fontSize:12, fill:0x000000, resolution:window.devicePixelRatio||2 });
    this.labelGfx.addChild(this.bg); this.labelGfx.addChild(this.label);

    this.leftLabelText = leftLabelText;
    this.leftLabelGfx = new PIXI.Container();
    this.leftBg = new PIXI.Graphics();
    this.leftLabel = new PIXI.Text('', { fontFamily:'Arial', fontSize:12, fill:0xffffff, resolution:window.devicePixelRatio||2 });
    this.leftLabelGfx.addChild(this.leftBg);
    this.leftLabelGfx.addChild(this.leftLabel);
  }
  setPrice(v, isLong){
    if (v == null || isNaN(v)) { this.price = null; return; }
    this.price = +v;
    if (typeof isLong === 'boolean') this.color = isLong ? 0x3fb950 : 0xf85149;
  }
  setColor(c){ this.color = c|0; }
  setWidth(w){ this.width = Math.max(1, Number(w)||1); }
  setDashed(flag){ this.dashed = !!flag; }

  draw(priceScale, _timeScale, opts = {}) {
    this.line.clear();               // <— всегда чистим сначала
    if (this.price == null) {
      // убрать метку с оси тоже
      if (this.labelGfx.parent) this.labelGfx.parent.removeChild(this.labelGfx);
      if (this.leftLabelGfx.parent) this.leftLabelGfx.parent.removeChild(this.leftLabelGfx);
      return;
    }

    // дальше твой код рисования
    const axisWidth = opts.axisWidth ?? 0;
    const fullWidth = opts.fullWidth ?? 0;
    const axisLayer = opts.axisLayer ?? null;

    const y  = Math.round(priceScale.toY(this.price));
    const x0 = 0, x1 = fullWidth - axisWidth;

    if (this.dashed) {
      // пунктир
      const dash=2, gap=1, alpha=0.8, h=this.width, y0=Math.round(y-h/2);
      this.line.beginFill(this.color, alpha);
      for (let x=x0; x<x1; x+=dash+gap) {
        const w = Math.min(dash, x1-x);
        this.line.drawRect(x, y0, w, h);
      }
      this.line.endFill();
    } else {
      const h = this.width, y0 = Math.round(y - h/2);
      this.line.beginFill(this.color, 1);
      this.line.drawRect(x0, y0, x1 - x0, h);
      this.line.endFill();
    }

    if (axisLayer) {
      if (this.labelGfx.parent !== axisLayer) axisLayer.addChild(this.labelGfx);
      const padX=6, padY=3;
      this.label.text = this.price.toFixed(3);
      const tw = this.label.width + padX*2;
      const th = this.label.height + padY*2;
      const rectX = fullWidth - axisWidth + (axisWidth - tw) - 5;
      const rectY = y - Math.round(th/2);
      this.bg.clear();
      this.bg.beginFill(this.color, 1);
      this.bg.drawRect(rectX, rectY, tw, th);
      this.bg.endFill();
      this.label.x = rectX + padX;
      this.label.y = rectY + padY;
    }

    if (axisLayer && this.leftLabelText) {
      if (this.leftLabelGfx.parent !== axisLayer) axisLayer.addChild(this.leftLabelGfx);
      const padX=6, padY=3;
      this.leftLabel.text = this.leftLabelText;
      const tw = this.leftLabel.width + padX*2;
      const th = this.leftLabel.height + padY*2;
      const rectX = 5; // слева от графика
      const rectY = y - Math.round(th/2);
      this.leftBg.clear();
      this.leftBg.beginFill(this.color, 1);
      this.leftBg.drawRect(rectX, rectY, tw, th);
      this.leftBg.endFill();
      this.leftLabel.x = rectX + padX;
      this.leftLabel.y = rectY + padY;
    }
  }
}