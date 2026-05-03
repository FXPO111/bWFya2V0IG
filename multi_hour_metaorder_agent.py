from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from order import Order, OrderSide, OrderType, TICK, quant


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _now(ctx) -> float:
    v = getattr(ctx, "now_ts", None)
    return float(v) if v is not None else time.time()

def _round_px(px: float) -> float:
    # Используем quant() из движка — гарантирует попадание на сетку TICK
    return quant(float(px))

def _best_bid_ask(ob) -> Tuple[Optional[float], Optional[float]]:
    try:
        bb = ob._best_bid_price()
        ba = ob._best_ask_price()
        return (float(bb) if bb is not None else None,
                float(ba) if ba is not None else None)
    except Exception:
        return (None, None)

def _spread_ticks(bid: float, ask: float) -> int:
    return max(1, int(round((ask - bid) / max(TICK, 1e-12))))

def _mid(bid: float, ask: float) -> float:
    return 0.5 * (bid + ask)

def _bbo_sizes(ob) -> Tuple[float, float]:
    try:
        b = float(ob._best_bid_size())
    except Exception:
        b = 0.0
    try:
        a = float(ob._best_ask_size())
    except Exception:
        a = 0.0
    return max(0.0, b), max(0.0, a)

def _top_depth_notional(ob, side: OrderSide, k: int = 3) -> float:
    try:
        snap = ob.get_order_book_snapshot(depth=max(6, k)) or {}
    except Exception:
        return 0.0
    # snapshot возвращает 'volume', не 'qty'
    lvls = snap.get("bids" if side == OrderSide.BID else "asks", []) or []
    tot = 0.0
    for lv in lvls[:k]:
        try:
            p = float(lv.get("price", 0.0))
            v = float(lv.get("volume", 0.0))
        except Exception:
            continue
        if p > 0 and v > 0:
            tot += p * v
    return tot

def _recent_trades(ob, now: float, lookback_s: float) -> List[Dict[str, Any]]:
    th = getattr(ob, "trade_history", None)
    if not th:
        return []
    cut = now - lookback_s
    out = []
    for t in reversed(th):
        try:
            ts = float(t.get("timestamp", 0.0))
        except Exception:
            ts = 0.0
        if ts < cut:
            break
        out.append(t)
    out.reverse()
    return out

def _recent_notional(ob, now: float, lookback_s: float) -> float:
    tot = 0.0
    for t in _recent_trades(ob, now, lookback_s):
        try:
            px = float(t.get("price", 0.0))
            v = float(t.get("volume", 0.0))
        except Exception:
            continue
        if px > 0 and v > 0:
            tot += px * v
    return tot

def _taker_imbalance_10s_q95(ob, now: float, lookback_s: float = 180.0) -> float:
    """Q95 по |net taker volume| в 10s-бакетах за окно lookback_s."""
    trades = _recent_trades(ob, now, lookback_s)
    if not trades:
        return 0.0
    buckets: Dict[int, float] = {}
    for t in trades:
        try:
            ts = float(t.get("timestamp", 0.0))
            v = float(t.get("volume", 0.0))
            side = str(t.get("taker_side", "")).lower()
        except Exception:
            continue
        b = int(ts // 10) * 10
        sgn = 1.0 if side in ("buy", "bid") else -1.0
        buckets[b] = buckets.get(b, 0.0) + sgn * v
    arr = [abs(x) for x in buckets.values() if abs(x) > 0]
    if not arr:
        return 0.0
    arr.sort()
    idx = int(0.95 * (len(arr) - 1))
    return float(arr[idx])

def _price_change_ticks(a: float, b: float) -> int:
    if not (math.isfinite(a) and math.isfinite(b)):
        return 0
    return int(round((b - a) / max(TICK, 1e-12)))


# ──────────────────────────────────────────────────────────────
# Agent state
# ──────────────────────────────────────────────────────────────

@dataclass
class _Live:
    order_id: str
    side: OrderSide
    price: float
    created_ts: float
    kind: str       # "passive"  (aggr не используется — market ордера _live не ставят)
    total_qty: float
    filled_qty: float = 0.0

_PROFILES: Dict[str, Dict[str, Any]] = {
    "pov": {
        "decision_min_s":        (1.2, 2.4),
        "session_min":           (45, 140),
        "p_passive":             (0.55, 0.75),
        "pov_lo_hi":             (0.10, 0.22),
        "burst_uimb_lo_hi":      (0.55, 1.05),
        "deadline_boost":        (1.10, 1.35),
        "max_consecutive_aggr":  (2, 4),
        "spread_stop_ticks":     (3, 5),
        "bbo_min_qty":           (18, 35),
        "displacement_pause_ticks": (4, 8),
    },
    "twap": {
        "decision_min_s":        (1.4, 3.1),
        "session_min":           (60, 190),
        "p_passive":             (0.70, 0.88),
        "pov_lo_hi":             (0.06, 0.14),
        "burst_uimb_lo_hi":      (0.30, 0.70),
        "deadline_boost":        (1.05, 1.20),
        "max_consecutive_aggr":  (1, 3),
        "spread_stop_ticks":     (2, 4),
        "bbo_min_qty":           (22, 45),
        "displacement_pause_ticks": (3, 7),
    },
    "deadline": {
        "decision_min_s":        (1.0, 2.0),
        "session_min":           (35, 95),
        "p_passive":             (0.40, 0.65),
        "pov_lo_hi":             (0.12, 0.26),
        "burst_uimb_lo_hi":      (0.70, 1.35),
        "deadline_boost":        (1.25, 1.70),
        "max_consecutive_aggr":  (1, 3),
        "spread_stop_ticks":     (3, 6),
        "bbo_min_qty":           (20, 40),
        "displacement_pause_ticks": (5, 10),
    },
}


class MultiHourMetaOrderAgent:
    """
    Мезо-агент (35–190 мин): исполняет мета-ордера child-ордерами.
    Совместим с движком order_book.py / server.py:
      - generate_orders(order_book, market_context) → List[Order]
      - on_order_filled(order_id, price, qty, side)
      - loop_interval — уважается server.agents_loop

    ИЗМЕНЕНИЯ по сравнению с оригиналом:
      [FIX-1] cross-limit → MARKET-ордер:
              устраняет «resting bid/ask at ask/bid», bid==ask, spread=0 на весь
              цикл между add_order и следующим tick().
      [FIX-2] inside improvement только при spread_t >= 3 + p снижена 0.45→0.20
              + явная anti-lock проверка: выставляем только если новый px
              не равен противоположному BBO.
      [FIX-3] сторона тикета: убран momentum-bias 0.55→pure random 0.50,
              чтобы три экземпляра не коррелировали в одну сторону.
      [FIX-4] _live не выставляется для MARKET-ордеров — нет смысла
              отслеживать их как «живые» (исполняются синхронно).
      [FIX-5] quant() из движка для всех цен — гарантирует попадание на TICK-сетку.
    """

    def __init__(self, agent_id: str, capital: float,
                 profile: str = "pov", seed: Optional[int] = None):
        self.agent_id = agent_id
        self.capital = float(capital)
        self.profile = profile if profile in _PROFILES else "pov"
        if seed is not None:
            random.seed(int(seed))

        p = _PROFILES[self.profile]
        self.decision_min_s     = random.uniform(*p["decision_min_s"])
        self.session_minutes    = random.randint(*p["session_min"])
        self.p_passive          = random.uniform(*p["p_passive"])
        self.pov_lo, self.pov_hi = p["pov_lo_hi"]
        self.burst_uimb_lo, self.burst_uimb_hi = p["burst_uimb_lo_hi"]
        self.deadline_boost_lo, self.deadline_boost_hi = p["deadline_boost"]
        self.max_consecutive_aggr = random.randint(*p["max_consecutive_aggr"])
        self.spread_stop_ticks  = random.randint(*p["spread_stop_ticks"])
        self.bbo_min_qty        = random.uniform(*p["bbo_min_qty"])
        self.displacement_pause_ticks = random.randint(*p["displacement_pause_ticks"])

        # state
        self.active             = False
        self.side: Optional[OrderSide] = None
        self.start_ts           = 0.0
        self.end_ts             = 0.0
        self.target_notional    = 0.0
        self.executed_notional  = 0.0

        self._seq               = 0
        self._live: Optional[_Live] = None
        self._last_decision_ts  = 0.0
        self._next_allowed_ts   = 0.0
        self._consecutive_aggr  = 0
        self._last_aggr_ts      = 0.0
        self._last_mid: Optional[float] = None
        self._last_mid_ts: float = 0.0
        self._uimb              = 0.0
        self._uimb_ts           = 0.0

        # server.agents_loop уважает loop_interval
        self.loop_interval = random.uniform(0.7, 1.2)

    # ── engine hooks ─────────────────────────────────────────

    def restore_capital(self):
        pass

    def perceive_market(self, market_context):
        return f"meta_{self.profile}"

    def on_order_filled(self, order_id: str, price, qty, side,
                        slippage: float = 0.0):
        """Вызывается из notify_agent_fill в server.py."""
        try:
            px = float(price)
            q  = float(qty)
        except Exception:
            return
        if px <= 0 or q <= 0:
            return
        self.executed_notional += px * q

        if self._live and self._live.order_id == order_id:
            self._live.filled_qty += q
            if self._live.filled_qty >= 0.98 * self._live.total_qty:
                self._live = None

        if self.active and self.executed_notional >= 0.999 * self.target_notional:
            self.active = False
            self._live  = None
            self._consecutive_aggr = 0

    def generate_orders(self, order_book, market_context=None,
                        **kwargs) -> List[Order]:
        now = _now(market_context)

        # decision gate
        if self._next_allowed_ts and now < self._next_allowed_ts:
            return []
        if self._last_decision_ts and (now - self._last_decision_ts) < self.decision_min_s:
            return []

        bid, ask = _best_bid_ask(order_book)
        if bid is None or ask is None:
            return []

        mid       = _mid(bid, ask)
        spread_t  = _spread_ticks(bid, ask)
        bbo_bid_sz, bbo_ask_sz = _bbo_sizes(order_book)

        # трекинг смещения mid на ~20s шкале
        if self._last_mid is None or (now - self._last_mid_ts) > 20.0:
            self._last_mid    = mid
            self._last_mid_ts = now

        # обновление U_imb каждые ~15s
        if (now - self._uimb_ts) > 15.0 or self._uimb <= 0.0:
            u = _taker_imbalance_10s_q95(order_book, now, lookback_s=180.0)
            if u <= 0.0:
                rn = _recent_notional(order_book, now, 60.0)
                u  = max(5.0, 0.12 * (rn / max(mid, 1e-9)))
            self._uimb    = float(u)
            self._uimb_ts = now

        # запуск нового тикета если idle
        if (not self.active) and (random.random() < 0.03):
            self._start_new_ticket(order_book, market_context, now, mid)
            self._last_decision_ts = now
            self._next_allowed_ts  = now + random.uniform(3.0, 12.0)
            return []

        if not self.active or self.side is None:
            self._last_decision_ts = now
            return []

        # hard stop: тикет истёк
        if now >= self.end_ts:
            out = []
            if self._live:
                out.append(self._cancel(self._live.order_id))
            self._live             = None
            self.active            = False
            self._consecutive_aggr = 0
            self._last_decision_ts = now
            return out

        # ── чистка устаревшего/сдвинутого пассива ─────────
        out: List[Order] = []
        if self._live and self._live.kind == "passive":
            stale_age = random.uniform(5.0, 11.0)
            if (now - self._live.created_ts) > stale_age:
                out.append(self._cancel(self._live.order_id))
                self._live             = None
                self._last_decision_ts = now
                self._next_allowed_ts  = now + random.uniform(1.5, 4.0)
                return out

            desired = bid if self.side == OrderSide.BID else ask
            if abs(desired - self._live.price) >= 2 * TICK:
                out.append(self._cancel(self._live.order_id))
                self._live             = None
                self._last_decision_ts = now
                self._next_allowed_ts  = now + random.uniform(1.0, 3.0)
                return out

        # ── circuit-breaker 1: широкий спред → только пассив ──
        if spread_t >= self.spread_stop_ticks:
            if self._live:
                self._last_decision_ts = now
                self._next_allowed_ts  = now + random.uniform(1.8, 4.5)
                return []
            qty = self._small_passive_qty(mid)
            px  = bid if self.side == OrderSide.BID else ask
            out.append(self._limit(self.side, px, qty, tag="meta_passive_spread"))
            self._live = _Live(out[-1].order_id, self.side,
                               _round_px(px), now, "passive", qty)
            self._last_decision_ts = now
            self._next_allowed_ts  = now + random.uniform(2.0, 5.0)
            return out

        # ── circuit-breaker 2: тонкий противоположный BBO ──
        contra_bbo = bbo_ask_sz if self.side == OrderSide.BID else bbo_bid_sz
        if contra_bbo < self.bbo_min_qty:
            if self._live:
                out.append(self._cancel(self._live.order_id))
                self._live = None
            self._last_decision_ts = now
            self._next_allowed_ts  = now + random.uniform(2.5, 7.0)
            self._consecutive_aggr = 0
            return out

        # ── circuit-breaker 3: слишком быстрое смещение в нашу сторону ──
        disp = _price_change_ticks(self._last_mid or mid, mid)
        if (self.side == OrderSide.ASK
                and disp <= -self.displacement_pause_ticks
                and (now - self._last_mid_ts) <= 22.0):
            if self._live:
                out.append(self._cancel(self._live.order_id))
                self._live = None
            self._last_decision_ts = now
            self._next_allowed_ts  = now + random.uniform(3.0, 9.0)
            self._consecutive_aggr = 0
            self._last_mid         = mid
            self._last_mid_ts      = now
            return out
        if (self.side == OrderSide.BID
                and disp >= self.displacement_pause_ticks
                and (now - self._last_mid_ts) <= 22.0):
            if self._live:
                out.append(self._cancel(self._live.order_id))
                self._live = None
            self._last_decision_ts = now
            self._next_allowed_ts  = now + random.uniform(3.0, 9.0)
            self._consecutive_aggr = 0
            self._last_mid         = mid
            self._last_mid_ts      = now
            return out

        # ── основная логика ────────────────────────────────
        rem = max(0.0, self.target_notional - self.executed_notional)
        if rem <= 0.0:
            self.active            = False
            self._live             = None
            self._consecutive_aggr = 0
            self._last_decision_ts = now
            return out

        total_s  = max(self.end_ts - self.start_ts, 1.0)
        prog     = max(0.0, min(1.0, (now - self.start_ts) / total_s))
        expected = self.target_notional * prog
        gap      = expected - self.executed_notional   # > 0 → отстаём

        recent60 = _recent_notional(order_book, now, 60.0)
        if recent60 <= 0.0:
            recent60 = mid * 10.0

        pov         = random.uniform(self.pov_lo, self.pov_hi)
        uimb        = max(1.0, self._uimb)
        burst_notional = uimb * random.uniform(self.burst_uimb_lo, self.burst_uimb_hi) * mid

        remain_s    = max(self.end_ts - now, 1.0)
        deadline_frac = 1.0 - (remain_s / total_s)
        if deadline_frac > 0.75:
            burst_notional *= random.uniform(self.deadline_boost_lo, self.deadline_boost_hi)

        if gap > 0:
            burst_notional *= 1.0 + min(0.35, (gap / max(self.target_notional, 1e-9)) * 2.5)

        allow_notional = pov * recent60 * (self.decision_min_s / 60.0)
        burst_notional = min(burst_notional, allow_notional * 2.2)
        burst_notional = min(burst_notional, rem)
        burst_notional = max(0.0, burst_notional)

        qty = burst_notional / mid
        if qty < 1.0:
            if not self._live and random.random() < 0.6:
                px = bid if self.side == OrderSide.BID else ask
                q2 = self._small_passive_qty(mid)
                out.append(self._limit(self.side, px, q2, tag="meta_passive_small"))
                self._live = _Live(out[-1].order_id, self.side,
                                   _round_px(px), now, "passive", q2)
            self._last_decision_ts = now
            self._next_allowed_ts  = now + random.uniform(1.2, 3.0)
            return out

        qty = float(round(max(1.0, qty), 2))

        behind   = gap > 0.06 * self.target_notional
        p_passive = self.p_passive
        if behind:
            p_passive = max(0.15, p_passive - 0.22)
        if (now - self._last_aggr_ts) < random.uniform(3.0, 7.0):
            p_passive = min(0.92, p_passive + 0.18)

        # уже есть живой пассив — не стакуем
        if self._live:
            self._last_decision_ts = now
            self._next_allowed_ts  = now + random.uniform(1.1, 2.5)
            return out

        do_passive = (random.random() < p_passive) or \
                     (self._consecutive_aggr >= self.max_consecutive_aggr)

        if do_passive:
            px = bid if self.side == OrderSide.BID else ask

            # ── [FIX-2] inside improvement ────────────────────────────
            # Только если spread >= 3 тиков, p=0.20, и новый px НЕ создаёт
            # locked market (bid_new != ask, ask_new != bid).
            if spread_t >= 3 and random.random() < 0.20:
                if self.side == OrderSide.BID:
                    candidate = _round_px(ask - TICK)
                    # anti-lock: убеждаемся, что не равно ask
                    if candidate < ask - TICK * 0.5:
                        px = candidate
                else:
                    candidate = _round_px(bid + TICK)
                    if candidate > bid + TICK * 0.5:
                        px = candidate
            # ── конец FIX-2 ───────────────────────────────────────────

            out.append(self._limit(self.side, px, qty, tag="meta_passive"))
            self._live = _Live(out[-1].order_id, self.side,
                               _round_px(px), now, "passive", qty)
            self._next_allowed_ts  = now + random.uniform(2.2, 6.0)
            self._consecutive_aggr = max(0, self._consecutive_aggr - 1)

        else:
            # ── [FIX-1] агрессивный путь: MARKET вместо cross-limit ──
            # cross-limit ставил bid по ask (или ask по bid), что создавало
            # crossed book до следующего tick(). MARKET исполняется синхронно
            # внутри add_order(), книга никогда не остаётся crossed.
            out.append(self._market(self.side, qty))
            # [FIX-4] _live НЕ ставим: market исполняется сразу, нечего трекать.
            # on_order_filled обновит executed_notional через notify_agent_fill.

            self._consecutive_aggr += 1
            self._last_aggr_ts      = now
            # mandatory cooldown после агрессии
            self._next_allowed_ts   = now + random.uniform(3.5, 9.5)

        self._last_decision_ts = now
        return out

    # ── internals ─────────────────────────────────────────────

    def _start_new_ticket(self, ob, ctx, now: float, mid: float) -> None:
        # Считаем taker-imbalance за последние 35 секунд
        imb = 0.0
        for t in _recent_trades(ob, now, 35.0):
            try:
                v    = float(t.get("volume", 0.0))
                side = str(t.get("taker_side", "")).lower()
            except Exception:
                continue
            imb += (v if side in ("buy", "bid") else -v)

        # [FIX-3] убираем momentum-bias: p=0.50 (чистая монета).
        # Оригинал использовал 0.55 в сторону imbalance, из-за чего три
        # одновременных экземпляра коррелировали в одну сторону и давали
        # самоусиливающееся направленное движение.
        if random.random() < 0.50:
            self.side = OrderSide.BID if imb >= 0 else OrderSide.ASK
        else:
            self.side = OrderSide.ASK if imb >= 0 else OrderSide.BID

        self.start_ts          = now
        self.end_ts            = now + float(self.session_minutes) * 60.0
        self.executed_notional = 0.0
        self._consecutive_aggr = 0
        self._live             = None

        recent60 = _recent_notional(ob, now, 60.0)
        if recent60 <= 0.0:
            recent60 = mid * 50.0

        base_part = {
            "twap":     (0.06, 0.12),
            "pov":      (0.10, 0.18),
            "deadline": (0.12, 0.22),
        }[self.profile]
        part = random.uniform(*base_part)

        notional_per_min = recent60  # уже notional за 60 секунд
        target = part * notional_per_min * float(self.session_minutes)

        cap    = max(1.0, self.capital)
        target = min(target, 0.15 * cap)
        target = max(target, 0.002 * cap)

        self.target_notional = float(target)
        self.active          = True
        self._last_mid       = mid
        self._last_mid_ts    = now

    def _small_passive_qty(self, mid: float) -> float:
        q = max(1.0, round(0.12 * max(1.0, self._uimb), 2))
        return float(min(q, 35.0))

    def _oid(self) -> str:
        self._seq += 1
        return f"{self.agent_id}_{self._seq}"

    def _market(self, side: OrderSide, qty: float) -> Order:
        o = Order(self._oid(), self.agent_id, side,
                  float(qty), None, OrderType.MARKET, None)
        setattr(o, "metadata", {"tag": f"meta_{self.profile}_mkt"})
        return o

    def _limit(self, side: OrderSide, price: float, qty: float,
               tag: str) -> Order:
        o = Order(self._oid(), self.agent_id, side,
                  float(qty), _round_px(price), OrderType.LIMIT, None)
        setattr(o, "metadata", {"tag": tag, "profile": self.profile})
        return o

    def _cancel(self, oid: str) -> Order:
        # В движке add_order(CANCEL) пишет Event(CANCEL, oid=order.order_id),
        # поэтому order_id кансел-объекта = oid ордера, который хотим отменить.
        o = Order(oid, self.agent_id, OrderSide.BID, 0.0,
                  None, OrderType.CANCEL, None)
        setattr(o, "metadata", {"tag": f"meta_{self.profile}_cancel"})
        return o