# latency_arb.py
import uuid
import random
import time
from collections import deque
from typing import List, Optional, Dict, Any

from backend.core.order import Order, OrderSide, OrderType, TICK


def _ctx_now(ctx) -> float:
    v = getattr(ctx, "now_ts", None)
    try:
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class LatencyArb:
    """
    Latency arbitrageur: pickoff stale quotes before slow LPs cancel/requote.

    - Uses MARKET for entry (fast hit)
    - Uses small LIMIT for exit with quick cancel/replace
    - Hard risk limits + cooldown
    """

    def __init__(
        self,
        agent_id: str = "lat_arb",
        capital: float = 5_000_000.0,
        edge_ticks: int = 2,
        take_ticks: int = 1,
        max_pos_notional_frac: float = 0.01,   # max 1% of capital exposure
        base_notional_frac: float = 0.0008,    # per shot
        cooldown_s: float = 0.35,
        max_hold_s: float = 1.5,
        fast_latency_ms: int = 1,
        fast_jitter_ms: int = 1,
    ):
        self.agent_id = agent_id
        self.capital = float(capital)

        self.edge_ticks = int(edge_ticks)
        self.take_ticks = int(take_ticks)

        self.max_pos_notional_frac = float(max_pos_notional_frac)
        self.base_notional_frac = float(base_notional_frac)

        self.cooldown_s = float(cooldown_s)
        self.max_hold_s = float(max_hold_s)

        self.fast_latency_ms = int(fast_latency_ms)
        self.fast_jitter_ms = int(fast_jitter_ms)

        # state
        self.pos_qty = 0.0
        self.avg_px: Optional[float] = None

        self._last_trade_idx = 0
        self._trades = deque(maxlen=120)

        self._cooldown_until = 0.0

        self._exit_order_id: Optional[str] = None
        self._exit_px: Optional[float] = None
        self._hold_deadline = 0.0

    # -------- fills --------

    def on_order_filled(self, order_id: str, trade_price: float, trade_volume: float, side: str):
        """
        server.py обычно зовёт on_order_filled(...) у агента.
        side тут может быть 'bid'/'ask' или 'BID'/'ASK' — поэтому нормализуем.
        """
        s = str(side).lower()
        is_buy = ("bid" in s) or (s == "buy")

        qty = float(trade_volume)
        px = float(trade_price)

        # if this was our exit order, clear tracking
        if self._exit_order_id == order_id:
            self._exit_order_id = None
            self._exit_px = None

        if qty <= 0:
            return

        if self.avg_px is None or self.pos_qty == 0:
            self.pos_qty = qty if is_buy else -qty
            self.avg_px = px
            return

        # position accounting (simple)
        pos = self.pos_qty
        if is_buy:
            if pos >= 0:
                new_pos = pos + qty
                self.avg_px = (self.avg_px * pos + px * qty) / max(new_pos, 1e-9)
                self.pos_qty = new_pos
            else:
                # cover short
                closing = min(qty, -pos)
                pos += closing
                if qty > closing:
                    # flip to long
                    self.pos_qty = qty - closing
                    self.avg_px = px
                else:
                    self.pos_qty = pos
                    if self.pos_qty == 0:
                        self.avg_px = None
        else:
            if pos <= 0:
                new_pos = pos - qty
                # keep avg for short (approx)
                self.avg_px = px if self.avg_px is None else self.avg_px
                self.pos_qty = new_pos
            else:
                # sell to reduce long
                closing = min(qty, pos)
                pos -= closing
                if qty > closing:
                    # flip to short
                    self.pos_qty = -(qty - closing)
                    self.avg_px = px
                else:
                    self.pos_qty = pos
                    if self.pos_qty == 0:
                        self.avg_px = None

    # -------- core logic --------

    def _update_trades(self, trade_history: List[Dict[str, Any]]):
        if not trade_history:
            return
        n = len(trade_history)
        start = min(max(self._last_trade_idx, 0), n)
        for i in range(start, n):
            t = trade_history[i]
            self._trades.append(t)
        self._last_trade_idx = n

    def _fair_mid(self, mid: float, spread: float) -> float:
        """
        Fast fair mid:
          - tape imbalance (taker buys vs sells)
          - micro momentum (last price vs older)
        """
        if not self._trades:
            return mid

        # tape imbalance by taker_side
        buy_v = 0.0
        sell_v = 0.0
        prices = []

        for t in list(self._trades)[-60:]:
            px = float(t.get("price", mid))
            prices.append(px)
            vol = float(t.get("volume", 0.0))
            ts = str(t.get("taker_side", "")).lower()
            if "buy" in ts or "bid" in ts:
                buy_v += vol
            elif "sell" in ts or "ask" in ts:
                sell_v += vol

        tot = buy_v + sell_v
        imb = 0.0 if tot <= 1e-12 else (buy_v - sell_v) / tot  # [-1..+1]

        # momentum in ticks
        mom_ticks = 0.0
        if len(prices) >= 8:
            mom_ticks = (prices[-1] - prices[-8]) / max(TICK, 1e-9)

        # weights
        k_flow = 0.55
        k_mom = 0.10

        return mid + (k_flow * imb * max(spread, TICK)) + (k_mom * mom_ticks * TICK)

    def _mk_md(self, latency_ms: int, jitter_ms: int, ttl_ms: int = 0) -> dict:
        md = {"latency_ms": int(latency_ms), "latency_jitter_ms": int(jitter_ms)}
        if ttl_ms > 0:
            md["ttl_ms"] = int(ttl_ms)
        return md

    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = _ctx_now(market_context)

        # единый источник тейпа — из ордербука, как у других агентов
        th = list(getattr(order_book, "trade_history", []))
        self._update_trades(th)

        snap = order_book.get_order_book_snapshot(depth=1)
        bids = snap.get("bids", [])
        asks = snap.get("asks", [])
        if not bids or not asks:
            return []

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        mid = 0.5 * (best_bid + best_ask)
        spread = max(best_ask - best_bid, TICK)

        # cooldown
        if now < self._cooldown_until:
            return self._maybe_unwind(now, best_bid, best_ask, mid)

        fair = self._fair_mid(mid, spread)
        edge = self.edge_ticks * TICK

        # risk: cap exposure
        max_notional = self.capital * self.max_pos_notional_frac
        cur_notional = abs(self.pos_qty) * mid
        can_add = max_notional - cur_notional

        orders: List[Order] = []

        # always try to manage exit if in position
        orders.extend(self._maybe_unwind(now, best_bid, best_ask, fair))

        if can_add <= self.capital * 0.0001:
            return orders

        # entry sizing (scaled by dislocation)
        disloc_up = fair - best_ask
        disloc_dn = best_bid - fair

        # BUY pickoff
        if disloc_up >= edge:
            strength = _clip(disloc_up / (edge * 3.0), 0.0, 1.0)
            notional = min(self.capital * self.base_notional_frac * (0.35 + 0.9 * strength), can_add)
            qty = max(1.0, notional / best_ask)

            o = Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=OrderSide.BID,
                volume=float(qty),
                price=None,
                order_type=OrderType.MARKET,
                ttl=None,
                metadata=self._mk_md(self.fast_latency_ms, self.fast_jitter_ms),
            )
            orders.append(o)

            self._cooldown_until = now + self.cooldown_s
            self._hold_deadline = now + self.max_hold_s
            return orders

        # SELL pickoff
        if disloc_dn >= edge:
            strength = _clip(disloc_dn / (edge * 3.0), 0.0, 1.0)
            notional = min(self.capital * self.base_notional_frac * (0.35 + 0.9 * strength), can_add)
            qty = max(1.0, notional / best_bid)

            o = Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=OrderSide.ASK,
                volume=float(qty),
                price=None,
                order_type=OrderType.MARKET,
                ttl=None,
                metadata=self._mk_md(self.fast_latency_ms, self.fast_jitter_ms),
            )
            orders.append(o)

            self._cooldown_until = now + self.cooldown_s
            self._hold_deadline = now + self.max_hold_s
            return orders

        return orders

    def _maybe_unwind(self, now: float, best_bid: float, best_ask: float, fair: float) -> List[Order]:
        """
        If we have inventory:
          - place/refresh small limit to exit
          - if holding too long, cross the spread with MARKET to flatten
        """
        if self.pos_qty == 0.0:
            # cancel stale exit if any
            if self._exit_order_id is not None:
                return [self._cancel(self._exit_order_id)]
            self._exit_order_id = None
            self._exit_px = None
            return []

        orders: List[Order] = []

        # hard timeout -> flatten by market
        if self._hold_deadline > 0 and now >= self._hold_deadline:
            side = OrderSide.ASK if self.pos_qty > 0 else OrderSide.BID
            qty = abs(self.pos_qty)

            # cancel exit first (optional)
            if self._exit_order_id is not None:
                orders.append(self._cancel(self._exit_order_id))
                self._exit_order_id = None
                self._exit_px = None

            orders.append(
                Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=float(qty),
                    price=None,
                    order_type=OrderType.MARKET,
                    ttl=None,
                    metadata=self._mk_md(self.fast_latency_ms, self.fast_jitter_ms),
                )
            )
            self._cooldown_until = now + self.cooldown_s
            self._hold_deadline = 0.0
            return orders

        # normal exit via limit
        if self.pos_qty > 0:
            # sell to exit
            px = max(best_ask, fair + self.take_ticks * TICK)
            side = OrderSide.ASK
        else:
            # buy to exit short
            px = min(best_bid, fair - self.take_ticks * TICK)
            side = OrderSide.BID

        # refresh if price moved
        if self._exit_order_id is not None and self._exit_px is not None:
            if abs(px - self._exit_px) >= 0.5 * TICK:
                orders.append(self._cancel(self._exit_order_id))
                self._exit_order_id = None
                self._exit_px = None

        if self._exit_order_id is None:
            oid = str(uuid.uuid4())
            self._exit_order_id = oid
            self._exit_px = px
            orders.append(
                Order(
                    order_id=oid,
                    agent_id=self.agent_id,
                    side=side,
                    volume=float(abs(self.pos_qty)),
                    price=float(round(px, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=None,
                    metadata=self._mk_md(self.fast_latency_ms + 1, self.fast_jitter_ms, ttl_ms=900),
                )
            )

        return orders

    def _cancel(self, target_order_id: str) -> Order:
        # IMPORTANT: to cancel you send an Order with order_type=CANCEL and order_id=target id
        return Order(
            order_id=str(target_order_id),
            agent_id=self.agent_id,
            side=OrderSide.BID,
            volume=0.0,
            price=None,
            order_type=OrderType.CANCEL,
            ttl=None,
            metadata=self._mk_md(self.fast_latency_ms, self.fast_jitter_ms),
        )
