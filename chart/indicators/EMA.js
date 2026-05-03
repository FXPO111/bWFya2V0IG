const PIXI = window.PIXI;

/**
 * EMASeries — экспоненциальное скользящее среднее поверх свечей.
 */
export class EMASeries {
  constructor(period = 20, candleSeries = null) {
    this.period = period;
    this.alpha = 2 / (period + 1);
    this.values = [];              // [{ ts, value }]
    this.candleSeries = candleSeries;
    this.gfx = new PIXI.Graphics();
  }

  setData(candles) {
    this.values = [];
    if (!candles?.length) return;

    let ema = +candles[0].close;
    this.values.push({ ts: +candles[0].timestamp, value: ema });
    for (let i = 1; i < candles.length; i++) {
      const close = +candles[i].close;
      ema = close * this.alpha + ema * (1 - this.alpha);
      this.values.push({ ts: +candles[i].timestamp, value: ema });
    }
  }

  update(candle) {
    if (!candle || candle.timestamp == null) return;
    const close = +candle.close;

    if (!this.values.length) {
      this.values.push({ ts: +candle.timestamp, value: close });
      return;
    }

    const last = this.values[this.values.length - 1];

    if (+candle.timestamp === last.ts) {
      // текущая свеча → EMA горизонтально "подтягиваем"
      last.value = last.value; // можно оставить как есть или слегка сглаживать
    } else if (+candle.timestamp > last.ts) {
      // свеча закрылась → добавляем новую EMA-точку
      const ema = close * this.alpha + last.value * (1 - this.alpha);
      this.values.push({ ts: +candle.timestamp, value: ema });
    }
  }


  draw(timeScale, priceScale) {
    this.gfx.clear();
    if (!this.values.length || !this.candleSeries?.bars?.length) return;

    const lastBarTs = this.candleSeries.bars[this.candleSeries.bars.length - 1].ts;

    this.gfx.lineStyle(2, 0xffd700, 1);
    let started = false;

    for (let i = 0; i < this.values.length; i++) {
      const v = this.values[i];
      if (v.ts > lastBarTs) continue; // не рисуем будущее

      const x = Math.round(timeScale.toXFromTs(v.ts, this.candleSeries));
      const y = Math.round(priceScale.toY(v.value));

      if (!started) {
        this.gfx.moveTo(x, y);
        started = true;
      } else {
        this.gfx.lineTo(x, y);
      }
    }
    this.gfx.stroke();
  }
}
