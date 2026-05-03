# anti_trend_hedger.py
# AntiTrendHedger — потоковый хеджер / ребалансировщик против ускоряющегося тренда.
#
# Интерфейс совместим с твоей архитектурой:
#   class AntiTrendHedger:
#       __init__(agent_id: str, capital: float)
#       generate_orders(order_book, market_context=None, **kwargs) -> list[Order]
#       on_order_filled(order_id, price, qty, side, slippage: float = 0.0)
#       perceive_market(market_context) -> str
#       restore_capital()

import uuid
import time
import random
import math
from collections import deque
from typing import Deque, Optional, Dict, Any

import numpy as np

from order import Order, OrderSide, OrderType, TICK, quant


def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


class AntiTrendHedger:
    """
    Потоковый хеджер / ребалансировщик.

    Поведение:
      - следит за тем, насколько далеко mid ушёл от средней цены;
      - когда рынок "перетянут" вверх — мягко продаёт (SELL) небольшими клипами;
      - когда рынок "перетянут" вниз — мягко покупает (BUY);
      - контролирует инвентарь (до ~15% капитала в одну сторону);
      - часть ордеров лимитные около лучших цен, часть — маленькие market-клипы;
      - тайминг шагов зависит от силы растяжения (чем сильнее, тем чаще).
    """

    def __init__(self, agent_id: str, capital: float):
        self.agent_id = str(agent_id)
        self.capital = float(capital)

        self.cash = float(capital)
        self.position = 0.0  # >0: long, <0: short
        self.max_inv_notional = self.capital * 0.15  # до ~15% капитала в одну сторону

        # история сделок для оценки волы/тренда
        self.trade_history: Deque[Dict[str, Any]] = deque(maxlen=600)

        # EMAs по mid для оценки тренда и "растяжения"
        self.mid_ema_fast: Optional[float] = None
        self.mid_ema_slow: Optional[float] = None
        self.fast_tau = 30.0    # ~30 секунд
        self.slow_tau = 180.0   # ~3 минуты
        self.last_mid_ts = time.time()

        # оценка волатильности
        self.ewma_sigma: Optional[float] = None

        # тайминг
        self.next_ts = time.time() + random.uniform(5.0, 25.0)

        # флаг "отдыха", если рынок слишком бешеный
        self.cooldown_until = 0.0

    # ------------------------------------------------------------------
    def _update_trade_history(self, order_book):
        recent = list(order_book.trade_history)[-120:]
        for t in recent:
            self.trade_history.append(t)

    def _extract_features(self, order_book) -> Dict[str, Any]:
        snap = order_book.get_order_book_snapshot(depth=5)

        bids = snap.get("bids") or []
        asks = snap.get("asks") or []

        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None

        if best_bid is not None and best_ask is not None:
            mid = 0.5 * (best_bid + best_ask)
            spread = best_ask - best_bid
        else:
            mid = None
            spread = None

        top_bid_vol = float(bids[0]["volume"]) if bids else 0.0
        top_ask_vol = float(asks[0]["volume"]) if asks else 0.0
        top_depth = max(
            top_bid_vol * (best_bid or 0.0),
            top_ask_vol * (best_ask or 0.0)
        )

        trades = list(order_book.trade_history)[-200:]
        prices = [float(t["price"]) for t in trades]
        if len(prices) >= 10:
            rets = np.diff(np.log(prices))
            sigma = float(np.std(rets))
        else:
            sigma = 0.0

        return {
            "mid": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "top_depth": top_depth,
            "sigma": sigma,
        }

    def _update_emas(self, mid: float, now: float):
        dt = now - self.last_mid_ts
        if dt <= 0:
            dt = 1e-6
        self.last_mid_ts = now

        if self.mid_ema_fast is None or self.mid_ema_slow is None:
            self.mid_ema_fast = mid
            self.mid_ema_slow = mid
            return

        def a(dt_, tau_):
            return 1.0 - math.exp(-dt_ / max(tau_, 1e-9))

        af = a(dt, self.fast_tau)
        as_ = a(dt, self.slow_tau)

        self.mid_ema_fast = (1 - af) * self.mid_ema_fast + af * mid
        self.mid_ema_slow = (1 - as_) * self.mid_ema_slow + as_ * mid

    # ------------------------------------------------------------------
    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = time.time()

        if now < self.next_ts:
            return []

        if now < self.cooldown_until:
            self.next_ts = now + random.uniform(5.0, 15.0)
            return []

        self._update_trade_history(order_book)
        feat = self._extract_features(order_book)

        mid = feat["mid"]
        best_bid = feat["best_bid"]
        best_ask = feat["best_ask"]
        spread = feat["spread"] or 0.0

        if mid is None or best_bid is None or best_ask is None:
            self.next_ts = now + random.uniform(3.0, 10.0)
            return []

        self._update_emas(mid, now)

        if self.mid_ema_slow is None:
            self.next_ts = now + random.uniform(5.0, 15.0)
            return []

        # насколько mid ушёл от медленной EMA (в тиках)
        stretch_px = mid - self.mid_ema_slow
        stretch_ticks = stretch_px / max(TICK, 1e-9)

        # вола: при очень высокой просто ждём
        sigma = feat["sigma"]
        if self.ewma_sigma is None:
            self.ewma_sigma = sigma
        else:
            alpha = 0.05
            self.ewma_sigma = (1 - alpha) * self.ewma_sigma + alpha * sigma

        if self.ewma_sigma is not None and self.ewma_sigma > 0.0025:
            self.cooldown_until = now + random.uniform(20.0, 60.0)
            self.next_ts = now + random.uniform(10.0, 30.0)
            return []

        THRESH_BASE = 6.0    # начало реакции (~6 тик)
        THRESH_STRONG = 18.0 # максимум шкалы

        if abs(stretch_ticks) < THRESH_BASE:
            orders = self._flatten_inventory(mid, best_bid, best_ask, spread)
            self.next_ts = now + random.uniform(8.0, 25.0)
            return orders

        # сторона хеджа: против растяжения
        if stretch_ticks > THRESH_BASE:
            hedge_side = OrderSide.ASK  # перекуплено → продаём
        else:
            hedge_side = OrderSide.BID  # перепродано → покупаем

        # сила сигнала
        stretch_mag = _clip(abs(stretch_ticks), THRESH_BASE, THRESH_STRONG)
        stretch_factor = (stretch_mag - THRESH_BASE) / max(THRESH_STRONG - THRESH_BASE, 1e-9)
        stretch_factor = _clip(stretch_factor, 0.0, 1.0)

        # риск по инвентарю
        inv_notional = abs(self.position * mid)
        inv_ratio = inv_notional / max(self.max_inv_notional, 1e-9)

        # если уже под упор в одну сторону — дальше не лезем в ту же
        if inv_ratio >= 1.0:
            hedge_side = OrderSide.BID if self.position < 0 else OrderSide.ASK
            stretch_factor *= 0.4

        # базовый notional клипа: 0.2–0.8% капитала
        base_notional = self.capital * random.uniform(0.002, 0.008)
        notional = base_notional * (0.7 + 1.6 * stretch_factor)

        remaining_notional = max(self.max_inv_notional - inv_notional, 0.0)
        if remaining_notional <= 0:
            orders = self._flatten_inventory(mid, best_bid, best_ask, spread)
            self.next_ts = now + random.uniform(15.0, 35.0)
            return orders

        notional = min(notional, remaining_notional)
        qty = notional / max(mid, 1.0)
        qty = max(1.0, qty)

        orders = []

        # MARKET-доля растёт с растяжением
        use_mkt_prob = 0.15 + 0.35 * stretch_factor
        if spread <= 2 * TICK:
            use_mkt_prob += 0.05
        use_mkt_prob = _clip(use_mkt_prob, 0.05, 0.55)

        if random.random() < use_mkt_prob:
            mkt_qty = qty * random.uniform(0.4, 0.8)
            orders.append(
                Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=hedge_side,
                    volume=float(mkt_qty),
                    price=None,
                    order_type=OrderType.MARKET,
                )
            )
            qty -= mkt_qty

        if qty > 0.5:
            if hedge_side == OrderSide.ASK:
                ref = best_ask
                offset = random.choice([0, 1, 1, 2])
                price = ref + offset * TICK
            else:
                ref = best_bid
                offset = random.choice([0, 1, 1, 2])
                price = ref - offset * TICK

            price = quant(price)

            orders.append(
                Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=hedge_side,
                    volume=float(qty),
                    price=float(price),
                    order_type=OrderType.LIMIT,
                    ttl=random.randint(15, 45),
                )
            )

        base_dt = random.uniform(10.0, 30.0)
        dt_factor = 1.4 - 0.8 * stretch_factor  # от ~1.4 до ~0.6
        dt = base_dt * dt_factor

        jitter = random.uniform(-0.4 * dt, 0.4 * dt)
        dt_final = max(4.0, dt + jitter)

        self.next_ts = now + dt_final
        return orders

    # ------------------------------------------------------------------
    def _flatten_inventory(self, mid: float, best_bid: float, best_ask: float, spread: float):
        orders = []
        if self.position > 0.0:
            qty = min(self.position, max(1.0, self.position * 0.25))
            ref = best_ask if best_ask is not None else (mid + TICK)
            price = quant(ref + random.choice([0, 0, 1]) * TICK)
            orders.append(
                Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=OrderSide.ASK,
                    volume=float(qty),
                    price=float(price),
                    order_type=OrderType.LIMIT,
                    ttl=random.randint(20, 60),
                )
            )
        elif self.position < 0.0:
            qty = min(-self.position, max(1.0, -self.position * 0.25))
            ref = best_bid if best_bid is not None else (mid - TICK)
            price = quant(ref - random.choice([0, 0, 1]) * TICK)
            orders.append(
                Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=OrderSide.BID,
                    volume=float(qty),
                    price=float(price),
                    order_type=OrderType.LIMIT,
                    ttl=random.randint(20, 60),
                )
            )
        return orders

    # ------------------------------------------------------------------
    def on_order_filled(self, order_id, price, qty, side, slippage: float = 0.0):
        notional = price * qty
        if side == OrderSide.BID:
            self.position += qty
            self.cash -= notional
        else:
            self.position -= qty
            self.cash += notional

    def restore_capital(self):
        pass

    def perceive_market(self, market_context):
        return "anti_trend_hedger"
