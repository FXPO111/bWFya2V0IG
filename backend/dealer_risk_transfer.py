# dealer_risk_transfer.py
#
# DealerRiskTransfer — дилерский риск-трансфер (FX-style):
#  - получает инвентарь через случайные филлы (как будто internalization/клиентский поток),
#  - затем хеджирует/переводит риск на рынок агрессивными ордерами (MARKET),
#  - делает это НЕ "в 3 секунды", а через план (staggered slicing) с естественной стохастикой.
#
# ВАЖНО: вы просили не добавлять новые лимитные логики — здесь только MARKET-ордера.
#
# Совместимость с сервером:
#   - agents_loop вызывает generate_orders(order_book, market_context)
#   - notify_agent_fill вызывает on_order_filled(order_id, price, qty, side)

import time
import uuid
import random
from collections import deque
from typing import List, Dict, Any, Optional, Tuple

from order import Order, OrderSide, OrderType, TICK


def _ctx_now(ctx) -> float:
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _best_from_snapshot(snapshot: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], float, float, float]:
    bids = snapshot.get("bids") or []
    asks = snapshot.get("asks") or []

    best_bid = _safe_float(bids[0]["price"], None) if bids else None
    best_ask = _safe_float(asks[0]["price"], None) if asks else None

    bid_sz1 = _safe_float(bids[0].get("volume", bids[0].get("qty", 0.0)), 0.0) if bids else 0.0
    ask_sz1 = _safe_float(asks[0].get("volume", asks[0].get("qty", 0.0)), 0.0) if asks else 0.0

    if best_bid is not None and best_ask is not None:
        mid = 0.5 * (best_bid + best_ask)
        spread = max(0.0, best_ask - best_bid)
    else:
        mid = None
        spread = 0.0

    return best_bid, best_ask, mid, spread, max(bid_sz1, 0.0), max(ask_sz1, 0.0)


class DealerRiskTransfer:
    """
    FX dealer risk transfer (MARKET-only).

    1) При нулевом инвентаре агент иногда "абсорбит" дисбаланс потока (MARKET против imbal),
       тем самым набирает позицию (как internalization/клиентский поток).
    2) Когда позиция стала существенной — запускает план риск-трансфера:
       режет инвентарь MARKET-слайсами с неравномерной сеткой по времени и heavy-tail по размеру.
    """

    supports_conn = False

    def __init__(
        self,
        agent_id: str,
        capital: float,
        *,
        soft_inventory_frac: float = 0.01,
        hard_inventory_frac: float = 0.03,
        max_slice_frac: float = 0.00035,
        min_slice_notional: float = 15_000.0,
        max_slice_notional: float = 180_000.0,
        min_interval_s: float = 0.6,
        max_interval_s: float = 2.4,
        plan_horizon_s: Tuple[float, float] = (25.0, 140.0),
        microhedge_prob: float = 0.10,
        microhedge_max_frac: float = 0.00008,
    ):
        self.agent_id = str(agent_id)
        self.capital = float(capital)

        # риск-параметры (в notional)
        self.soft_inventory_frac = float(soft_inventory_frac)
        self.hard_inventory_frac = float(hard_inventory_frac)
        self.max_slice_frac = float(max_slice_frac)
        self.min_slice_notional = float(min_slice_notional)
        self.max_slice_notional = float(max_slice_notional)
        self.min_interval_s = float(min_interval_s)
        self.max_interval_s = float(max_interval_s)
        self.plan_horizon_s = (float(plan_horizon_s[0]), float(plan_horizon_s[1]))
        self.microhedge_prob = float(microhedge_prob)
        self.microhedge_max_frac = float(microhedge_max_frac)

        # состояние
        self.inventory_qty: float = 0.0
        self.avg_price: Optional[float] = None
        self.realized_pnl: float = 0.0

        # для warmup_agents()
        self.price_history = deque(maxlen=900)

        # план
        self._hedge_active: bool = False
        self._plan_end_ts: float = 0.0
        self._next_action_ts: float = 0.0
        self._cooldown_until: float = 0.0

        # стохастика активности (AR(1))
        self._activity: float = 0.25

    # ---------- позиция / pnl ----------
    def _apply_fill(self, side: OrderSide, price: float, qty: float):
        price = float(price)
        qty = float(qty)
        if qty <= 0:
            return

        if side == OrderSide.BID:
            if self.inventory_qty >= 0:
                new_qty = self.inventory_qty + qty
                if new_qty > 0:
                    if self.avg_price is None:
                        self.avg_price = price
                    else:
                        self.avg_price = (self.avg_price * self.inventory_qty + price * qty) / max(new_qty, 1e-12)
                self.inventory_qty = new_qty
            else:
                cover = min(qty, -self.inventory_qty)
                if self.avg_price is not None:
                    self.realized_pnl += (self.avg_price - price) * cover
                self.inventory_qty += cover
                qty_left = qty - cover
                if abs(self.inventory_qty) <= 1e-12:
                    self.inventory_qty = 0.0
                    self.avg_price = None
                if qty_left > 0:
                    self.inventory_qty = qty_left
                    self.avg_price = price

        else:
            if self.inventory_qty <= 0:
                new_qty = self.inventory_qty - qty
                if new_qty < 0:
                    if self.avg_price is None:
                        self.avg_price = price
                    else:
                        self.avg_price = (self.avg_price * (-self.inventory_qty) + price * qty) / max((-new_qty), 1e-12)
                self.inventory_qty = new_qty
            else:
                sell = min(qty, self.inventory_qty)
                if self.avg_price is not None:
                    self.realized_pnl += (price - self.avg_price) * sell
                self.inventory_qty -= sell
                qty_left = qty - sell
                if abs(self.inventory_qty) <= 1e-12:
                    self.inventory_qty = 0.0
                    self.avg_price = None
                if qty_left > 0:
                    self.inventory_qty = -qty_left
                    self.avg_price = price

    def on_order_filled(self, order_id, price, qty, side, slippage: float = 0.0):
        # server зовёт (order_id, price, volume, OrderSide)
        try:
            self._apply_fill(side, price, qty)
        except Exception:
            pass

    def restore_capital(self):
        return

    def perceive_market(self, market_context):
        return "dealer_risk_transfer"

    # ---------- поток по trade_history ----------
    def _flow_imbalance(self, order_book, now: float, window_s: float = 12.0) -> Tuple[float, float, float]:
        """
        Возвращает (imbalance in [-1,1], buy_vol, sell_vol) по taker_side.
        """
        try:
            trades = getattr(order_book, "trade_history", None)
            if not trades:
                return 0.0, 0.0, 0.0
            buy = 0.0
            sell = 0.0
            for t in reversed(trades):
                ts = _safe_float(t.get("timestamp", now), now)
                if now - ts > window_s:
                    break
                vol = _safe_float(t.get("volume", 0.0), 0.0)
                if str(t.get("taker_side", "")).lower() in ("buy", "bid"):
                    buy += vol
                else:
                    sell += vol
            tot = buy + sell
            if tot <= 1e-12:
                return 0.0, buy, sell
            imb = (buy - sell) / tot
            return float(_clip(imb, -1.0, 1.0)), buy, sell
        except Exception:
            return 0.0, 0.0, 0.0

    # ---------- планирование ----------
    def _maybe_start_plan(self, now: float, inv_notional: float) -> None:
        if self._hedge_active:
            return
        if now < self._cooldown_until:
            return

        hard_th = self.capital * self.hard_inventory_frac
        if inv_notional < hard_th:
            return

        horizon = random.uniform(self.plan_horizon_s[0], self.plan_horizon_s[1])
        self._plan_end_ts = now + horizon
        self._hedge_active = True
        self._next_action_ts = now + random.uniform(self.min_interval_s, self.max_interval_s)
        self._cooldown_until = now + random.uniform(6.0, 14.0)

    def _plan_finished(self, now: float, inv_notional: float) -> bool:
        if not self._hedge_active:
            return True
        if inv_notional <= self.capital * (self.soft_inventory_frac * 0.55):
            return True
        if now >= self._plan_end_ts:
            return True
        return False

    def _slice_notional(self, mid: float, spread: float, top_liq_qty: float) -> float:
        base = self.capital * self.max_slice_frac

        sp = max(0.0, spread)
        sp_pen = 1.0
        if mid > 0 and sp > 0:
            sp_ticks = sp / max(TICK, 1e-12)
            if sp_ticks > 4.0:
                sp_pen = 1.0 / (1.0 + 0.22 * (sp_ticks - 4.0))

        liq_cap_qty = top_liq_qty * random.uniform(0.35, 0.70)
        liq_cap_notional = max(0.0, liq_cap_qty) * mid

        notional = base * sp_pen
        notional = _clip(notional, self.min_slice_notional, self.max_slice_notional)
        if liq_cap_notional > 0:
            notional = min(notional, liq_cap_notional)

        notional *= random.uniform(0.75, 1.25)
        return max(0.0, notional)

    def _schedule_next(self, now: float, urgency: float) -> None:
        urgency = _clip(urgency, 0.0, 1.0)
        lo = self.min_interval_s * (1.0 - 0.35 * urgency)
        hi = self.max_interval_s * (1.0 - 0.55 * urgency)
        lo = max(0.15, lo)
        hi = max(lo, hi)
        self._next_action_ts = now + random.uniform(lo, hi)

    # ---------- main ----------
    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        now = _ctx_now(market_context)
        self._activity = _clip(0.86 * self._activity + 0.14 * random.random(), 0.0, 1.0)

        snapshot = order_book.get_order_book_snapshot(depth=5)
        best_bid, best_ask, mid, spread, bid_sz1, ask_sz1 = _best_from_snapshot(snapshot)
        if mid is None:
            return []
        self.price_history.append(float(mid))

        inv = float(self.inventory_qty)
        inv_notional = abs(inv) * mid

        imb, _, _ = self._flow_imbalance(order_book, now, window_s=12.0)

        # 1) Набор позиции (internalization-like): MARKET против дисбаланса
        if inv_notional <= 1e-9:
            strength = abs(imb)
            p = 0.02 + 0.22 * strength + 0.10 * self._activity
            if random.random() > p:
                return []

            side = OrderSide.ASK if imb >= 0.0 else OrderSide.BID
            frac = _clip(0.00004 + 0.00026 * strength, 0.00004, 0.00040)
            notional = self.capital * frac * random.uniform(0.70, 1.25)

            top_liq_qty = ask_sz1 if side == OrderSide.ASK else bid_sz1
            if top_liq_qty > 0:
                notional = min(notional, top_liq_qty * mid * random.uniform(0.25, 0.60))

            notional = _clip(notional, self.min_slice_notional, self.max_slice_notional * 0.90)
            qty = max(1.0, notional / max(mid, 1e-12))

            return [
                Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=float(qty),
                    price=None,
                    order_type=OrderType.MARKET,
                    ttl=None,
                    metadata={"role": "dealer_risk_transfer", "absorb": True, "flow_imb": float(imb)},
                )
            ]

        # 2) Старт плана при hard threshold
        self._maybe_start_plan(now, inv_notional)
        if self._plan_finished(now, inv_notional):
            self._hedge_active = False

        soft_th = self.capital * self.soft_inventory_frac
        do_micro = (not self._hedge_active) and (inv_notional >= soft_th) and (
            random.random() < (self.microhedge_prob + 0.10 * self._activity)
        )

        if not do_micro and (not self._hedge_active or now < self._next_action_ts):
            return []

        hedge_side = OrderSide.ASK if inv > 0 else OrderSide.BID

        # срочность: поток против позиции => быстрее
        flow_against = max(0.0, -imb) if inv > 0 else max(0.0, imb)
        inv_pressure = _clip(inv_notional / max(self.capital * self.hard_inventory_frac, 1e-12), 0.0, 2.0)
        urgency = _clip(0.35 * inv_pressure + 0.65 * flow_against, 0.0, 1.0)

        top_liq_qty = ask_sz1 if hedge_side == OrderSide.ASK else bid_sz1
        slice_notional = self._slice_notional(mid=mid, spread=spread, top_liq_qty=top_liq_qty)

        if do_micro:
            slice_notional = min(slice_notional, self.capital * self.microhedge_max_frac)

        slice_qty = slice_notional / max(mid, 1e-12)
        slice_qty = max(1.0, float(slice_qty))
        slice_qty = min(slice_qty, abs(inv))

        if slice_qty <= 1e-9:
            return []

        self._schedule_next(now, urgency=urgency)

        return [
            Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=hedge_side,
                volume=float(slice_qty),
                price=None,
                order_type=OrderType.MARKET,
                ttl=None,
                metadata={"role": "dealer_risk_transfer", "hedge": True, "urgency": float(urgency)},
            )
        ]
