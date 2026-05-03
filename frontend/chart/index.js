import { ChartCore }    from './core/ChartCore.js';
import { CandleSeries } from './series/CandleSeries.js';
import { updateOrderbook, addTrade } from '../scripts.js';
import { VolumeSeries } from './series/VolumeSeries.js';
import { EMASeries } from './indicators/EMA.js';
import { ClusterSeries } from './series/ClusterSeries.js';
import { HeatmapSeries } from './series/HeatmapSeries.js';
import { MidSpreadSeries } from './series/MidSpreadSeries.js';

function median(arr){
  if (!arr.length) return 0;
  const a = arr.slice().sort((x,y)=>x-y);
  const m = Math.floor(a.length/2);
  return a.length%2 ? a[m] : 0.5*(a[m-1]+a[m]);
}

function inferTfSec(candles){
  if (!Array.isArray(candles) || candles.length < 2) return 0;
  const diffs=[];
  for (let i=1;i<candles.length;i++){
    const dt = Number(candles[i].timestamp) - Number(candles[i-1].timestamp);
    if (dt>0) diffs.push(dt);
  }
  const step = median(diffs);
  return Math.round(step);
}

document.addEventListener('DOMContentLoaded', async () => {
  const host = document.getElementById('price-chart');
  const chart = new ChartCore(host);

  await chart.init();

  await PIXI.Assets.load(['/images/eye.png', '/images/eye-off.png']);
  const eyeTex = PIXI.Texture.from('/images/eye.png');
  const eyeOffTex = PIXI.Texture.from('/images/eye-off.png');
  window.iconTextures = { eye: eyeTex, eyeOff: eyeOffTex };

  window.chart = chart;

  window.indicators = [];

  function registerIndicator(id, name, series, order) {
    const row = new PIXI.Container();

    const eye = new PIXI.Sprite(window.iconTextures.eye);
    eye.width = 16;
    eye.height = 16;
    eye.x = 0;
    eye.y = 0;
    eye.eventMode = 'static';
    eye.cursor = 'pointer';

    const label = new PIXI.Text(name, { fontFamily:'Arial', fontSize:13, fill:0xffffff });
    label.x = 24;
    label.y = 0;

    // восстановить сохранённое состояние (если есть)
    const saved = localStorage.getItem(`indicators_state_${id}`);
    if (saved !== null) {
      const visible = JSON.parse(saved);
      series.gfx.visible = visible;
      eye.texture = visible ? window.iconTextures.eye : window.iconTextures.eyeOff;
      label.style.fill = visible ? 0xffffff : 0x777777;
      label.style.fontStyle = visible ? 'normal' : 'italic';
    }

    eye.on('pointerdown', (e) => {
      e.stopPropagation();
      series.gfx.visible = !series.gfx.visible;

      // обновить глаз
      eye.texture = series.gfx.visible ? window.iconTextures.eye : window.iconTextures.eyeOff;

      // обновить стиль текста
      label.style.fill = series.gfx.visible ? 0xffffff : 0x777777;
      label.style.fontStyle = series.gfx.visible ? 'normal' : 'italic';

      // сохранить состояние
      localStorage.setItem(`indicators_state_${id}`, JSON.stringify(series.gfx.visible));

      // событие наружу
      window.dispatchEvent(new CustomEvent('indicator_toggle', {
        detail: { id, visible: series.gfx.visible }
      }));

      window.chart.requestDraw();
    });

    row.addChild(eye);
    row.addChild(label);

    row.x = 8;
    row.y = 8 + order * 22;

    chart.indicatorOverlay.addChild(row);
    window.indicators.push({ id, name, series, eye, label });
  }

  const series = new CandleSeries();
  chart.addSeries(series);

  const volSeries = new VolumeSeries(series);
  chart.addSeries(volSeries);

  const emaSeries = new EMASeries(24, series);
  chart.addSeries(emaSeries);

  const clusterSeries = new ClusterSeries(series);
  chart.addSeries(clusterSeries);

  const heatmapSeries = new HeatmapSeries({
    candleSeries: series,
    tfSec: chart.tfSec,

    levelsPerSide: 100,
    tickSize: 0.001,
    maxColumns: 3500,

    // тусклый фон
    alpha: 0.48,
    minAlphaCut: 0.08,
    percentile: 0.995,
    emaAlphaRef: 0.05,
    useLog: true,

    bandsToRight: true,
    historyMode: 'curr',

    // пороги как ты требовал
    historyColdThreshold: 1000,
    historyHotThreshold: 10000,

    // live-колонка ещё слабее
    liveAlpha: 0.18,
    liveMinAlphaCut: 0.06,
    livePercentile: 0.99,
    liveEmaAlphaRef: 0.20,
    liveColdThreshold: 2000,
    liveHotThreshold: 10000,

    // меньше “шума” от снапшотов
    snapshotMaxHz: 30,

    centerCoreEnabled: true,
    centerCoreAlphaMul: 1.22, // чуть ярче центра
    centerCoreFrac: 0.18,
  });

  heatmapSeries.gfx.visible = false;
  chart.addSeries(heatmapSeries);
  chart.seriesLayer.setChildIndex(heatmapSeries.gfx, 0);

  const midSpreadSeries = new MidSpreadSeries({
    candleSeries: series,
    tickSize: 0.001,

    midWidth: 1,
    midAlpha: 0.90,

    bandAlpha: 0.12,
  });

  midSpreadSeries.gfx.visible = false;
  chart.addSeries(midSpreadSeries);
  chart.seriesLayer.setChildIndex(midSpreadSeries.gfx, chart.seriesLayer.children.length - 1);


  registerIndicator('volume', 'Volume', volSeries, 0);
  registerIndicator('ema24', 'EMA 24', emaSeries, 1);
  registerIndicator('clusters', 'Clusters', clusterSeries, 2);
  registerIndicator('heatmap', 'Heatmap', heatmapSeries, 3);

  chart.heatmapEnabled = heatmapSeries.gfx.visible;

  applyTapeMode();

  function applyTapeMode() {
    const on = (chart.tfSec === 1) && !!chart.heatmapEnabled;

    midSpreadSeries.gfx.visible = on;

    // свечи: в tape-режиме всегда скрыты
    if (on) {
      midSpreadSeries.seedFromCandles();
      series.gfx.visible = false;
    } else {
      // стандарт: свечи скрываются только когда включены clusters
      series.gfx.visible = !clusterSeries.gfx.visible;
    }

    chart.requestDraw();
  }


  window.addEventListener('indicator_toggle', (e) => {
    if (e.detail.id === 'clusters') {
      if (!(chart.tfSec === 1 && chart.heatmapEnabled)) {
        series.gfx.visible = !e.detail.visible;
      }
    }
    if (e.detail.id === 'heatmap') {
      chart.heatmapEnabled = e.detail.visible;
      heatmapSeries.setLockAnchor(chart.heatmapEnabled);

      chart.autoPrice = !chart.heatmapEnabled; // heatmap => autoscale OFF

      // В heatmap-режиме домен должен быть фиксированным вокруг anchor/mid,
      // иначе MidSpread tape "исчезает" (рисуется вне экрана).
      if (chart.heatmapEnabled) {
        const ref = midPrice ?? getRefPrice();
        if (ref != null && Number.isFinite(ref)) {
          const pd = heatmapSeries.heatmapSuggestedDomain(ref);
          if (pd) chart.priceScale.setDomain(pd);
        }

        chart.autoPrice = false;
        chart.priceScale.userControlled = true;
        chart.priceUserControlled = true;

        chart._heatmapDomainInit = true;
      } else {
        // выключили heatmap — возвращаем обычное поведение
        chart._heatmapDomainInit = false;
      }

      applyTapeMode();
      chart.requestDraw();
    }
  });

  let bestBid = null, bestAsk = null, midPrice = null;
  const clusterStores = new Map();
  let boot = true;
  let scheduled = false;
  let lastUp = true;

  let currentCandle = null; // живая свеча (тело = mid, тени = last)

  function getRefPrice() {
    if (midPrice != null && Number.isFinite(midPrice)) return midPrice;
    if (currentCandle?.close != null && Number.isFinite(+currentCandle.close)) return +currentCandle.close;

    const bars = series.bars || [];
    if (bars.length) {
      const last = bars[bars.length - 1];
      const c = last?.close ?? last?.c;
      if (c != null && Number.isFinite(+c)) return +c;
    }
    return null;
  }

  function findBarIndexByTs(bars, ts) {
    // Ищем с конца, потому что нужный бар почти всегда последний/рядом с ним
    for (let i = bars.length - 1; i >= 0; i--) {
      const bts = bars[i].ts ?? bars[i].timestamp;
      if (+bts === +ts) return i;
      if (+bts < +ts) break;
    }
    return bars.length - 1;
  }

  const scheduleDraw = () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; chart.requestDraw(); });
  };

  async function fetchCandles(tfSec) {
    try {
      const res = await fetch(`/candles?interval=${tfSec}`);
      const data = await res.json();

      const clean = data.filter(c =>
        c && c.timestamp != null &&
        c.open != null && c.high != null &&
        c.low != null && c.close != null
      );

      if (clean.length) {
        series.setData(clean.map(c => ({
          timestamp: +c.timestamp,
          open: +c.open,
          high: +c.high,
          low:  +c.low,
          close:+c.close
        })));
        chart.resetView();

        volSeries.setData(clean.map(c => ({
          timestamp: +c.timestamp,
          volume: +c.volume,
          open: +c.open,
          close:+c.close
        })));

        emaSeries.setData(clean);

        const last = clean[clean.length-1];
        currentCandle = {
          timestamp: +last.timestamp,
          open: last.open,
          high: last.high,
          low:  last.low,
          close:last.close,
          volume: +last.volume || 0
        };
      }
    } catch (err) {
      console.error("Ошибка загрузки свечей:", err);
    }
  }

  document.querySelectorAll('#tf-selector button').forEach(btn => {
    btn.addEventListener('click', async () => {
      const tf = Number(btn.dataset.tf);
      if (!tf) return;

      // если таймфрейм уже выбран — ничего не делаем
      if (tf === chart.tfSec) return;

      // подсветка
      document.querySelectorAll('#tf-selector button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      // сохранить текущие кластера под старым TF
      clusterStores.set(chart.tfSec, clusterSeries.getStore());

      chart.setTimeframe(tf);
      heatmapSeries.setTfSec(chart.tfSec);
      heatmapSeries.clear();

      series.setData([]);
      midSpreadSeries.clear();
      volSeries.setData([]);
      currentCandle = null;
      boot = true;
      chart.redraw();
      await fetchCandles(chart.tfSec);
      midSpreadSeries.seedFromCandles();
      applyTapeMode();

      // восстановить стор кластера для нового TF (если был)
      const restored = clusterStores.get(chart.tfSec);
      if (restored) clusterSeries.setStore(restored);
      else clusterSeries.setStore(new Map());

      // вычислить диапазон по загруженным свечам
      const bars = series.bars || [];
      if (bars.length) {
        const first = bars[0].ts;
        const last  = bars[bars.length - 1].ts + chart.tfSec; // включительно
        await fetchTrades(chart.tfSec, first, last);
      } else {
        await fetchTrades(chart.tfSec); // дефолт: последний час на сервере
      }
    });
  });

  // первичная загрузка данных
  await fetchCandles(chart.tfSec);
  midSpreadSeries.seedFromCandles();
  {
    const bars = series.bars || [];
    if (bars.length) {
      const first = bars[0].ts;
      const last  = bars[bars.length - 1].ts + chart.tfSec;
      await fetchTrades(chart.tfSec, first, last);
    } else {
      await fetchTrades(chart.tfSec);
    }
  }

  const socket = io("http://localhost:8000");
  window.socket = socket;

  socket.on("orderbook_update", (book) => {
    if (!book?.bids?.length || !book?.asks?.length) return;
    updateOrderbook(book.bids, book.asks);

    bestBid = parseFloat(book.bids[0].price);
    bestAsk = parseFloat(book.asks[0].price);
    if (isNaN(bestBid) || isNaN(bestAsk)) return;

    midPrice = (bestBid + bestAsk) / 2;
    heatmapSeries.updateOrderbook(book.bids, book.asks, midPrice);

    // === HEATMAP PRICE DOMAIN: set ONCE, then NEVER touch ===
    if (chart.heatmapEnabled && !chart._heatmapDomainInit) {
      const pd = heatmapSeries.heatmapSuggestedDomain(midPrice);
      if (pd) chart.priceScale.setDomain(pd);

      chart.autoPrice = false;
      chart.priceScale.userControlled = true;
      chart.priceUserControlled = true;

      chart._heatmapDomainInit = true;
    }



    if (chart.heatmapEnabled) {
      const tsNow = Date.now() / 1000; // секунды
      heatmapSeries.snapshotTick(tsNow, midPrice);
    }

    if (chart.heatmapEnabled && !chart._heatmapDomainInit) {
      const pd = heatmapSeries.heatmapSuggestedDomain(midPrice);
      if (pd) chart.priceScale.setDomain(pd);

      chart.autoPrice = false;
      chart.priceScale.userControlled = true;
      chart.priceUserControlled = true;

      chart._heatmapDomainInit = true;
    }

    const now = Math.floor(Date.now() / 1000);
    const bucket = Math.floor(now / chart.tfSec) * chart.tfSec;
    const isNewCandle = (!currentCandle || currentCandle.timestamp !== bucket);

    if (!currentCandle || currentCandle.timestamp !== bucket) {
      // новая свеча
      const prevClose = series.bars.length ? series.bars[series.bars.length-1].close : midPrice;
      const isUp = midPrice >= prevClose;
      currentCandle = {
        timestamp: bucket,
        open: midPrice,
        high: midPrice,
        low:  midPrice,
        close: isUp ? midPrice + 1e-8 : midPrice - 1e-8, // микросдвиг для тела
        volume: 0
      };
      lastUp = isUp;
    } else {
      currentCandle.high  = Math.max(currentCandle.high, midPrice);
      currentCandle.low   = Math.min(currentCandle.low,  midPrice);
      currentCandle.close = midPrice;
      lastUp = currentCandle.close >= currentCandle.open;
    }

    series.update(currentCandle);

    if (chart.tfSec === 1 && chart.heatmapEnabled) {
      midSpreadSeries.updateLast(currentCandle.timestamp, bestBid, bestAsk, midPrice, lastUp);
    }
    // freeze heatmap column ровно 1 раз на новую свечу
    if (isNewCandle && chart.heatmapEnabled) {
      const barIndex = findBarIndexByTs(series.bars, currentCandle.timestamp);


    }
    volSeries.update(currentCandle);
    emaSeries.update(currentCandle);

    chart.priceLine.setPrice(midPrice, lastUp);
    chart.spreadLine.setPrice(lastUp ? bestAsk : bestBid,
                              lastUp ? 0xf85149 : 0x3fb950);

    scheduleDraw();
  });

  socket.on("candles", (incomingCandles) => {
    if (!Array.isArray(incomingCandles) || !incomingCandles.length) return;

    const tfIn = inferTfSec(incomingCandles);
    const tf = chart.tfSec;
    if (Math.abs(tfIn - tf) > Math.max(1, Math.round(tf*0.1))) return;

    if (boot) {
      series.setData(incomingCandles);
      midSpreadSeries.seedFromCandles();
      applyTapeMode();
      volSeries.setData(incomingCandles);
      emaSeries.setData(incomingCandles);
      chart.resetView();
      boot = false;

      const last = incomingCandles[incomingCandles.length-1];
      currentCandle = {
        timestamp: +last.timestamp,
        open: last.open,
        high: last.high,
        low:  last.low,
        close:last.close,
        volume: +last.volume || 0
      };
    } else {
      incomingCandles.forEach(c => {
        series.update(c);
        volSeries.update(c);
        emaSeries.update(c);
      });
      scheduleDraw();
    }
  });

  socket.on("trade", (trade) => {
    addTrade(trade);
    if (!currentCandle) return;

    currentCandle.volume = (currentCandle.volume || 0) + (+trade.volume || 0);

    // нормализуем структуру для ClusterSeries
    const norm = {
      timestamp: currentCandle.timestamp,
      price: +trade.price,
      volume: +trade.volume,
      side: trade.initiator_side   // всегда есть в сервере
    };

    clusterSeries.update(norm, chart.tfSec);

    series.update(currentCandle);
    volSeries.update(currentCandle);
    emaSeries.update(currentCandle);
    scheduleDraw();
  });

  async function fetchTrades(tfSec, fromTs, toTs) {
    try {
      const params = new URLSearchParams();
      if (fromTs) params.set('from', String(fromTs));
      if (toTs)   params.set('to',   String(toTs));
      params.set('limit', '50000');
      const res = await fetch(`/api/trades?${params.toString()}`);
      if (!res.ok) { console.warn('HTTP', res.status); return; } // защита

      const trades = await res.json();
      for (const t of trades) {
        clusterSeries.update({
          timestamp: +t.timestamp,
          price: +t.price,
          volume: +t.volume,
          side: t.side || t.initiator_side
        }, tfSec);
      }
      window.chart?.requestDraw();
    } catch (err) {
      console.error("Ошибка загрузки трейдов:", err);
    }
  }
  chart.redraw();

  // Подсветить активный tf при загрузке
  const currentBtn = document.querySelector(`#tf-selector button[data-tf="${chart.tfSec}"]`);
  if (currentBtn) currentBtn.classList.add('active');

  // ENTRY по позиции
  socket.on('positions_update', (pos) => {
    const was = window.hasPosition;
    const qty = Math.abs(Number(pos?.position_qty) || 0);
    window.hasPosition = qty > 0;

    // позиция появилась → отправляем заранее выставленные TP/SL
    if (!was && window.hasPosition) {
      if (window.pendingTpsl && window.socket?.connected) {
        window.socket.emit('set_conditionals', window.pendingTpsl);
        window.pendingTpsl_inflight = true;
      }
      // позиция открылась, просто ждём ack
    }


    // qty==0: два случая
    if (!qty) {
      if (was) {
        // 1) ПОСЛЕ закрытия позиции → убрать всё
        chart.entryLine?.setPrice(null);
        chart.tpLine?.setPrice(null);
        chart.slLine?.setPrice(null);
        window.pendingTpsl = null;
        window.pendingTpsl_inflight = false;
        // TP/SL уберутся через conditional_update
        const tpUI = document.getElementById('tp-input');
        const slUI = document.getElementById('sl-input');
        if (tpUI) tpUI.value = '';
        if (slUI) slUI.value = '';
      } else {
        // 2) ДО открытия позиции → сохраняем pending
        chart.entryLine?.setPrice(null);
        chart.requestDraw();
        return;
      }
      chart.requestDraw();
      return;
    }
    const isLong = Number(pos.position_qty) > 0;
    window.lastIsLong = isLong;
    chart.entryLine.setDashed(false);
    chart.entryLine.setWidth(3);
    chart.entryLine.setPrice(Number(pos.entry_price || 0), isLong);
    chart.requestDraw();
  });

  // TP/SL из условных ордеров
  socket.on('conditional_update', (conds) => {

    // до открытия позиции не трогаем локальные линии
    if (!window.hasPosition) {
      chart.requestDraw?.();
      return;
    }

    const hasServerConds = Array.isArray(conds) && conds.length > 0;
    const inflight = !!window.pendingTpsl_inflight;
    // если мы только что отправили TP/SL — игнорируем пустой апдейт
    if (inflight && !hasServerConds) return;

    // пока позиции нет — сохраняем локальные линии
    if (!window.hasPosition && window.pendingTpsl && !hasServerConds) return;
    // позиция уже есть, мы только что отправили TP/SL — ждём подтверждение
    if (inflight && !hasServerConds) return;

    const mine = Array.isArray(conds)
      ? conds.filter(c => c && c.reduce_only)
      : [];
    const tp = mine.find(c => c.type === 'tp');
    const sl = mine.find(c => c.type === 'sl');

    // во время inflight принимаем только точное совпадение с pending
    if (inflight) {
      if (!hasServerConds) return;
      const eq = (a,b) => (a == null && b == null) ||
                          (a != null && b != null && Math.abs(Number(a) - Number(b)) < 1e-8);
      const okTp = eq(tp?.trigger, window.pendingTpsl?.tp);
      const okSl = eq(sl?.trigger, window.pendingTpsl?.sl);
      if (!(okTp && okSl)) return; // это старые/чужие conds → игнор
    }

    if (chart.tpLine && tp) {
      chart.tpLine.setDashed(false);
      chart.tpLine.setWidth(3);
      chart.tpLine.setPrice(Number(tp.trigger), true);
    }
    if (chart.slLine && sl) {
      chart.slLine.setDashed(false);
      chart.slLine.setWidth(3);
      chart.slLine.setPrice(Number(sl.trigger), false);
    }

    // подтверждаем только если сервер прислал ровно наши триггеры
    if (hasServerConds && window.pendingTpsl) {
      const eq = (a,b) => (a == null && b == null) ||
                          (a != null && b != null && Math.abs(Number(a) - Number(b)) < 1e-8);
      const okTp = eq(tp?.trigger, window.pendingTpsl.tp);
      const okSl = eq(sl?.trigger, window.pendingTpsl.sl);
      if (okTp && okSl) {
        window.pendingTpsl = null;
        window.pendingTpsl_inflight = false;
      } else if (inflight) {
        // получили несоответствующие conds во время ожидания — не перерисовываем локальные линии
        return;
      }
    }
    chart.requestDraw();
  });
});
