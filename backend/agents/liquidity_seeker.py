# liquidity_seeker.py
# LiquiditySeeker — market-only aggressor (FX-style)
#
# Engine compatibility:
# - server.py calls: agent.generate_orders(order_book, order_book.market_context)
# - Keep signature: generate_orders(self, order_book, market_context=None, **kwargs)
# - Fills: on_order_filled(order_id, price, qty, side, slippage=0.0)
#
# This agent deliberately does NOT place LIMIT orders.
# It only contributes MARKET orders to fix the limit/market aggressiveness ratio.

import uuid
import random
import time
from dataclasses import dataclass
from collections import deque
from typing import List, Optional, Dict, Any

from backend.core.order import Order, OrderSide, OrderType, TICK


def _ctx_now(ctx) -> float:
    v = getattr(ctx, "now_ts", None)
    if v is None:
        return time.time()
    try:
        return float(v)
    except Exception:
        return time.time()


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _safe_mid(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if best_bid is None and best_ask is None:
        return None
    if best_bid is None:
        return float(best_ask)
    if best_ask is None:
        return float(best_bid)
    return 0.5 * (float(best_bid) + float(best_ask))


def _snapshot_top(order_book, depth: int = 10) -> Optional[Dict[str, Any]]:
    try:
        snap = order_book.get_order_book_snapshot(depth=depth)
    except Exception:
        return None

    bids = snap.get("bids") or []
    asks = snap.get("asks") or []

    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    mid = _safe_mid(best_bid, best_ask)
    if mid is None:
        return None

    spread = 0.0
    if best_bid is not None and best_ask is not None:
        spread = float(best_ask - best_bid)

    return {
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": float(mid),
        "spread": float(spread),
    }


def _tape_stats(order_book, now: float, window_s: float = 8.0) -> Dict[str, float]:
    """Very lightweight tape features from order_book.trade_history."""
    try:
        trades = list(order_book.trade_history)[-400:]
    except Exception:
        trades = []

    buy_notional = 0.0
    sell_notional = 0.0
    buy_qty = 0.0
    sell_qty = 0.0

    for t in reversed(trades):
        ts = float(t.get("timestamp", now))
        if now - ts > window_s:
            break
        px = float(t.get("price", 0.0) or 0.0)
        q = float(t.get("volume", 0.0) or 0.0)
        side = str(t.get("taker_side", "")).lower()
        if side in ("buy", "bid"):
            buy_notional += px * q
            buy_qty += q
        elif side in ("sell", "ask"):
            sell_notional += px * q
            sell_qty += q

    tot = buy_notional + sell_notional
    imb = 0.0 if tot <= 0 else (buy_notional - sell_notional) / tot  # [-1..1]

    return {
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "imb": float(imb),
        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "tot_notional": tot,
    }


def _compute_gap_score(levels: List[Dict[str, Any]], mid: float, max_levels: int = 10) -> float:
    """Detects holes in near-book: missing levels or large tick gaps.
    Returns score ~ [0..1.5]. Higher => more gappy.
    """
    if not levels:
        return 1.2

    n = min(len(levels), max_levels)
    ps = [float(levels[i]["price"]) for i in range(n)]

    gaps = []
    for i in range(1, len(ps)):
        gaps.append(abs(ps[i] - ps[i - 1]))

    if not gaps:
        return 0.0

    tick = max(TICK, 1e-9)
    gap_ticks = [g / tick for g in gaps]

    big = sum(1.0 for gt in gap_ticks if gt >= 2.0)
    very_big = sum(1.0 for gt in gap_ticks if gt >= 5.0)

    first = ps[0]
    dist_ticks = abs(first - mid) / tick

    score = 0.15 * (sum(gap_ticks) / max(1.0, float(len(gap_ticks))))
    score += 0.20 * big
    score += 0.35 * very_big
    score += 0.02 * dist_ticks

    return float(_clip(score, 0.0, 1.5))


def _top_liq_notional(levels: List[Dict[str, Any]], k: int = 3) -> float:
    s = 0.0
    for lvl in (levels or [])[:k]:
        p = float(lvl.get("price", 0.0) or 0.0)
        v = float(lvl.get("volume", 0.0) or 0.0)
        s += p * v
    return float(s)


@dataclass
class _LSProfileState:
    name: str
    seed: int
    min_interval_s: float
    max_interval_s: float
    burst_min: int
    burst_max: int
    min_slice_notional: float
    max_slice_notional: float
    thin_mult: float
    gap_ratio: float


class LiquiditySeeker:
    """Market-only liquidity seeker with 3 internal profiles (async personalities)."""

    def __init__(self, agent_id: str, capital: float):
        self.agent_id = str(agent_id)
        self.capital = float(capital)

        self.position = 0.0
        self.cash = 0.0

        self._last_mid: Optional[float] = None
        self._recent_mids = deque(maxlen=256)

        base_seed = hash((self.agent_id, "ls")) & 0xFFFFFFFF

        # CALIB: 1 lot = 11,240 contracts. EUR retail: 0.01-0.05 lot = 112-562 contracts.
        # p1 (micro_snipe) targets small flow: 0.005-0.03 lot range.
        # At capital=300M: 300M * 0.00014 = 42k = 420 contracts = 0.037 lot ✓
        self._p1 = _LSProfileState(
            name="micro_snipe",
            seed=base_seed ^ 0xA531,
            min_interval_s=0.08,
            max_interval_s=0.30,           # CALIB: was 0.35 → slightly faster
            burst_min=1,
            burst_max=3,                   # CALIB: was 2
            min_slice_notional=self.capital * 0.00014,   # CALIB: was 0.00006 (2.3x)
            max_slice_notional=self.capital * 0.00080,   # CALIB: was 0.00035 (2.3x)
            thin_mult=0.95,
            gap_ratio=0.12,
        )

        # CALIB: p2 (opportunistic) — EUR mid-tier: 0.05-0.3 lot = 562-3372 contracts.
        # At 300M capital: 300M * 0.0015 = 450k = 4500 contracts = 0.4 lot ✓ (upper bound)
        self._p2 = _LSProfileState(
            name="opportunistic",
            seed=base_seed ^ 0xB19F,
            min_interval_s=0.18,
            max_interval_s=0.75,           # CALIB: was 0.85
            burst_min=1,
            burst_max=4,                   # CALIB: was 3
            min_slice_notional=self.capital * 0.00023,   # CALIB: was 0.00010 (2.3x)
            max_slice_notional=self.capital * 0.00150,   # CALIB: was 0.00065 (2.3x)
            thin_mult=0.80,
            gap_ratio=0.20,
        )

        # CALIB: p3 (rare_heavy) — EUR large block: 0.5-3 lot = 5620-33720 contracts.
        # At 300M capital: 300M * 0.0028 = 840k = 8400 contracts = 0.75 lot ✓
        self._p3 = _LSProfileState(
            name="rare_heavy",
            seed=base_seed ^ 0xC02D,
            min_interval_s=0.50,
            max_interval_s=2.20,           # CALIB: was 2.50
            burst_min=1,
            burst_max=5,                   # CALIB: was 4
            min_slice_notional=self.capital * 0.00045,   # CALIB: was 0.00018 (2.5x)
            max_slice_notional=self.capital * 0.00280,   # CALIB: was 0.00110 (2.5x)
            thin_mult=0.62,
            gap_ratio=0.30,
        )

        self._profiles = [self._p1, self._p2, self._p3]

        self._next_ts: Dict[str, float] = {}
        self._burst_left: Dict[str, int] = {}
        self._last_profile_side: Dict[str, Optional[OrderSide]] = {}

        now = time.time()
        for p in self._profiles:
            rnd = random.Random(p.seed)
            self._next_ts[p.name] = now + rnd.uniform(p.min_interval_s, p.max_interval_s)
            self._burst_left[p.name] = 0
            self._last_profile_side[p.name] = None

    def perceive_market(self, market_context=None):
        return "liquidity_seeker_v1"

    def restore_capital(self):
        pass

    def on_order_filled(self, order_id, price, qty, side, slippage: float = 0.0):
        if price is None or qty is None:
            return
        try:
            px = float(price)
            q = float(qty)
        except Exception:
            return
        if q <= 0:
            return

        if side == OrderSide.BID:
            self.position += q
            self.cash -= px * q
        elif side == OrderSide.ASK:
            self.position -= q
            self.cash += px * q

    def _risk_bias(self, mid: float) -> float:
        if mid <= 0:
            return 0.0
        inv_notional = self.position * mid
        frac = inv_notional / max(self.capital, 1.0)
        return float(_clip(-frac / 0.02, -1.0, 1.0))

    def _choose_side(
        self,
        rnd: random.Random,
        tape_imb: float,
        gap_bid: float,
        gap_ask: float,
        risk_bias: float,
    ) -> OrderSide:
        # asks more gappy => easier to sweep up => BUY
        gap_delta = gap_ask - gap_bid  # >0 => BUY tilt

        score = 0.0
        score += 0.55 * _clip(gap_delta / 1.0, -1.0, 1.0)
        score += 0.35 * _clip(tape_imb / 0.65, -1.0, 1.0)
        score += 0.45 * _clip(risk_bias, -1.0, 1.0)

        p_buy = 0.50 + 0.30 * _clip(score, -1.0, 1.0)
        p_buy = _clip(p_buy, 0.15, 0.85)
        return OrderSide.BID if rnd.random() < p_buy else OrderSide.ASK

    def _slice_qty(self, rnd: random.Random, mid: float, p: _LSProfileState, thick_book: bool) -> float:
        if mid <= 0:
            return 0.0
        lo = max(1.0, float(p.min_slice_notional))
        hi = max(lo, float(p.max_slice_notional))
        notional = rnd.uniform(lo, hi)
        if thick_book:
            notional *= p.thin_mult

        notional = min(notional, self.capital * 0.0025)
        notional = max(notional, self.capital * 0.00002)

        qty = notional / mid
        return float(max(1.0, qty))

    def _should_act(self, rnd: random.Random, p: _LSProfileState, snap: Dict[str, Any]) -> bool:
        bids = snap["bids"]
        asks = snap["asks"]
        mid = snap["mid"]

        gap_bid = _compute_gap_score(bids, mid)
        gap_ask = _compute_gap_score(asks, mid)

        top_bid = _top_liq_notional(bids, k=3)
        top_ask = _top_liq_notional(asks, k=3)
        top_tot = top_bid + top_ask
        thick = (top_tot / max(self.capital, 1.0)) > 0.010

        spread = float(snap.get("spread", 0.0) or 0.0)
        spread_ticks = spread / max(TICK, 1e-9)

        gappy = max(gap_bid, gap_ask)

        base = 0.10 + 0.55 * _clip(gappy / 1.2, 0.0, 1.0)
        base += 0.08 * _clip(spread_ticks / 3.0, 0.0, 1.0)
        if thick:
            base *= 0.55

        base *= (0.75 + 0.9 * (gappy >= (p.gap_ratio * 1.5)))
        p_act = _clip(base, 0.02, 0.65)
        return rnd.random() < p_act

    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        now = _ctx_now(market_context)

        snap = _snapshot_top(order_book, depth=12)
        if snap is None:
            return []

        mid = float(snap["mid"])
        self._last_mid = mid
        self._recent_mids.append(mid)

        tape = _tape_stats(order_book, now, window_s=8.0)
        tape_imb = float(tape["imb"])  # [-1..1]

        bids = snap["bids"]
        asks = snap["asks"]
        gap_bid = _compute_gap_score(bids, mid)
        gap_ask = _compute_gap_score(asks, mid)

        thick_book = (_top_liq_notional(bids, 3) + _top_liq_notional(asks, 3)) / max(self.capital, 1.0) > 0.010
        risk_bias = self._risk_bias(mid)

        out: List[Order] = []

        for p in self._profiles:
            if now < self._next_ts[p.name]:
                continue

            rnd = random.Random(p.seed ^ (int(now * 1000) & 0xFFFFFFFF))

            if self._burst_left[p.name] <= 0:
                if not self._should_act(rnd, p, snap):
                    self._next_ts[p.name] = now + rnd.uniform(p.min_interval_s, p.max_interval_s)
                    continue
                self._burst_left[p.name] = rnd.randint(p.burst_min, p.burst_max)

            prev_side = self._last_profile_side.get(p.name)
            if prev_side is not None and rnd.random() < 0.55:
                side = prev_side
            else:
                side = self._choose_side(rnd, tape_imb, gap_bid, gap_ask, risk_bias)

            self._last_profile_side[p.name] = side

            qty = self._slice_qty(rnd, mid, p, thick_book=thick_book)

            max_qty = max(1.0, (self.capital * 0.012) / max(mid, 1e-9))
            qty = float(min(qty, max_qty))

            out.append(
                Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=float(qty),
                    price=None,
                    order_type=OrderType.MARKET,
                    ttl=None,
                    metadata={
                        "ls_profile": p.name,
                        "ls_gap_bid": float(gap_bid),
                        "ls_gap_ask": float(gap_ask),
                        "ls_tape_imb": float(tape_imb),
                    },
                )
            )

            self._burst_left[p.name] = max(0, int(self._burst_left[p.name]) - 1)

            if self._burst_left[p.name] > 0:
                dt = rnd.uniform(p.min_interval_s * 0.55, p.min_interval_s * 1.10)
            else:
                dt = rnd.uniform(p.min_interval_s, p.max_interval_s)
            self._next_ts[p.name] = now + max(0.01, float(dt))

        return out