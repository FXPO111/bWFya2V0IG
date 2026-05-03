const PIXI = window.PIXI;

function formatVol(v) {
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + "M";
  if (v >= 1_000) return (v / 1000).toFixed(0) + "K";
  return Math.round(v).toString();
}

function lum(color){
  const r=((color>>16)&255)/255, g=((color>>8)&255)/255, b=(color&255)/255;
  const a=[r,g,b].map(v=>v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4));
  return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2];
}

export class ClusterSeries {
  constructor(candleSeries, cfg = {}) {
    this.candleSeries = candleSeries;

    // ts → Map(price → {buy,sell,__show?})
    this.bars = new Map();

    // сцена
    this.gfx = new PIXI.Container();
    this.cells = new PIXI.Graphics();
    this.labels = new PIXI.Container();
    this.labelBgs = new PIXI.Graphics();
    this.gfx.addChild(this.cells, this.labelBgs, this.labels);

    // пул текстов
    this.textPool = [];

    // конфиг
    this.cfg = Object.assign({
      minUnitForCells: 3,
      minUnitForText: 7,
      fullUnit: 14,
      cellHeight: 12,         // высота клетки, px
      textVolFrac: 0.30,      // порог подписи по доле от maxVol бара
      maxTextsOnScreen: 2500, // общий лимит цифр в кадре
      maxTextsPerBar: 6,      // лимит цифр на один бар (кроме full)
      tick: 0.001,             // шаг цены для квантизации уровня
      fontName: "VolFont",
      fontPx: 14,
      textColorMode: 'auto',     // 'auto' | 'fixed' | 'byDelta'
      textFixedColor: 0xffffff,  // для 'fixed'
      labelBg: true,             // подложка под цифрой
      labelBgAlpha: 0.35,
      labelBgPad: 1,
      cellAlphaBase: 0.25,       // базовая прозрачность клетки
      cellAlphaGain: 0.55        // усиление от интенсивности
    }, cfg || {});

    // BitmapText доступен? Если нет — фолбэк на обычный Text
    this.useBitmap = !!(PIXI.BitmapText && PIXI.BitmapFont && PIXI.BitmapFont.from);
    if (this.useBitmap && (!PIXI.BitmapFont.available || !PIXI.BitmapFont.available[this.cfg.fontName])) {
      PIXI.BitmapFont.from(this.cfg.fontName, {
        fontFamily: "Arial",
        fontSize: this.cfg.fontPx,
        fill: 0xffffff
      });
    }


    this.setStore = (store)=>{ this.bars = store instanceof Map ? store : new Map(); };
    this.getStore = ()=> this.bars;
    this.clear = ()=>{ this.bars.clear(); for (const t of this.textPool) t.visible = false; };

    // опционально: индекс бара, для которого форсить все подписи
    this.forceIndex = null;
  }

  update(trade, tfSec = 1) {
    const bucket = Math.floor(Number(trade.timestamp) / tfSec) * tfSec;
    const ts = bucket;
    let bar = this.bars.get(ts);
    if (!bar) {
      bar = new Map();
      this.bars.set(ts, bar);
    }

    const step = this.cfg.tick || 0.001;
    const price = Math.round(trade.price / step) * step;

    if (!bar.has(price)) bar.set(price, { buy: 0, sell: 0 });

    const rec = bar.get(price);
    const side = trade.side || trade.initiator_side; // совместимость
    if (side === "buy") rec.buy += trade.volume;
    else rec.sell += trade.volume;
  }

  // Полный список уровней бара (для инспектора по наведению)
  levelsForIndex(i) {
    const c = this.candleSeries.bars[i];
    if (!c) return [];
    const m = this.bars.get(c.ts);
    if (!m) return [];
    return [...m.entries()]
      .map(([p, v]) => ({ price: p, buy: v.buy, sell: v.sell, total: v.buy + v.sell, delta: v.buy - v.sell }))
      .sort((a, b) => b.price - a.price);
  }

  draw(timeScale, priceScale) {
   this.cells.clear();
   this.labelBgs.clear();

    const bars = this.candleSeries.bars;
    const n = bars.length;
    if (!n) {
      for (let j = 0; j < this.textPool.length; j++) this.textPool[j].visible = false;
      return;
    }

    const dom = timeScale.domain;
    const d0 = Array.isArray(dom) ? dom[0] : 0;
    const d1 = Array.isArray(dom) ? dom[1] : n - 1;
    const i0 = Math.max(0, Math.floor(d0));
    const i1 = Math.min(n - 1, Math.ceil(d1));

    const cfg = this.cfg;
    let textIndex = 0;

    for (let i = i0; i <= i1; i++) {
      const c = bars[i];
      const levels = this.bars.get(c.ts);
      if (!levels) continue;

      // максимум объёма в баре
      let maxVol = 0;
      for (const [, v] of levels) {
        const t = v.buy + v.sell;
        if (t > maxVol) maxVol = t;
      }
      if (maxVol <= 0) continue;

      // ширина бара
      const x0 = timeScale.toX(i);
      let x1;
      if (i < n - 1) x1 = timeScale.toX(i + 1);
      else if (n > 1) {
        const xm1 = timeScale.toX(i - 1);
        x1 = x0 + (x0 - xm1);
      } else x1 = x0 + 8;

      const unit = Math.max(1, Math.abs(x1 - x0));
      const thinCells = unit < cfg.minUnitForCells;
      const full = unit >= cfg.fullUnit || this.forceIndex === i;
      const allowText = full || unit >= cfg.minUnitForText;

      if (thinCells) continue;

      const w = Math.max(1, Math.floor(unit * 0.9));
      const rectX = x0 - w / 2;
      const h = cfg.cellHeight;

      let shownTexts = 0;

      // стабилен порядок: сверху вниз
      const entries = [...levels.entries()].sort((a, b) => b[0] - a[0]);

      for (const [price, vol] of entries) {
        const total = vol.buy + vol.sell;
        if (total <= 0) continue;

        const y = priceScale.toY(price);
        const rectY = y - h / 2;

        const cellColor = vol.buy >= vol.sell ? 0x00aa00 : 0xaa0000;
        const intensity = total / maxVol;

        // клетка
        const a = cfg.cellAlphaBase + cfg.cellAlphaGain * intensity;
        this.cells.beginFill(cellColor, a);
        this.cells.drawRect(rectX, rectY, w, h);
        this.cells.endFill();

        // решаем, рисовать ли цифры
        const pass = full || total >= cfg.textVolFrac * maxVol;
        const perBarCap = full ? 1e9 : cfg.maxTextsPerBar;

        if (allowText && pass && shownTexts < perBarCap && textIndex < cfg.maxTextsOnScreen) {
          let txt = this.textPool[textIndex];
          if (!txt) {
            txt = this.useBitmap
              ? new PIXI.BitmapText("", { fontName: cfg.fontName })
              : new PIXI.Text("", { fontFamily: "Arial", fontSize: cfg.fontPx, fill: 0xffffff, resolution: window.devicePixelRatio || 2 });
            this.labels.addChild(txt);
            this.textPool.push(txt);
          }

          const s = formatVol(total);
          if (txt.text !== s) txt.text = s;

          // масштаб под клетку
          const targetPx = Math.min(cfg.fontPx, Math.min(w, h) * 0.9);
          const k = targetPx / cfg.fontPx;
          if (txt.scale.x !== k || txt.scale.y !== k) txt.scale.set(k, k);

          // контрастный цвет текста
          let textColor;
          if (cfg.textColorMode === 'fixed') textColor = cfg.textFixedColor;
          else if (cfg.textColorMode === 'byDelta') textColor = cellColor;
          else textColor = lum(cellColor) < 0.4 ? 0xffffff : 0x000000;
          if ('tint' in txt && txt.tint !== textColor) txt.tint = textColor;

          txt.visible = true;
          txt.x = rectX + (w - txt.width) / 2;
          txt.y = rectY + (h - txt.height) / 2;

          // подложка под цифрой
          if (cfg.labelBg) {
            const pad = cfg.labelBgPad|0;
            this.labelBgs.beginFill(0x000000, cfg.labelBgAlpha);
            this.labelBgs.drawRect(txt.x - pad, txt.y - pad, txt.width + 2*pad, txt.height + 2*pad);
            this.labelBgs.endFill();
          }

          textIndex++;
          shownTexts++;
        }
      }
    }

    // скрыть неиспользованные тексты
    for (let j = textIndex; j < this.textPool.length; j++) {
      this.textPool[j].visible = false;
    }
  }
}
