const PIXI = window.PIXI;
import { TimeScale, PriceScale } from './Scales.js';
import { Interaction } from './Interaction.js';
import { PriceLine } from '../overlays/PriceLine.js';
import { SpreadLine } from '../overlays/SpreadLine.js';
import { ClusterSeries } from '../series/ClusterSeries.js';

export class ChartCore {
  constructor(host) {
    this.host = host;
    this.app = new PIXI.Application();
    this.timeScale  = new TimeScale();
    this.priceScale = new PriceScale();

    this.gridLayer   = new PIXI.Container();
    this.seriesLayer = new PIXI.Container();
    this.uiLayer     = new PIXI.Container();
    this.app.stage.addChild(this.gridLayer, this.seriesLayer, this.uiLayer);
    this.uiLayer.sortableChildren = true;

    this.series = [];
    this.autoPrice = true;
    this.visibleBars = 150;
    this.tfSec = 60;

    this.priceLine = new PriceLine(null, 0xffd700, 1);
    this.spreadLine = new SpreadLine();
    this.uiLayer.addChild(this.spreadLine.gfx);
    this.uiLayer.addChild(this.priceLine.gfx);

    this.entryLine = new PriceLine(null, 0x3fb950, 3, true, 'ENTRY');
    this.tpLine    = new PriceLine(null, 0x3fb950, 3, true, 'TP');
    this.slLine    = new PriceLine(null, 0xf85149, 3, true, 'SL');

    this.entryLine.setDashed(false);
    this.tpLine.setDashed(false);
    this.slLine.setDashed(false);

    this.uiLayer.addChild(this.entryLine.gfx);
    this.uiLayer.addChild(this.tpLine.gfx);
    this.uiLayer.addChild(this.slLine.gfx);

    this.axisWidth = 60;
    this.priceAxisLayer = new PIXI.Container();
    this.app.stage.addChild(this.priceAxisLayer);

    this.uiLayer.eventMode = 'static';
    this.uiLayer.cursor = 'default';
    this._dragging = null; // 'tp' | 'sl'
    this._dragStart = { tp: null, sl: null };

    this._rafPending = false;
    this.gridGfx = new PIXI.Graphics();
    this.gridLayer.addChild(this.gridGfx);

    this.axisBg = new PIXI.Graphics();
    this.priceAxisLayer.addChild(this.axisBg);
    this.axisTexts = [];

    this.axisOverlayLayer = new PIXI.Container();
    this.priceAxisLayer.addChild(this.axisOverlayLayer);

    this.priceAxisLayer.sortableChildren = true;
    this.axisBg.zIndex = 0;
    this.axisOverlayLayer.zIndex = 2;

    this.indicatorOverlay = new PIXI.Container();
    this.indicatorOverlay.x = 10;
    this.indicatorOverlay.y = 10;
    this.indicatorOverlay.eventMode = 'static';
    this.indicatorOverlay.zIndex = 1000;

    this.uiLayer.addChild(this.indicatorOverlay);
  }

  requestDraw() {
    if (this._rafPending) return;
    this._rafPending = true;
    requestAnimationFrame(() => {
      this._rafPending = false;
      this.redraw();
    });
  }


  async init() {
    await this.app.init({
      width: this.host.clientWidth || 800,
      height: this.host.clientHeight || 600,
      background: '#0e1117',
      antialias: true,
      resolution: window.devicePixelRatio || 1,
      autoDensity: true,
    });
    this.host.appendChild(this.app.canvas);
    this._resize();
    const makeHitArea = () => {
      const w = this.host.clientWidth;
      const h = this.host.clientHeight;
      this.uiLayer.hitArea = new PIXI.Rectangle(0, 0, w, h);
    };
    makeHitArea();
    window.addEventListener('resize', makeHitArea);

    window.addEventListener('resize', () => this._resize());
    this.interaction = new Interaction(this);

    // --- Drag & Drop для TP/SL линий ---
    const near = (y, line) => {
      if (!line || line.price == null) return false;
      const ly = this.priceScale.toY(line.price);
      return Math.abs(y - ly) <= 6; // зона захвата в пикселях
    };

    // pointerdown: начали тянуть TP или SL
    this.uiLayer.on('pointerdown', (e) => {
      const p = e.data.getLocalPosition(this.uiLayer);
      if (near(p.y, this.tpLine)) {
        this._dragging = 'tp';
        this._dragStart = { tp: this.tpLine.price, sl: this.slLine?.price };
        this.uiLayer.cursor = 'ns-resize';
        this._autoWas = this.autoPrice; this.autoPrice = false;
      } else if (near(p.y, this.slLine)) {
        this._dragging = 'sl';
        this._dragStart = { tp: this.tpLine?.price, sl: this.slLine.price };
        this.uiLayer.cursor = 'ns-resize';
        this._autoWas = this.autoPrice; this.autoPrice = false;
      }
    });

    // pointermove: если тянем — двигаем линию, иначе меняем курсор по наведению
    this.uiLayer.on('pointermove', (e) => {
      const p = e.data.getLocalPosition(this.uiLayer);
      if (!this._dragging) {
        if (near(p.y, this.tpLine) || near(p.y, this.slLine)) {
          this.uiLayer.cursor = 'ns-resize';
        } else {
          this.uiLayer.cursor = 'default';
        }
        return;
      }

      const price = this.priceScale.fromY(p.y);
      if (this._dragging === 'tp' && this.tpLine) this.tpLine.setPrice(price, true);
      if (this._dragging === 'sl' && this.slLine) this.slLine.setPrice(price, false);

      const id = this._dragging === 'tp' ? 'tp-input' : 'sl-input';
      const el = document.getElementById(id);
      if (el) el.value = price.toFixed(2);

      this.requestDraw();
    });

    window.hasPosition ??= false;
    window.pendingTpsl ??= null;

    // pointerup: сохраняем результат и шлём на сервер
    const finishDrag = () => {
      if (!this._dragging) return;
      const tpEl = document.getElementById('tp-input');
      const slEl = document.getElementById('sl-input');

      // сохраняем в localStorage
      try {
        const tp = this.tpLine?.price ?? null;
        const sl = this.slLine?.price ?? null;
        try { localStorage.setItem('tpsl', JSON.stringify({ tp, sl })); } catch {}
        const tick = 0.001; // шаг тика инструмента
        const roundTo = (p) => p == null ? null : Math.round(p / tick) * tick;
        const payload = { tp: roundTo(tp), sl: roundTo(sl), trigger_by: 'last' };
        // применяем нормализованные значения
        if (this.tpLine) this.tpLine.setPrice(payload.tp, true);
        if (this.slLine) this.slLine.setPrice(payload.sl, false);
        if (tpEl && payload.tp != null) tpEl.value = payload.tp.toFixed(2);
        if (slEl && payload.sl != null) slEl.value = payload.sl.toFixed(2);
        // отправка
        if (window.socket && window.socket.connected) {
          window.socket.emit('set_conditionals', payload);
        }
        window.pendingTpsl = payload; // всегда сохраняем локально
        window.pendingTpsl_inflight = true;
      } finally {
        if (this._autoWas !== undefined) { this.autoPrice = this._autoWas; this._autoWas = undefined; }
        this._dragging = null;
        this.uiLayer.cursor = 'default';
        this.requestDraw();
      }
    }
    this.uiLayer.on('pointerup', finishDrag);
    this.uiLayer.on('pointerupoutside', finishDrag);
  }

  addSeries(series) {
    this.series.push(series);
    this.seriesLayer.addChild(series.gfx);
  }

  setTimeframe(tfSec) {
    this.tfSec = Math.max(1, tfSec|0);
    // Heatmap uses a fixed price window; autoscale causes domain jumps/desync.
    this.autoPrice = !this.heatmapEnabled;
  }

  _resize() {
    const w = this.host.clientWidth || 800;
    const h = this.host.clientHeight || 600;
    this.app.renderer.resize(w, h);

    this.indicatorOverlay.hitArea = new PIXI.Rectangle(0, 0, w, h);

    // график занимает всё, кроме зоны оси справа
    this.timeScale.setRange([0, w - this.axisWidth]);
    this.priceScale.setRange([0, h]);
    this.requestDraw();
  }

  _recalcDomains() {
    let ex = null;

    // When heatmap is enabled we must not auto-scale the price domain.
    // Otherwise the scale periodically jumps and the heatmap history appears to "slide".
    const auto =
      this.autoPrice &&
      !this.heatmapEnabled &&
      !this.priceUserControlled;


    if (this.series.length) {
      const main = this.series[0];
      const n = main.count();
      if (n) {
        const first = 0, last = n - 1;
        this.timeScale.setDataExtent(first, last);

        const [i0, i1] = this.timeScale.domain;
        ex = main.getExtentInRange(Math.floor(i0), Math.ceil(i1));
        if (ex) {
          this.priceScale.dataMin = ex.p0;
          this.priceScale.dataMax = ex.p1;
        }
        if (ex && auto) {
          const pad = Math.max(0.005, (ex.p1 - ex.p0) * 0.08);
          this.priceScale.setDomain([ex.p0 - pad, ex.p1 + pad]);
        }
      }
    }

    // гарантируем видимость линии цены
    if (auto && !this._dragging) {
      const anchors = [];
      if (this.priceLine?.price != null) anchors.push(this.priceLine.price);
      if (this.entryLine?.price != null) anchors.push(this.entryLine.price);
      if (this.tpLine?.price != null)    anchors.push(this.tpLine.price);
      if (this.slLine?.price != null)    anchors.push(this.slLine.price);

      if (anchors.length) {
        const [d0, d1] = this.priceScale.domain;
        const minA = Math.min(...anchors), maxA = Math.max(...anchors);
        let low = Math.min(d0, minA), high = Math.max(d1, maxA);
        const span = Math.max(1e-9, high - low);
        const pad = Math.max(0.005, span * 0.08);
        this.priceScale.setDomain([low - pad, high + pad]);
      }
    }
  }

  _roundStep(rawStep) {
    if (!isFinite(rawStep) || rawStep <= 0) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(rawStep)));
    const norm = rawStep / pow;
    let step;
    if (norm < 1.5) step = 1;
    else if (norm < 3) step = 2;
    else if (norm < 7) step = 5;
    else step = 10;
    return step * pow;
  }


  redraw() {
    this._recalcDomains();

    if (this._tapeDirty && typeof this._tapeReseed === 'function') {
      this._tapeDirty = false;
      this._tapeReseed();
    }

    // очищаем слой оси цен
    const w = this.host.clientWidth;
    const h = this.host.clientHeight;

    const [low, high] = this.priceScale.domain;
    const stepPx = 40;
    const stepVal = this._roundStep((high - low) / Math.max(1, (h / stepPx)));

    // фон оси — один Graphics, без новых объектов
    this.axisBg.clear();
    this.axisBg.beginFill(0x0e1117);
    this.axisBg.drawRect(w - this.axisWidth, 0, this.axisWidth, h);
    this.axisBg.endFill();

    // горизонтальная сетка — один Graphics
    this.gridGfx.clear();
    this.gridGfx.lineStyle(1, 0x333333, 0.6);

    let used = 0;
    for (let v = Math.ceil(low / stepVal) * stepVal; v <= high; v += stepVal) {
      const y = this.priceScale.toY(v);

    // линия сетки до границы оси
    this.gridGfx.moveTo(0, y);
    this.gridGfx.lineTo(w - this.axisWidth, y);

    // метка цены из пула
    let t = this.axisTexts[used];
    if (!t) {
      t = new PIXI.Text('', {
        fontFamily: 'Arial',
        fontSize: 12,
        fill: 0xffffff,
        resolution: window.devicePixelRatio || 2
      });
      t.roundPixels = true;
      this.priceAxisLayer.addChild(t);
      this.axisTexts[used] = t;
    }
    const label = v.toFixed(3);
    if (t.text !== label) t.text = label;
    t.visible = true;
    t.x = w - this.axisWidth + (this.axisWidth - t.width) - 11;
    t.y = y - t.height / 2;

    used++;
  }

  // скрыть лишние подписи, чтобы не удалять
  for (let i = used; i < this.axisTexts.length; i++) {
    const t = this.axisTexts[i];
    if (t) t.visible = false;
  }


    this.priceLine?.setWidth?.(1);
    if (this.spreadLine?.setThickness) this.spreadLine.setThickness(this.priceLine.width);
    this.entryLine?.setWidth?.(3);
    this.tpLine?.setWidth?.(3);
    this.slLine?.setWidth?.(3);

    // серии
    for (const s of this.series) s.draw(this.timeScale, this.priceScale);
    if (this.priceLine) {
      this.priceLine.draw(this.priceScale, this.timeScale, { axisWidth: this.axisWidth, fullWidth: w, axisLayer: this.axisOverlayLayer });
    }

    if (this.spreadLine) {
      this.spreadLine.draw(this.priceScale, this.timeScale, this.axisWidth); // SpreadLine сам рисует 1px
    }

    if (this.entryLine) this.entryLine.draw(this.priceScale, this.timeScale, { axisWidth: this.axisWidth, fullWidth: w, axisLayer: this.axisOverlayLayer });
    if (this.tpLine)    this.tpLine.draw(this.priceScale, this.timeScale,    { axisWidth: this.axisWidth, fullWidth: w, axisLayer: this.axisOverlayLayer });
    if (this.slLine)    this.slLine.draw(this.priceScale, this.timeScale,    { axisWidth: this.axisWidth, fullWidth: w, axisLayer: this.axisOverlayLayer });

  }

  resetView() {
    if (!this.series.length) return;
    const main = this.series[0];
    const n = main.count();
    if (!n) return;

    const first = 0;
    const last  = n - 1;

    this.timeScale.setDataExtent(first, last);

    const d1 = last;
    const d0 = Math.max(first - this.timeScale.leftOffset, d1 - this.visibleBars);
    this.timeScale.setDomain([d0, d1 + this.timeScale.rightOffset]);

    // Heatmap: autoscale OFF, domain is treated as user-controlled to prevent jumps.
    this.autoPrice = !this.heatmapEnabled;

    if (this.heatmapEnabled) {
      this.priceScale.userControlled = true;
      this.priceUserControlled = true;
    } else {
      this.priceScale.userControlled = false;
      this.priceUserControlled = false;
    }

    this.timeScale.userControlled = true;
    this.requestDraw();
  }
}