export class Interaction {
  constructor(chart) {
    this.chart = chart;
    this.view = chart.app.canvas;

    this.dragging = false;
    this.mode = null; // 'time' | 'priceZoom' | 'pan'
    this.lastX = 0;
    this.lastY = 0;
    this.crosshair = null;

    this._bind();
  }

  _inPriceAxis(mx) {
    const AXIS_W = this.chart.axisWidth || 60;
    return mx > this.view.width - AXIS_W;
  }

  _bind() {
    // === Drag start ===
    const nearLine = (y, line, chart) =>
      line && line.price != null && Math.abs(chart.priceScale.toY(line.price) - y) <= 6;

    this.view.addEventListener("mousedown", (e) => {
      const rect = this.view.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;

      // если жмём по TP/SL — не включаем панорамирование/зум
      if (nearLine(my, this.chart.tpLine, this.chart) || nearLine(my, this.chart.slLine, this.chart)) {
        this.dragging = false;
        this.mode = null;
        return;
      }

      if (this._inPriceAxis(mx)) {
        this.mode = "priceZoom";
      } else {
        this.mode = "pan"; // свободное перемещение
      }

      this.dragging = true;
      this.lastX = e.clientX;
      this.lastY = e.clientY;
      this.view.style.cursor = "grabbing";

      this.chart.autoPrice = false;
      this.chart.priceUserControlled = true;
      this.chart.priceScale.userControlled = true;
    });

    // === Drag end ===
    window.addEventListener("mouseup", () => {
      this.dragging = false;
      this.mode = null;
      this.view.style.cursor = "default";
    });

    // === Drag move ===
    window.addEventListener("mousemove", (e) => {
      if (this.dragging) {
        if (this.chart._dragging) return;
        if (this.mode === "pan") {
          // свободное перемещение по X и Y
          const dx = e.clientX - this.lastX;
          const dy = e.clientY - this.lastY;
          this.lastX = e.clientX;
          this.lastY = e.clientY;

          this.chart.timeScale.scroll(dx);
          this.chart.priceScale.scroll(-dy);
          this.chart.priceUserControlled = true;
          this.chart.priceScale.userControlled = true;
          this.chart.requestDraw();
        } else if (this.mode === "priceZoom") {
          // вертикальный зум
          const dy = e.clientY - this.lastY;
          this.lastY = e.clientY;

          const factor = Math.pow(1.0018, -dy);
          const centerY = this.view.height / 2; // как в TV всегда от центра
          this.chart.priceScale.zoom(factor, centerY);
          this.chart.requestDraw();
        }
      }

      this._crosshair(e);
    });

    // === Wheel zoom ===
    this.view.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        if (this.chart._dragging) return;
        const rect = this.view.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        const factor = e.deltaY < 0 ? 1.2 : 0.8;

        if (this._inPriceAxis(mx)) {
          // колесо над осью цен → вертикальный зум
          const centerY = this.view.height / 2;
          this.chart.priceScale.zoom(factor, centerY);
          this.chart.priceUserControlled = true;
          this.chart.priceScale.userControlled = true;
        } else {
          // колесо над графиком → горизонтальный зум
          this.chart.timeScale.zoom(factor, mx);
        }

        this.chart.autoPrice = false;
        this.chart.priceUserControlled = true;
        this.chart.requestDraw();
      },
      { passive: false }
    );
  }

  _crosshair(e) {
    if (!this.chart || !this.chart.series.length) return;
    const rect = this.view.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    // если курсор вне канвы → скрыть перекрестье
    if (mx < 0 || my < 0 || mx > this.view.width || my > this.view.height) {
      if (this.crosshair) {
        this.crosshair.clear();
      }
    if (this.priceLabel) this.priceLabel.visible = false;
      return;
    }

    if (!this.crosshair) {
      this.crosshair = new PIXI.Graphics();
      this.chart.uiLayer.addChild(this.crosshair);
    }

    const w = this.view.width;
    const h = this.view.height;

    this.crosshair.clear();
    this.crosshair.lineStyle(1, 0x555555, 0.8);
    const idx = Math.round(this.chart.timeScale.fromX(mx));
    const snapX = this.chart.timeScale.toX(idx);
    this.crosshair.moveTo(snapX, 0);
    this.crosshair.lineTo(snapX, h);

    this.crosshair.moveTo(0, my);
    this.crosshair.lineTo(w, my);
    this.crosshair.stroke();

    // === метка цены справа ===
    const axisW = this.chart.axisWidth || 60;
    const fullW = this.view.width;
    const price = this.chart.priceScale.fromY(my);

    if (!this.priceLabel) {
      this.priceLabel = new PIXI.Container();
      this.priceBg = new PIXI.Graphics();
      this.priceText = new PIXI.Text('', {
        fontFamily: 'Arial',
        fontSize: 12,
        fill: 0xffffff,
        resolution: window.devicePixelRatio || 2
      });
      this.priceLabel.addChild(this.priceBg);
      this.priceLabel.addChild(this.priceText);
      this.chart.axisOverlayLayer.addChild(this.priceLabel);
    }
    else if (this.priceLabel.parent !== this.chart.axisOverlayLayer) {
      this.chart.axisOverlayLayer.addChild(this.priceLabel);
    }
    this.priceLabel.visible = true;

    const txt = price.toFixed(3);
    this.priceText.text = txt;

    const padX = 6, padY = 3;
    const tw = this.priceText.width + padX * 2;
    const th = this.priceText.height + padY * 2;
    const rectX = fullW - axisW + (axisW - tw) - 5;
    const rectY = my - Math.round(th / 2);

    this.priceBg.clear();
    this.priceBg.beginFill(0x444444, 1);
    this.priceBg.drawRect(rectX, rectY, tw, th);
    this.priceBg.endFill();

    this.priceText.x = rectX + padX;
    this.priceText.y = rectY + padY;
    if (this.priceLabel.parent === this.chart.axisOverlayLayer) {
      this.chart.axisOverlayLayer.setChildIndex(this.priceLabel, this.chart.axisOverlayLayer.children.length - 1);
    }
  }
}