import time
import uuid
import random

from collections import deque
from datetime import datetime

import numpy as np

from order import Order, OrderSide, OrderType, quant

def _ctx_now(ctx):
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()

# Константы
PREP_RANGE = 0.002       # 0.2% от диапазона, начинаем "подготовку"
ACTIVE_WINDOW = 30 * 60  # 30 минут в активной фазе
COOLDOWN_WINDOW = 2 * 60 # 2 минуты на остывание
MAX_ACTIVE_RATIO = 0.15  # Максимум 15% капитала в торговле при пробое

class BreakoutTrader:
    def __init__(self, agent_id: str, capital: float):
        self.agent_id = agent_id
        self.capital = capital
        self.cash = capital
        self.inventory = 0.0

        self.mode = "idle"
        self.mode_ts = None

        self.local_high = None
        self.local_low = None
        self.recent_range = None

        self.last_order_time = 0
        self.order_interval = random.uniform(1, 4)  # интервал в секундах между ордерами
        self.entry_price = None  # цена входа в позицию (для отслеживания движения)
        self.max_move_pct = 0.01  # например, 1% движения, после которого перестаем торговать

        self.pnl_hist = deque(maxlen=200)

    def update_market_structure(self, conn):
        from candle_db import load_candles
        now = int(time.time())
        candles = load_candles(conn, '1m', now - 60 * 60, now)

        if not candles:
            self.local_high = self.local_low = self.recent_range = None
            return

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        self.local_high = max(highs)
        self.local_low = min(lows)
        self.recent_range = self.local_high - self.local_low

    def _reset(self):
        self.mode = "idle"
        self.mode_ts = None

    def _enter_mode(self, new_mode):
        self.mode = new_mode
        self.mode_ts = getattr(self, "_now_ts", time.time())

    def _should_exit_active(self, mid):
        if getattr(self, "_now_ts", time.time()) - self.mode_ts > ACTIVE_WINDOW:
            return True
        if self.mode == "active_long" and mid < self.local_high:
            return True
        if self.mode == "active_short" and mid > self.local_low:
            return True
        return False

    def _should_exit_prep(self, mid):
        if self.mode == "prep_long" and mid < self.local_high * (1 - PREP_RANGE):
            return True
        if self.mode == "prep_short" and mid > self.local_low * (1 + PREP_RANGE):
            return True
        return False

    def generate_orders(self, order_book, market_context=None):
        now = _ctx_now(market_context)
        self._now_ts = now
        snap = order_book.get_order_book_snapshot(depth=1)
        if not snap['bids'] or not snap['asks'] or self.local_high is None:
            return []

        best_bid = snap['bids'][0]['price']
        best_ask = snap['asks'][0]['price']
        mid = (best_bid + best_ask) / 2.0

        orders = []

        # Cooldown
        if self.mode == "cooldown":
            if getattr(self, "_now_ts", time.time()) - self.mode_ts > COOLDOWN_WINDOW:
                self._reset()
            return []

        # Проверка выхода из active
        if self.mode in ["active_long", "active_short"] and self._should_exit_active(mid):
            if self.inventory > 0:
                self._enter_mode("exit_long")
            elif self.inventory < 0:
                self._enter_mode("exit_short")
            else:
                self._enter_mode("cooldown")
            return []

        # Фаза выхода
        if self.mode in ["exit_long", "exit_short"]:
            if abs(self.inventory) < 1e-6:
                self._enter_mode("cooldown")
                return []

            # маленький кусочек позиции (5-10%)
            exit_qty = abs(self.inventory) * np.random.uniform(0.05, 0.1)
            side = OrderSide.ASK if self.mode == "exit_long" else OrderSide.BID
            price = best_bid if side == OrderSide.ASK else best_ask

            orders.append(Order(
                uuid.uuid4().hex, self.agent_id, side,
                exit_qty, quant(price), OrderType.MARKET, ttl=None
            ))

            # уменьшаем инвентарь вручную (будет уточнено в on_order_filled)
            if side == OrderSide.ASK:
                self.inventory -= exit_qty
            else:
                self.inventory += exit_qty

            return orders

        # Check for mode exits
        if self.mode in ["active_long", "active_short"] and self._should_exit_active(mid):
            self._enter_mode("cooldown")
            return []

        if self.mode in ["prep_long", "prep_short"] and self._should_exit_prep(mid):
            self._reset()
            return []

        # Detect potential breakouts
        if self.mode == "idle" and self.recent_range > 0:
            if abs(mid - self.local_high) / self.recent_range < PREP_RANGE:
                self._enter_mode("prep_long")
            elif abs(mid - self.local_low) / self.recent_range < PREP_RANGE:
                self._enter_mode("prep_short")

        trade_amount = self.capital * MAX_ACTIVE_RATIO / mid
        trade_amount = max(1, trade_amount)

        if self.mode == "prep_long":
            price = best_ask + 0.01
            orders.append(Order(
                uuid.uuid4().hex, self.agent_id, OrderSide.BID,
                trade_amount * 0.25, quant(price), OrderType.LIMIT, ttl=3
            ))
            if mid > self.local_high:
                self._enter_mode("active_long")

        elif self.mode == "prep_short":
            price = best_bid - 0.01
            orders.append(Order(
                uuid.uuid4().hex, self.agent_id, OrderSide.ASK,
                trade_amount * 0.25, quant(price), OrderType.LIMIT, ttl=3
            ))
            if mid < self.local_low:
                self._enter_mode("active_short")

        elif self.mode == "active_long":
            price = best_ask + 0.01
            current_time = now
            # Если ещё нет цены входа — запишем её при первом ордере
            if self.entry_price is None:
                self.entry_price = mid

            # Проверяем движение цены от entry_price
            price_move = abs(mid - self.entry_price) / self.entry_price
            if price_move > self.max_move_pct:
                # Превышен порог движения цены — переходим в cooldown
                self._enter_mode("cooldown")
                self.entry_price = None
                return []

            # Проверяем таймер отправки ордера
            if current_time - self.last_order_time < self.order_interval:
                return []  # ещё рано слать новый ордер

            # Обновляем время и интервал для следующего ордера
            self.last_order_time = current_time
            self.order_interval = random.uniform(1, 5)

            orders.append(Order(
                uuid.uuid4().hex, self.agent_id, OrderSide.BID,
                trade_amount * 0.5, quant(price), OrderType.MARKET, ttl=None
            ))

        elif self.mode == "active_short":
            price = best_bid - 0.01
            current_time = now

            # Если ещё нет цены входа — запишем её при первом ордере
            if self.entry_price is None:
                self.entry_price = mid

            # Проверяем движение цены от entry_price
            price_move = abs(mid - self.entry_price) / self.entry_price
            if price_move > self.max_move_pct:
                # Превышен порог движения цены — переходим в cooldown
                self._enter_mode("cooldown")
                self.entry_price = None
                return []

            # Проверяем таймер отправки ордера
            if current_time - self.last_order_time < self.order_interval:
                return []  # ещё рано слать новый ордер

            # Обновляем время и интервал для следующего ордера
            self.last_order_time = current_time
            self.order_interval = random.uniform(1, 5)

            orders.append(Order(
                uuid.uuid4().hex, self.agent_id, OrderSide.ASK,
                trade_amount * 0.5, quant(price), OrderType.MARKET, ttl=None
            ))

        return orders

    def on_order_filled(self, oid, price, qty, side):
        if side == OrderSide.BID:
            self.cash -= price * qty
            self.inventory += qty
        else:
            self.cash += price * qty
            self.inventory -= qty

        mtm = self.inventory * price + self.cash - self.capital
        self.pnl_hist.append(mtm)
