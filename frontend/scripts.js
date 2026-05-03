const chartContainer = document.getElementById('price-chart');
const bidsTbody = document.querySelector('#bids-table tbody');
const asksTbody = document.querySelector('#asks-table tbody');
const tradesList = document.getElementById('trades-list');

let socket = null;

// === фазы ===
let showPhases = false;
let marketPhases = [];

// ползунок
let lastMid = null;
let accAvailable = 0;

let tpPriceLine = null, slPriceLine = null;
let tpPrice = null, slPrice = null;
let entryPriceLine = null
let lastPosition = null;

let overlayEl = null;
const TP_OFFSET_PCT = 0.01;  // +1% от mid
const SL_OFFSET_PCT = 0.01;  // -1% от mid

let previewLockUntil = 0;

let serverLeverage = null;
let serverMaxNotional = null;
let serverMode = null;

let currentClosePos = null;

let lastQtyFromSlider = null;

let lastCandle = null;
let candleTimer = null;

// ликвидация
const EPS = 1e-6;
const LIQ_MAX_DISTANCE_PCT = 0.40;

// Кнопка фаз
const toggleBtn = document.getElementById('toggle-phase-btn');
if (toggleBtn) {
  toggleBtn.addEventListener('click', function() {
    showPhases = !showPhases;
    this.textContent = showPhases ? 'Скрыть фазы рынка' : 'Показать фазы рынка';
    renderPhasesOverlay();
  });
}

function calculateSMA(candles, length = 20) {
    if (!candles || candles.length === 0) return [];
    const values = [];
    for (let i = 0; i < candles.length; i++) {
        if (i < length - 1) continue;
        let sum = 0;
        for (let j = 0; j < length; j++) {
            sum += candles[i - j].close;
        }
        const avg = sum / length;
        values.push({ time: candles[i].timestamp, value: avg });
    }
    return values;
}

function ensureChartOverlay() {
  if (overlayEl) return overlayEl;
  const container = chartContainer;
  if (!container) return null;
  if (getComputedStyle(container).position === 'static') {
    container.style.position = 'relative';
  }
  overlayEl = document.createElement('div');
  overlayEl.className = 'chart-overlay';
  Object.assign(overlayEl.style, {
    position: 'absolute',
    left: '0', top: '0', right: '0', bottom: '0',
    zIndex: '6',                 // поверх канваса
    pointerEvents: 'none'        // чтобы ловить мышь
  });
  container.appendChild(overlayEl);
  return overlayEl;
}



// ==== ФАЗЫ РЫНКА: загрузка с сервера и overlay ====

function fetchPhases() {
    fetch('/market_phases')
        .then(res => res.json())
        .then(data => {
            marketPhases = data;
            renderPhasesOverlay();
        });
}
fetchPhases();
setInterval(fetchPhases, 3000);

function getPhaseColor(phase, microphase) {
    if (phase === 'panic') return '#cb2424';
    if (phase === 'trend_up') return '#19bc65';
    if (phase === 'trend_down') return '#f8a627';
    if (phase === 'flat') {
        if (microphase === 'flat_squeeze') return '#d4b01e';
        if (microphase === 'flat_microtrend_up' || microphase === 'flat_microtrend_down') return '#3d71fc';
        return '#334c77';
    }
    if (phase === 'volatile') return '#9452f7';
    return '#2e3d4f';
}

// Обработка ресайза окна
window.addEventListener('resize', () => {
    
});

// === Перетаскивание/скролл графика ===
let isDragging = false;
let dragStartX = 0;

if (chartContainer) {
  chartContainer.style.cursor = 'grab';
  chartContainer.addEventListener('mousedown', e => {
    isDragging = true;
    dragStartX = e.clientX;
    chartContainer.style.cursor = 'grabbing';
    e.preventDefault();
  });
}

window.addEventListener('mouseup', () => {
    isDragging = false;
    if (chartContainer) chartContainer.style.cursor = 'grab';
});

window.addEventListener('mousemove', e => {
    if (!isDragging || !chartContainer) return;
    const dx = dragStartX - e.clientX;
    dragStartX = e.clientX;
});


// ==== Анимация полей объёма в стакане ====
function animateWidth(element, targetWidthPercent, duration = 400) {
    let start = null;
    if (!element.parentElement) return;
    const parentWidth = element.parentElement.clientWidth;
    let initialWidth = 0;
    try {
        initialWidth = parseFloat(getComputedStyle(element).width) / parentWidth * 100 || 0;
    } catch {
        initialWidth = 0;
    }

    function step(timestamp) {
        if (!start) start = timestamp;
        const progress = Math.min((timestamp - start) / duration, 1);
        const currentWidth = initialWidth + (targetWidthPercent - initialWidth) * progress;
        element.style.width = `${currentWidth}%`;
        if (progress < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
}

function updateOrderbook(bids, asks) {
    if (!asks.length || !bids.length) return;

    const asksContainer = document.getElementById('asks-container');
    const bidsContainer = document.getElementById('bids-container');
    const midDiv = document.getElementById('mid-price');

    asksContainer.innerHTML = '';
    bidsContainer.innerHTML = '';

    const bestAsk = asks.length ? asks[0].price : null;
    const bestBid = bids.length ? bids[0].price : null;

    if (bestAsk !== null && bestBid !== null) {
        const newMid = (bestAsk + bestBid) / 2;
        const prevMid = parseFloat(midDiv.dataset.prevMid || 0);
        midDiv.dataset.prevMid = newMid.toFixed(3);
        midDiv.textContent = newMid.toFixed(3);

        lastMid = newMid;

        if (newMid > prevMid) midDiv.style.color = '#3fb950';
        else if (newMid < prevMid) midDiv.style.color = '#f85149';
        else midDiv.style.color = '#c9d1d9';

        // === СПРЕД-ЛИНИЯ ===
        let spreadColor = '#c9d1d9';
        let spreadLinePrice = null;

        if (lastCandle) {
          if (lastCandle.close >= lastCandle.open) {
            spreadColor = '#f85149';   // свеча зелёная → спред красный
            spreadLinePrice = bestAsk; // ближайшее предложение
          } else {
            spreadColor = '#3fb950';   // свеча красная → спред зелёный
            spreadLinePrice = bestBid; // ближайший спрос
          }
        }

    } else {
        midDiv.textContent = '—';
        midDiv.style.color = '#8b949e';
    }



    const MAX_LEVELS = 13;

    // Берём только то, что реально показываем
    const askView = asks.slice(0, MAX_LEVELS);   // лучшие аски (asks[0] = bestAsk)
    const bidView = bids.slice(0, MAX_LEVELS);   // лучшие биды (bids[0] = bestBid)

    // Нормализацию тоже считаем по отображаемым уровням, а не по всем 200
    const maxAskVol = Math.max(...askView.map(a => Number(a.volume) || 0), 1);
    const maxBidVol = Math.max(...bidView.map(b => Number(b.volume) || 0), 1);

    // Ask: обычно рисуют так, чтобы ближний к mid был ближе к середине стакана,
    // поэтому реверсим отображаемую “лучшие->дальше” в “дальше->лучшие”.
    askView.slice().reverse().forEach(({ price, volume, agent_id, source }) => {
      const aid = agent_id ?? source; // сервер у тебя шлёт "source" :contentReference[oaicite:2]{index=2}

      const row = document.createElement('div');
      row.className = 'orderbook-row sell';

      const priceDiv = document.createElement('div');
      priceDiv.className = 'price';
      priceDiv.textContent = Number(price).toFixed(3);

      const volumeDiv = document.createElement('div');
      volumeDiv.className = 'volume';
      volumeDiv.textContent = Number(volume).toFixed(3);

      const line = document.createElement('div');
      line.className = 'volume-line ask';

      if (aid && String(aid).startsWith('advcore')) row.classList.add('advcore-order');
      if (aid && String(aid).includes('advmm')) row.classList.add('advmm-order');

      line.style.width = `${(Number(volume) / maxAskVol) * 100}%`;

      row.appendChild(priceDiv);
      row.appendChild(volumeDiv);
      row.appendChild(line);
      asksContainer.appendChild(row);
    });

    // Bid: bids уже best-first, оставляем как есть
    bidView.forEach(({ price, volume, agent_id, source }) => {
      const aid = agent_id ?? source;

      const row = document.createElement('div');
      row.className = 'orderbook-row buy';

      const priceDiv = document.createElement('div');
      priceDiv.className = 'price';
      priceDiv.textContent = Number(price).toFixed(3);

      const volumeDiv = document.createElement('div');
      volumeDiv.className = 'volume';
      volumeDiv.textContent = Number(volume).toFixed(3);

      const line = document.createElement('div');
      line.className = 'volume-line bid';

      if (aid && String(aid).includes('advmm')) row.classList.add('advmm-order');

      line.style.width = `${(Number(volume) / maxBidVol) * 100}%`;

      row.appendChild(priceDiv);
      row.appendChild(volumeDiv);
      row.appendChild(line);
      bidsContainer.appendChild(row);
    });
}

// ==== Терминал ====

// === TP/SL и лента: управление состоянием ===
function currentTapeLimit() {
  const tp = document.getElementById('enable-tpsl');
  return tp && tp.checked ? 7 : 10;  // при включённом TP/SL показываем меньше
}

function enforceTapeLimit() {
  const list = document.getElementById('trades-list');
  const limit = currentTapeLimit();
  while (list.children.length > limit) list.removeChild(list.lastChild);
}

document.addEventListener('DOMContentLoaded', () => {
  const tpSlCheckbox = document.getElementById('enable-tpsl');
  const tpSlBlock = document.getElementById('tpsl-block');
  const sidePanel = document.getElementById('side-panel');
  const qtyInput = document.getElementById('order-qty');
  const priceInput = document.getElementById('order-price');
  const leverageSelect = document.getElementById('leverage');
  const availableSpan = document.getElementById('available');
  const costSpan = document.getElementById('cost');
  const maxSpan = document.getElementById('max');
  const liqSpan = document.getElementById('liq');
  const feeSpan = document.getElementById('fee');

  // сохраняем последние данные
let lastCalc = { cost: 0, fee: 0, max: 0, liq: "--" };

function recalc() {
  const qtyRaw = parseFloat(qtyInput.value);
  const qty = isNaN(qtyRaw) ? (lastQtyFromSlider || 0) : qtyRaw;

  const mid = parseFloat(document.getElementById('mid-price').textContent);
  const price = parseFloat(priceInput.value) || (isNaN(mid) ? 0 : mid);

  // серверное плечо и лимит нотации
  const uiLev = parseInt(leverageSelect.value.replace('x','')) || 1;
  const lev   = serverLeverage || uiLev;

  const available = (() => {
    const txt = (document.getElementById('available').textContent || '').replace(' USDT','');
    return Number(txt) || 0;
  })();

  const maxNotional = (serverMaxNotional != null) ? serverMaxNotional : (available * lev);

  if (!qty || !price) {
    costSpan.textContent = (0).toFixed(2);
    feeSpan.textContent  = (0).toFixed(2);
    maxSpan.textContent  = maxNotional.toFixed(2);
    liqSpan.textContent  = "--";
    return;
  }

  const notional = qty * price;
  const cost = notional / lev;
  const fee  = notional * 0.0004; // taker по умолчанию

  costSpan.textContent = cost.toFixed(2);
  feeSpan.textContent  = fee.toFixed(2);
  maxSpan.textContent  = maxNotional.toFixed(2);

  // ликвидация тут не считаем — её отдаёт сервер; оставим прочерк
  liqSpan.textContent  = "--";

  const buyBtn  = document.querySelector('.actions .buy');
  const sellBtn = document.querySelector('.actions .sell');

  if (lastPosition && lastPosition.entry_price) {
    if (lastPosition.position_qty > 0 && tpPrice && tpPrice <= lastPosition.entry_price) {
      if (sellBtn) sellBtn.disabled = true;
      if (buyBtn) buyBtn.disabled = false;
    } else if (lastPosition.position_qty < 0 && tpPrice && tpPrice >= lastPosition.entry_price) {
      if (buyBtn) buyBtn.disabled = true;
      if (sellBtn) sellBtn.disabled = false;
    } else {
      if (buyBtn) buyBtn.disabled = false;
      if (sellBtn) sellBtn.disabled = false;
    }
  }

}

// слушатели — СНАРУЖИ recalc()
qtyInput.addEventListener('input', recalc);
priceInput.addEventListener('input', recalc);
leverageSelect.addEventListener('change', () => {
  const lev = parseInt(leverageSelect.value.replace('x','')) || 1;
  if (window.socket && socket.connected) socket.emit('set_account', { leverage: lev });
  recalc();
});

// TP/SL видимость
if (tpSlCheckbox && tpSlBlock) {
  const applyTpSlState = () => {
    const checked = tpSlCheckbox.checked;
    tpSlBlock.classList.toggle('hidden', !checked);
    if (sidePanel) sidePanel.classList.toggle('compact', checked);
    enforceTapeLimit();
    if (!checked && window.chart) {
      window.chart.tpLine?.setPrice(null);
      window.chart.slLine?.setPrice(null);
      window.chart.redraw?.();
    }

  };
  tpSlCheckbox.addEventListener('change', applyTpSlState);
  applyTpSlState();
  // кнопки-плюсики TP/SL
  const tpRow = document.getElementById('tp-row');
  const slRow = document.getElementById('sl-row');

  if (tpRow) {
    const btn = tpRow.querySelector('.add-tp');
    if (btn) btn.addEventListener('click', () => {
      tpRow.innerHTML = `
        <label for="tp-input">TP</label>
        <input id="tp-input" type="number">
        <select>
          <option>Маркир.</option>
          <option>Послед.</option>
        </select>`;
      const initTP = lastMid ? lastMid * (1 + TP_OFFSET_PCT) : null;
      if (initTP && window.chart?.tpLine) {
        window.chart.tpLine.setDashed(false);
        window.chart.tpLine.setWidth(3);
        window.chart.tpLine.setPrice(initTP, true);   // зелёный
        window.chart.redraw();
      }
    });
  }

  if (slRow) {
    const btn = slRow.querySelector('.add-sl');
    if (btn) btn.addEventListener('click', () => {
      slRow.innerHTML = `
        <label for="sl-input">SL</label>
        <input id="sl-input" type="number">
        <select>
          <option>Маркир.</option>
          <option>Послед.</option>
        </select>`;
      const initSL = lastMid ? lastMid * (1 - SL_OFFSET_PCT) : null;
      if (initSL && window.chart?.slLine) {
        window.chart.slLine.setDashed(false);
        window.chart.slLine.setWidth(3);
        window.chart.slLine.setPrice(initSL, false); // красный
        window.chart.redraw();
      }
    });
  }

}

// начальный пересчёт, чтобы значения сразу появились
recalc();

// === Ползунок процента объёма ===
  const slider =
    document.getElementById('volumeSlider') ||
    document.querySelector('.slider-row input[type="range"]') ||
    document.querySelector('.row.slider input[type="range"]');
  const labels = document.querySelectorAll('.range-labels span');

  function updateQtyFromSlider() {
    // элементы
    const slider = document.getElementById('volumeSlider')
      || document.querySelector('.slider-row input[type="range"]')
      || document.querySelector('.row.slider input[type="range"]');
    if (!slider) return;

    const leverageSelect = document.getElementById('leverage');
    const availableSpan  = document.getElementById('available');
    const priceInput = document.getElementById('order-price');
    const qtyInput       = document.getElementById('order-qty');
    previewLockUntil = Date.now() + 24000; // держим предпросмотр 3 секунды
    if (!leverageSelect || !availableSpan || !priceInput || !qtyInput) return;

    // процент слайдера
    const percent = parseInt(slider.value, 10) || 0;

    // плечо: серверное приоритетно, иначе из селектора
    const uiLev = parseInt(String(leverageSelect.value).replace('x','')) || 1;
    const lev   = (typeof serverLeverage === 'number' && serverLeverage > 0) ? serverLeverage : uiLev;

    // доступный баланс
    const avail = Number((availableSpan.textContent || '').replace(' USDT','')) || 0;

    // лимит по нотации: серверный приоритетно
    const maxNotional = (typeof serverMaxNotional === 'number')
      ? serverMaxNotional
      : (avail * lev);

    // текущая цена: ручной input или mid-price
    const midEl = document.getElementById('mid-price');
    const mid   = Number(midEl ? midEl.textContent : NaN);
    const price = Number(priceInput.value) || (isFinite(mid) ? mid : 0);
    if (price <= 0 || maxNotional <= 0) return;

    // расчёт количества из доли нотации
    const targetNotional = maxNotional * (percent / 100);
    const qty = targetNotional / price;

    qtyInput.value = (qty > 0 ? qty : 0).toFixed(3);
    qtyInput.dispatchEvent(new Event('input')); // дёргаем пересчёт cost/fee

    // подсветка делений, если есть
    const labels = document.querySelectorAll('.range-labels span');
    if (labels && labels.length) {
      labels.forEach(l => l.classList.remove('active'));
      const idx = Math.round(percent / 25);
      if (labels[idx]) labels[idx].classList.add('active');
    }
  }
  window.updateQtyFromSlider = updateQtyFromSlider;
});

document.addEventListener('DOMContentLoaded', () => {
  const slider = document.getElementById('volumeSlider')
    || document.querySelector('.slider-row input[type="range"]')
    || document.querySelector('.row.slider input[type="range"]');
  if (!slider || typeof window.updateQtyFromSlider !== 'function') return;

  slider.addEventListener('input',  window.updateQtyFromSlider);
  slider.addEventListener('change', window.updateQtyFromSlider);
  slider.dispatchEvent(new Event('input'));
});

function addTrade(trade) {
    const li = document.createElement('li');
    li.classList.add(trade.initiator_side === 'buy' ? 'buy' : 'sell');

    const priceSpan = document.createElement('span');
    priceSpan.className = 'price';
    priceSpan.textContent = trade.price.toFixed(3);

    const volSpan = document.createElement('span');
    volSpan.className = 'volume';
    volSpan.textContent = trade.volume.toFixed(4);

    li.appendChild(priceSpan);
    li.appendChild(volSpan);

    li.style.opacity = '0';
    tradesList.prepend(li);

    requestAnimationFrame(() => {
        li.style.transition = 'opacity 0.5s ease';
        li.style.opacity = '1';
    });

    enforceTapeLimit();
    tradesList.scrollTop = 0;
}

function applyAccountSnapshot(acc) {
  const put = (id, v, suffix='') => {
    const el = document.getElementById(id);
    if (el) el.textContent = (Number(v) || 0).toFixed(2) + suffix;
  };

  // balance и equity как есть
  put('acc-balance',   acc.balance);
  put('acc-equity',    acc.equity);

  // === Realized PnL с цветом и % ===
  const realizedEl = document.getElementById('acc-realized');
  if (realizedEl) {
    const base = acc.balance || 1;
    const pct  = (acc.realized_pnl / base) * 100;
    realizedEl.textContent = `${acc.realized_pnl.toFixed(2)} (${pct.toFixed(2)}%)`;
    realizedEl.style.color = acc.realized_pnl >= 0 ? '#3fb950' : '#f85149';
  }

  // === Unrealized PnL с цветом и % ===
  const upnlEl = document.getElementById('acc-upnl');
  if (upnlEl) {
    const base = acc.balance || 1;
    const pct  = (acc.upnl / base) * 100;
    upnlEl.textContent = `${acc.upnl.toFixed(2)} (${pct.toFixed(2)}%)`;
    upnlEl.style.color = acc.upnl >= 0 ? '#3fb950' : '#f85149';
  }

  // эти поля есть не на всех страницах — обновляем только если существуют
  const avail = Number(acc.available) || 0;
  put('available', avail, ' USDT');

  const lev = (typeof acc.leverage === 'number' ? acc.leverage : (typeof serverLeverage === 'number' ? serverLeverage : 1));
  const maxNotional = (acc.max_position_notional != null) ? Number(acc.max_position_notional) : (avail * lev);
  const maxEl = document.getElementById('max'); if (maxEl) maxEl.textContent = maxNotional.toFixed(2);

  const liqEl = document.getElementById('liq'); if (liqEl) liqEl.textContent = acc.liq_price ? Number(acc.liq_price).toFixed(3) : '--';
  put('cost', acc.used_margin);
  put('fee',  acc.fee_paid);
}

// ==== Инициализация Socket.IO-клиента ====

function initSocketIO() {
    socket = io();

    socket.on('connect', () => {
      console.log('Socket.IO connected, SID=', socket.id);
      if (window.location.pathname === '/profile') {
        socket.emit('get_user_trades');
        socket.emit('get_account');
      }
    });

    socket.on('orderbook_update', (data) => {
        console.log('Order book update:', data);
        updateOrderbook(data.bids, data.asks);

        // если qty ещё 0, пересчитаем от слайдера, когда mid появился
        const sliderEl =
          document.getElementById('volumeSlider') ||
          document.querySelector('.slider-row input[type="range"]') ||
          document.querySelector('.row.slider input[type="range"]');
        const qtyEl = document.getElementById('order-qty');
        if (sliderEl && qtyEl && (!qtyEl.value || Number(qtyEl.value) === 0)) {
          sliderEl.dispatchEvent(new Event('input'));
        }

        // если модалка открыта — пересчитаем метрики по свежему lastMid
        const modal = document.getElementById('close-modal');
        if (currentClosePos && !modal.classList.contains('hidden')) {
          updateCloseMetrics();
        }

    });

    socket.on('trade', (trade) => {
        console.log('Trade:', trade);
        addTrade(trade);
    });

    socket.on('history', (historyArray) => {
        console.log('Trade history:', historyArray);
        historyArray.forEach(oldTrade => addTrade(oldTrade));
        // после первичного наполнения тоже обрежем по актуальному лимиту
        enforceTapeLimit();
    });

    socket.on('confirmation', (msg) => {
        console.log('Server confirmation:', msg.message);
    });

    socket.on('error', (err) => {
        console.error('Server error:', err.message || err);
    });

    socket.on('account_update', (acc) => {
      if (typeof acc.leverage === 'number') serverLeverage = acc.leverage;
      if (acc.max_position_notional != null) serverMaxNotional = Number(acc.max_position_notional);

      applyAccountSnapshot(acc);

      try { localStorage.setItem('acc_snapshot', JSON.stringify(acc)); } catch (_) {}
    });

    socket.on('user_trades', (trades) => {
      const tbody = document.getElementById('profile-trades');
      if (!tbody) return;
      tbody.innerHTML = '';
      trades.slice(-100).reverse().forEach(t => {
        const row = document.createElement('tr');

        // время
        const tdTime = document.createElement('td');
        tdTime.textContent = new Date(t.ts * 1000).toLocaleTimeString();
        row.appendChild(tdTime);

        // сторона
        const tdSide = document.createElement('td');
        tdSide.textContent = t.side;
        tdSide.style.color = (t.side === 'buy') ? '#3fb950' : '#f85149';
        if (t.side === 'buy') row.classList.add('buy-trade');
        else row.classList.add('sell-trade');
        row.appendChild(tdSide);

        // цена
        const tdPrice = document.createElement('td');
        tdPrice.textContent = t.price.toFixed(3);
        row.appendChild(tdPrice);

        // количество
        const tdQty = document.createElement('td');
        tdQty.textContent = t.qty.toFixed(3);
        row.appendChild(tdQty);

        // комиссия
        const tdFee = document.createElement('td');
        tdFee.textContent = t.fee_delta.toFixed(6);
        row.appendChild(tdFee);

        // PnL
        const pnlCell = document.createElement('td');
        pnlCell.textContent = t.realized_delta.toFixed(2);
        pnlCell.style.color = t.realized_delta >= 0 ? '#3fb950' : '#f85149';
        row.appendChild(pnlCell);

        // проценты (вместо equity)
        const baseNotional = t.price * t.qty;
        const pnlPct = baseNotional > 0 ? (t.realized_delta / baseNotional) * 100 : 0;
        const pctCell = document.createElement('td');
        pctCell.textContent = pnlPct.toFixed(2) + '%';
        pctCell.style.color = pnlPct >= 0 ? '#3fb950' : '#f85149';
        row.appendChild(pctCell);

        tbody.appendChild(row);
      });
    });

        socket.on('positions_update', (pos) => {
          const tbody = document.querySelector('#positions-table tbody');
          tbody.innerHTML = '';

          lastPosition = pos;

          const qtyAbs = Math.abs(Number(pos.position_qty) || 0);
          if (qtyAbs === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 9;
            td.textContent = 'Нет открытых позиций';
            tr.appendChild(td);
            tbody.appendChild(tr);
            currentClosePos = null;
            if (entryPriceLine) entryPriceLine = null;
            return;
          }

          const tr = document.createElement('tr');

          // Направление
          const isLong = pos.position_qty > 0;
          const dirTd = document.createElement('td');
          dirTd.textContent = isLong ? 'Лонг' : 'Шорт';
          dirTd.style.color = isLong ? '#3fb950' : '#f85149';
          tr.appendChild(dirTd);

          // Объём
          const qtyTd = document.createElement('td');
          qtyTd.textContent = qtyAbs.toFixed(3);
          tr.appendChild(qtyTd);

          // Цена входа
          const entryTd = document.createElement('td');
          entryTd.textContent = pos.entry_price ? Number(pos.entry_price).toFixed(3) : '—';
          tr.appendChild(entryTd);

           // Текущая цена
           const mark = (pos.mark_price ?? lastMid);
           const markTd = document.createElement('td');
           markTd.textContent = mark != null ? Number(mark).toFixed(3) : '—';
           tr.appendChild(markTd);

           // UPnL
           const upnlTd = document.createElement('td');
           const up = Number(pos.upnl || 0);
           upnlTd.textContent = up.toFixed(2);
           upnlTd.className = up >= 0 ? 'profit' : 'loss';
           tr.appendChild(upnlTd);

           // Реализованный PnL
           const rpnL = document.createElement('td');
           const r = Number(pos.realized_pnl || 0);
           rpnL.textContent = r.toFixed(2);
           rpnL.className = r >= 0 ? 'profit' : 'loss';
           tr.appendChild(rpnL);

           // Маржа
           const marginTd = document.createElement('td');
           marginTd.textContent = Number(pos.used_margin || 0).toFixed(2);
           tr.appendChild(marginTd);

           // Ликвидация с правилом прочерка
           const liqTd = document.createElement('td');
           const liqOk = pos.liq_price && mark != null &&
                         (Math.abs(pos.liq_price - mark) / Math.max(Math.abs(mark), EPS) <= LIQ_MAX_DISTANCE_PCT);
           liqTd.textContent = liqOk ? Number(pos.liq_price).toFixed(3) : '—';
           tr.appendChild(liqTd);

            // Кнопка
            const actionTd = document.createElement('td');
            const closeBtn = document.createElement('button');
            closeBtn.textContent = 'Закрыть';
            closeBtn.className = 'close-pos-btn';
            closeBtn.addEventListener('click', () => openCloseModal(pos));
            actionTd.appendChild(closeBtn);
            tr.appendChild(actionTd);

            tbody.appendChild(tr);
            if (tpPrice && !tpPriceLine) createDraggableLine('tp', tpPrice);
            if (slPrice && !slPriceLine) createDraggableLine('sl', slPrice);

        });

    socket.on('disconnect', () => {
        console.warn('Socket.IO disconnected, reconnecting in 3s...');
        setTimeout(() => {
            initSocketIO();
        }, 3000);
    });
}

document.addEventListener('DOMContentLoaded', () => {
  initSocketIO();

  if (window.location.pathname !== '/profile') {
    fetchAndRenderCandlesLoop();
    enforceTapeLimit();
  } else {
    // префилл из кэша до ответа сервера
    try {
      const cached = JSON.parse(localStorage.getItem('acc_snapshot') || 'null');
      if (cached) applyAccountSnapshot(cached);
    } catch (_) {}
  }
});

// ==== CLOSE POSITION MODAL: logic ====

function $(sel, root = document) { return root.querySelector(sel); }
function setText(id, val) { const n = document.getElementById(id); if (n) n.textContent = val; }
function round3(x){ return Math.round((x + Number.EPSILON) * 1000) / 1000; }

function openCloseModal(pos) {
  currentClosePos = pos;
  const modal = document.getElementById('close-modal');
  setText('close-symbol', pos.symbol || 'EUR/USD'); // временно символ
  if (!modal) return;

  // базовые поля
  const side = pos.position_qty > 0 ? 'Лонг' : 'Шорт';
  const qtyAbs = Math.abs(pos.position_qty);
  const mark = pos.mark_price ?? lastMid ?? 0;
  const entry = pos.entry_price ?? mark;

  setText('close-side', side);
  setText('close-entry', entry ? entry.toFixed(3) : '—');
  setText('close-mark',  mark ?  mark.toFixed(3) : '—');

  // начальные значения: 100% позиции
  const qtySel = qtyAbs;
  const pnlPart = pos.position_qty > 0
    ? (mark - entry) * qtySel
    : (entry - mark) * qtySel;

  // поля формы, если они есть
  const qtyInput   = document.getElementById('close-qty');
  const priceInput = document.getElementById('close-price');
  const typeSel    = document.getElementById('close-type');
  const slider     = document.getElementById('close-range');

  if (qtyInput)   qtyInput.value   = qtySel.toFixed(3);
  if (priceInput) priceInput.value = typeSel && typeSel.value === 'limit' ? mark.toFixed(3) : '';

  // метрики
  const notional = qtySel * mark;
  const pnlText = (pnlPart >= 0 ? '+' : '') + pnlPart.toFixed(2);

  setText('close-pos-sum', notional.toFixed(2));
  setText('close-pos-pnl', pnlText);

  // классы PnL
  const pnlEl = document.getElementById('close-pnl');
  if (pnlEl) {
    pnlEl.classList.remove('profit', 'loss');
    pnlEl.classList.add(pnlPart >= 0 ? 'profit' : 'loss');
  }

  // ползунок 100%
  if (slider) {
    slider.value = 100;
    updateCloseSlider(); // обновит qty, метки и прогресс
  }

  modal.classList.remove('hidden');
}

function closeCloseModal() {
  const modal = document.getElementById('close-modal');
  if (modal) modal.classList.add('hidden');
  currentClosePos = null;
  const qtyInput = document.getElementById('close-qty');
  const priceInput = document.getElementById('close-price');
  if (qtyInput) qtyInput.value = '';
  if (priceInput) priceInput.value = '';
}

function updateCloseMetrics() {
  if (!currentClosePos) return;

  const mark = lastMid ?? currentClosePos.mark_price ?? 0;
  const entry = currentClosePos.entry_price ?? mark;
  const qtyAbs = Math.abs(currentClosePos.position_qty);

  const qtyInput = document.getElementById('close-qty');
  const qtySel = Math.max(0, Math.min(qtyAbs, parseFloat(qtyInput?.value || '0') || 0));

  const notional = qtySel * mark;
  const fee = notional * 0.0004; // предполагаем taker

  const pnlPart = currentClosePos.position_qty > 0
    ? (mark - entry) * qtySel
    : (entry - mark) * qtySel;

  // всегда обновляем mark
  setText('close-mark', mark ? mark.toFixed(3) : '—');

  // проценты для верхнего PNL (только если qtySel > 0)
  let pnlPct = 0;
  if (entry !== 0 && qtySel > 0) {
    pnlPct = (pnlPart / (entry * qtySel)) * 100;
  }
  setText('close-pnl', (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) + '%');

  // цифры для расчётного PnL внизу
  setText('close-pos-sum', notional.toFixed(2));
  setText('close-pos-pnl', (pnlPart >= 0 ? '+' : '') + pnlPart.toFixed(2));

  // цвет
  const pnlEl = document.getElementById('close-pnl');
  if (pnlEl) {
    pnlEl.classList.remove('profit', 'loss');
    pnlEl.classList.add(pnlPct >= 0 ? 'profit' : 'loss');
  }

  // цвет для числового PnL (Расчетный PnL)
  const pnlNumEl = document.getElementById('close-pos-pnl');
  if (pnlNumEl) {
    pnlNumEl.classList.remove('profit', 'loss');
    pnlNumEl.classList.add(pnlPart >= 0 ? 'profit' : 'loss');
  }

  setText('close-remaining', (qtyAbs - qtySel > 0 ? (qtyAbs - qtySel).toFixed(3) : '0.000'));
}

function updateCloseSlider() {
  const slider = document.getElementById('close-range');
  const qtyInput = document.getElementById('close-qty');
  if (!slider || !qtyInput || !currentClosePos) return;

  const percent = parseInt(slider.value, 10) || 0;
  const qtyAbs = Math.abs(currentClosePos.position_qty);
  const qtySel = round3(qtyAbs * (percent / 100));

  qtyInput.value = qtySel.toFixed(3);

  // подсветка 0/25/50/75/100
  const labels = document.querySelectorAll('#close-modal .range-labels span');
  labels.forEach(l => l.classList.remove('active'));
  const idx = Math.round(percent / 25);
  if (labels[idx]) labels[idx].classList.add('active');

  // прогресс для webkit
  slider.style.setProperty('--range-progress', percent + '%');

  updateCloseMetrics();
}

document.addEventListener('DOMContentLoaded', () => {
  const modal = document.getElementById('close-modal');
  if (!modal) return;

  const slider     = document.getElementById('close-range');
  const qtyInput   = document.getElementById('close-qty');
  const priceInput = document.getElementById('close-price');
  const typeSel    = document.getElementById('close-type');
  const confirmBtn = document.getElementById('close-confirm');
  const cancelBtn  = document.getElementById('close-cancel');
  const closeX     = modal.querySelector('.modal-header button') || document.getElementById('close-x');

  // ползунок + метки
  if (slider) {
    slider.addEventListener('input', updateCloseSlider);
    // клики по меткам
    modal.querySelectorAll('.range-labels span').forEach((label, i) => {
      label.style.cursor = 'pointer';
      label.addEventListener('click', () => {
        const p = i * 25;
        slider.value = p;
        updateCloseSlider();
      });
    });
  }

  // ручной ввод объёма — синхронизируем метрики и (по возможности) ползунок
  if (qtyInput) {
    qtyInput.addEventListener('input', () => {
      if (!currentClosePos) return;
      const qtyAbs = Math.abs(currentClosePos.position_qty);
      const v = Math.max(0, Math.min(qtyAbs, parseFloat(qtyInput.value || '0') || 0));
      qtyInput.value = v.toFixed(3);
      if (slider) {
        const percent = qtyAbs > 0 ? Math.round((v / qtyAbs) * 100) : 0;
        slider.value = percent;
        slider.style.setProperty('--range-progress', percent + '%');
      }
      updateCloseMetrics();
    });
  }

  // тип закрытия меняет доступность цены
  if (typeSel && priceInput) {
    const applyType = () => {
      const isLimit = typeSel.value === 'limit';
      priceInput.disabled = !isLimit;
      if (!isLimit) priceInput.value = '';
      updateCloseMetrics();
    };
    typeSel.addEventListener('change', applyType);
    applyType();
  }

  // закрытия
  if (cancelBtn) cancelBtn.addEventListener('click', closeCloseModal);
  if (closeX)    closeX.addEventListener('click', closeCloseModal);
  // клик по фону
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeCloseModal();
  });
  // Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeCloseModal();
  });

  // подтверждение
  if (confirmBtn) {
    confirmBtn.addEventListener('click', () => {
      if (!currentClosePos) return;

      const qty = Math.max(0, parseFloat(qtyInput?.value || '0') || 0);
      if (qty <= 0) return;

      const isLong = currentClosePos.position_qty > 0;
      const reduceSide = isLong ? 'sell' : 'buy';

      const total = Math.abs(currentClosePos.position_qty);
      const type  = typeSel ? typeSel.value : 'market';   // <— было не определено

      const closeSound = new Audio('sounds/ok.mp3'); // можно тот же файл

      if (type === 'market') {
        const sendQty = Math.min(qty, total);
        if (Math.abs(sendQty - total) <= EPS) socket.emit('close_position', {});
        else socket.emit('close_partial', { qty: sendQty });
      } else {
        const px = parseFloat(priceInput?.value || '0') || null;
        if (!px) return;
        socket.emit('add_order', {
          agent_id: 'terminal-ui',
          side: (currentClosePos.position_qty > 0) ? 'sell' : 'buy',
          volume: Math.min(qty, total),
          price: px,
          order_type: 'limit',
          reduce_only: true
        });
      }

      closeSound.currentTime = 0;
      closeSound.play();

      closeCloseModal();
    });
  }
});

(function initVolumeSlider(){
  const slider = document.getElementById('volumeSlider') || document.getElementById('range');
  const tip    = document.getElementById('volumeTooltip') || document.getElementById('range-tip');
  if(!slider || !tip) return;

  const show = ()=> tip.style.opacity = '1';
  const hide = ()=> tip.style.opacity = '0';

  const update = ()=>{
    const min = Number(slider.min || 0);
    const max = Number(slider.max || 100);
    const val = Number(slider.value || 0);
    const p = (val - min) / (max - min);

    const rect = slider.getBoundingClientRect();
    const cs = getComputedStyle(slider);
    const thumb = parseFloat(cs.getPropertyValue('--thumb-w')) || 16;
    const padL = parseFloat(cs.paddingLeft) || 0;
    const padR = parseFloat(cs.paddingRight) || 0;

    // центр бегунка в px относительно левого края инпута
    const usable = rect.width - padL - padR - thumb;
    const cx = padL + p * usable + thumb / 2;

    tip.textContent = Math.round(p * 100) + '%';
    tip.style.left = cx + 'px';        // центр тултипа над центром бегунка
    slider.style.setProperty('--fill', (p * 100) + '%'); // заливка трека
  };

  ['input','change','mouseenter','focus'].forEach(ev => slider.addEventListener(ev, ()=>{ update(); show(); }));
  ['mouseleave','blur'].forEach(ev => slider.addEventListener(ev, hide));

  update(); // стартовое состояние
})();

// ==== Отправка ордеров с терминала ====
document.addEventListener('DOMContentLoaded', () => {
  const buyBtn = document.querySelector('.actions .buy');
  const sellBtn = document.querySelector('.actions .sell');
  const qtyInput = document.getElementById('order-qty');
  const priceInput = document.getElementById('order-price');
  const orderTypeBtns = document.querySelectorAll('.order-types button');

  // звук при ордере
  const orderSound = new Audio('sounds/ok.mp3');

  let currentType = 'limit'; // по умолчанию

  const priceRow = document.getElementById('price-row');

  // отслеживаем выбор типа ордера
  orderTypeBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      currentType = btn.dataset.type || 'limit';

      // подсветка
      orderTypeBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      // скрытие строки цены
      if (currentType === 'market') {
        priceRow.style.display = 'none';
      } else {
        priceRow.style.display = '';
      }
    });
  });

  function sendOrder(side) {
    const volume = parseFloat(qtyInput.value);
    const price = currentType === 'limit' ? parseFloat(priceInput.value) : null;

    if (!volume || (currentType === 'limit' && !price)) {
      alert("Введите данные ордера");
      return;
    }

  const tpSlCheckbox = document.getElementById('enable-tpsl');
  let tp = null, sl = null;
  if (tpSlCheckbox && tpSlCheckbox.checked) {
      tp = parseFloat(document.getElementById('tp-input').value) || null;
      sl = parseFloat(document.getElementById('sl-input').value) || null;
  }

    socket.emit('add_order', {
      agent_id: 'terminal-ui',
      side: side,           // 'buy' или 'sell'
      volume: volume,
      price: price,
      order_type: currentType, // 'limit' или 'market'
      tpsl_enabled: tpSlCheckbox && tpSlCheckbox.checked,
      tp: tp,
      sl: sl,
      trigger_by: 'mark'
    });
  }

  buyBtn.addEventListener('click', () => {
    orderSound.currentTime = 0;
    orderSound.play();
    sendOrder('buy');
  });

  sellBtn.addEventListener('click', () => {
    orderSound.currentTime = 0;
    orderSound.play();
    sendOrder('sell');
  });
});

// ==== Обновление графика (candles) ====

let selectedTF = 5;

let candlesFetchToken = 0;
let candlesController = null; // для отмены предыдущего запроса

document.querySelectorAll('#tf-selector button').forEach(btn => {
  btn.addEventListener('click', () => {
    selectedTF = parseInt(btn.dataset.tf);
    if (candleTimer) clearTimeout(candleTimer);

    if (typeof fetchAndRenderCandles === 'function') fetchAndRenderCandles();
    if (typeof fetchAndRenderCandlesLoop === 'function') fetchAndRenderCandlesLoop();
  });
});





export { updateOrderbook, addTrade, initSocketIO };