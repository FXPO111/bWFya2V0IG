import time
import uuid
import random
import math
from collections import deque
from typing import Dict, Any, List, Optional

import numpy as np

from backend.core.order import Order, OrderSide, OrderType, quant

# ──────────────────────────────────────────────────────────────
#  Константы и базовые настройки
# ──────────────────────────────────────────────────────────────

ROLLING_PNL_SIZE   = 800
MIN_ORDER_QTY      = 10.0
DEFAULT_ANCHOR_MID = 100.0

# геометрическая кривая уровней (в "единицах спреда")
LEVEL_OFFSETS = [0.5, 1.2, 2.3, 3.8]  # 4 "колена" книги


# ──────────────────────────────────────────────────────────────
#  EMA / волатильность
# ──────────────────────────────────────────────────────────────
def _ctx_now(ctx):
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()


class Ema:
    __slots__ = ("alpha", "value", "initialized")

    def __init__(self, alpha: float, init: float = 0.0):
        self.alpha = float(alpha)
        self.value = float(init)
        self.initialized = False

    def update(self, x: float) -> float:
        x = float(x)
        if not self.initialized:
            self.value = x
            self.initialized = True
        else:
            self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value


class VolEstimator:
    """
    Оценка волатильности по лог-ретурнам:
      - r_fast: короткий горизонт (1–3 сек)
      - r_slow: длиннее (10+ сек)
    """
    __slots__ = ("prev_mid", "r_fast", "r_slow")

    def __init__(self, a_fast: float = 0.25, a_slow: float = 0.05):
        self.prev_mid: Optional[float] = None
        self.r_fast = Ema(a_fast, 0.0)
        self.r_slow = Ema(a_slow, 0.0)

    def update(self, mid: float) -> None:
        mid = float(mid)
        if self.prev_mid is None:
            self.prev_mid = mid
            return
        if mid <= 0.0 or self.prev_mid <= 0.0:
            self.prev_mid = mid
            return
        r = math.log(mid / self.prev_mid)
        self.prev_mid = mid
        self.r_fast.update(abs(r))
        self.r_slow.update(abs(r))

    def sigma1_bps(self) -> float:
        return float(self.r_fast.value * 1e4)

    def sigma10_bps(self) -> float:
        return float(self.r_slow.value * 1e4)


# ──────────────────────────────────────────────────────────────
#  Queue-модель
# ──────────────────────────────────────────────────────────────

class QueueModel:
    """
    Очень грубая queue-модель: каждому ордеру — виртуальный rank.
    rank уменьшается, когда рядом идут сделки.
    """
    __slots__ = ("ranks", "decay")

    def __init__(self):
        self.ranks: Dict[str, float] = {}
        self.decay: float = 0.985

    def register(self, oid: str, initial_rank: float) -> None:
        self.ranks[oid] = float(initial_rank)

    def forget(self, oid: str) -> None:
        self.ranks.pop(oid, None)

    def on_trade(self, price: float, side: OrderSide, active: Dict[str, Dict[str, Any]]) -> None:
        for oid, meta in active.items():
            px = meta["price"]
            dist = abs(px - price)
            r = self.ranks.get(oid, 5.0)
            if dist <= 0.015:
                r = max(0.0, r - 1.5)
            else:
                r = r / self.decay
            self.ranks[oid] = min(25.0, max(0.0, r))

    def get(self, oid: str) -> float:
        return float(self.ranks.get(oid, 5.0))


# ──────────────────────────────────────────────────────────────
#  Tier-7 AdvancedMarketMaker
# ──────────────────────────────────────────────────────────────

class AdvancedMarketMaker2:
    """
    Tier-7 LP под твою архитектуру:

    • multi-horizon alpha (short / mid)
    • regime detection (calm / trend / volatile / panic)
    • microprice + OFI + cluster flow
    • queue model
    • iceberg уровни (часть глубины скрыта)
    • adaptive latency per level / regime
    • partial level quoting (дырки в книге)
    • динамическое исчезновение LP (fade-out)
    """

    def __init__(self, agent_id: str, capital: float):
        self.agent_id = str(agent_id)
        self.capital  = float(capital)

        self.inventory: float = 0.0
        self.cash: float      = float(capital)

        # базовые market-параметры
        self.tick: float = 0.001
        self.max_spread_ticks: float = 7.0

        # состояние mid
        self.local_mid: Optional[float] = None
        self.mid_fast  = Ema(0.25, DEFAULT_ANCHOR_MID)
        self.mid_slow  = Ema(0.06, DEFAULT_ANCHOR_MID)
        self.vol       = VolEstimator()

        self.recent_mids: deque[float] = deque(maxlen=ROLLING_PNL_SIZE)

        # alpha (две шкалы)
        self.alpha_short = Ema(0.45, 0.0)
        self.alpha_mid   = Ema(0.15, 0.0)

        # microstructure signals
        self.micro_ema   = Ema(0.35, DEFAULT_ANCHOR_MID)
        self.ofi_ema     = Ema(0.25, 0.0)
        self.cluster_ema = Ema(0.4, 0.0)
        self.tox_ema     = Ema(0.18, 0.0)

        # pnl / risk
        self.pnl_hist: deque = deque(maxlen=ROLLING_PNL_SIZE)
        self.pnl_var_ema = Ema(0.20, 0.0)

        # regime
        self.regime: str = "calm"  # "calm", "trend", "volatile", "panic"

        # очереди / активные ордера
        # oid -> {side, price, volume, ts_ms, ttl_ms, near, level, iceberg}
        self.active: Dict[str, Dict[str, Any]] = {}
        self.queue = QueueModel()

        # торговая цепочка
        self.last_trade_ts: float = 0.0
        self.trade_chain_len: int = 0
        self.trade_chain_ema = Ema(0.25, 0.0)

        # block-режим (LP ушёл с рынка)
        self.lp_block_until: float = 0.0

        # сессионные параметры (hook от внешнего MarketContext)
        self.session_liq_mult: float = 1.0
        self.session_spread_bias: float = 0.0

    # ──────────────────────────────────────────────────────────
    #  Вспомогательные вычисления
    # ──────────────────────────────────────────────────────────

    def _update_state(self, mid: float, ob=None) -> None:
        mid = float(mid)
        self.recent_mids.append(mid)
        fast = self.mid_fast.update(mid)
        slow = self.mid_slow.update(mid)

        self.vol.update(mid)

        # short alpha = локальное смещение
        if len(self.recent_mids) >= 2:
            ret1 = self.recent_mids[-1] - self.recent_mids[-2]
        else:
            ret1 = 0.0
        self.alpha_short.update(ret1)

        # mid-alpha по разности fast/slow
        self.alpha_mid.update(fast - slow)

        # pnl-variance
        pnl = self.mark_to_market(mid)
        self.pnl_hist.append((getattr(self, "_now_ts", time.time()), pnl))

        if len(self.pnl_hist) >= 2:
            diff = pnl - self.pnl_hist[-2][1]
            self.pnl_var_ema.update(diff * diff)

        # snapshot для microprice / OFI
        snap = None
        if ob is not None and hasattr(ob, "get_order_book_snapshot"):
            try:
                snap = ob.get_order_book_snapshot(depth=4)
            except Exception:
                snap = None

        if snap and snap.get("bids") and snap.get("asks"):
            mp = self._compute_microprice(snap)
            self.micro_ema.update(mp)
            if hasattr(self, "_last_snapshot") and getattr(self, "_last_snapshot") is not None:
                self._update_ofi(snap, self._last_snapshot)
            self._last_snapshot = snap
        else:
            self.micro_ema.update(mid)

        # trade-history: cluster, chain, toxicity
        if ob is not None and hasattr(ob, "trade_history") and ob.trade_history:
            last = ob.trade_history[-1]
            px = float(last.get("price", mid))
            ts = float(last.get("timestamp", getattr(self, "_now_ts", time.time())))
            side_str = last.get("side", None)

            # цепочка агрессивных
            if self.last_trade_ts == 0.0:
                self.last_trade_ts = ts
            dt = ts - self.last_trade_ts
            self.last_trade_ts = ts
            if dt < 1.0:
                self.trade_chain_len += 1
            else:
                self.trade_chain_len = 1
            self.trade_chain_ema.update(self.trade_chain_len)

            # cluster (последние N направлений)
            if len(ob.trade_history) >= 6:
                s = 0.0
                for t in list(ob.trade_history)[-6:]:
                    sd = t.get("side", None)
                    if sd == "buy":
                        s += 1.0
                    elif sd == "sell":
                        s -= 1.0
                self.cluster_ema.update(s / 6.0)

            # токсичность относительно инвентаря
            inv_sign = 1.0 if self.inventory > 0.0 else (-1.0 if self.inventory < 0.0 else 0.0)
            dir_px = 1.0 if px > mid else (-1.0 if px < mid else 0.0)
            score = -inv_sign * dir_px
            self.tox_ema.update(score)

            # queue update
            side = OrderSide.BID if side_str == "sell" else OrderSide.ASK
            self.queue.on_trade(px, side, self.active)

        # режим рынка
        self._detect_regime(mid)

    def _compute_microprice(self, snap: Dict[str, Any]) -> float:
        bids = snap.get("bids") or []
        asks = snap.get("asks") or []
        if not bids or not asks:
            return float(self.local_mid or DEFAULT_ANCHOR_MID)

        b0 = bids[0]
        a0 = asks[0]
        pb = float(b0["price"])
        pa = float(a0["price"])
        qb = max(float(b0["volume"]), 1e-9)
        qa = max(float(a0["volume"]), 1e-9)
        return (pa * qb + pb * qa) / (qa + qb)

    def _update_ofi(self, snap: Dict[str, Any], prev: Dict[str, Any]) -> None:
        bids = snap.get("bids") or []
        asks = snap.get("asks") or []
        pbids = prev.get("bids") or []
        pasks = prev.get("asks") or []
        depth = min(3, len(bids), len(pbids), len(asks), len(pasks))
        if depth == 0:
            self.ofi_ema.update(0.0)
            return

        ofi = 0.0
        total = 0.0
        for i in range(depth):
            b = bids[i]
            a = asks[i]
            pb = pbids[i]
            pa = pasks[i]

            dbq = float(b["volume"]) - float(pb["volume"])
            daq = float(a["volume"]) - float(pa["volume"])
            dbp = float(b["price"])  - float(pb["price"])
            dap = float(a["price"])  - float(pa["price"])

            ofi += dbq - daq + (dbp - dap) * 40.0
            total += float(b["volume"]) + float(a["volume"])

        if total <= 0.0:
            self.ofi_ema.update(0.0)
        else:
            val = max(-1.0, min(1.0, ofi / total))
            self.ofi_ema.update(val)

    def _detect_regime(self, mid: float) -> None:
        sig1 = self.vol.sigma1_bps()
        sig10 = self.vol.sigma10_bps()
        chain = self.trade_chain_ema.value
        ofi = abs(self.ofi_ema.value)
        cl  = abs(self.cluster_ema.value)
        tox = max(0.0, self.tox_ema.value)

        # простая логика режимов
        if sig1 < 4.0 and chain < 2 and ofi < 0.3 and tox < 0.3:
            self.regime = "calm"
        elif sig1 < 6.0 and chain < 4 and ofi < 0.6:
            self.regime = "trend" if self.alpha_mid.value != 0 and abs(self.alpha_mid.value) > abs(self.alpha_short.value) else "calm"
        elif sig1 < 10.0 and (chain >= 3 or ofi >= 0.5 or cl >= 0.6):
            self.regime = "volatile"
        else:
            self.regime = "panic"

    # ──────────────────────────────────────────────────────────
    #  Спред / риск
    # ──────────────────────────────────────────────────────────

    def _compute_spread_and_skew(self, mid: float) -> (float, float, float):
        sig1 = self.vol.sigma1_bps()
        sig10 = self.vol.sigma10_bps()
        sa  = self.alpha_short.value / max(self.tick, 1e-6)
        am  = self.alpha_mid.value / max(self.tick, 1e-6)
        ofi = self.ofi_ema.value
        cl  = self.cluster_ema.value
        tox = max(0.0, self.tox_ema.value)

        base = 1.2

        # волатильность
        base += max(0.0, (sig1 - 3.0) / 25.0)
        if sig10 > 0.0:
            slope = abs(sig1 - sig10) / sig10
            base += min(0.5, slope * 0.5)

        # alpha / дисбаланс
        base += 0.3 * min(1.5, abs(sa) / 5.0)
        base += 0.3 * min(1.5, abs(am) / 8.0)
        base += 0.4 * abs(ofi)
        base += 0.3 * abs(cl)

        # токсичность / pnl-variance
        base += 0.8 * tox
        base += min(1.0, math.sqrt(self.pnl_var_ema.value) / (0.006 * self.capital + 1e-6))

        # сессия
        base *= (1.0 + self.session_spread_bias)

        # режим
        if self.regime == "calm":
            base *= 0.9
        elif self.regime == "trend":
            base *= 1.0
        elif self.regime == "volatile":
            base *= 1.1
        else:  # panic
            base *= 1.3

        # небольшой шум
        base *= (1.0 + random.uniform(-0.08, 0.08))

        base = max(0.8, min(self.max_spread_ticks, base))

        # skew (asym bid/ask)
        drift_comp = (sa / 3.0) + 0.5 * (am / 5.0) + ofi + 0.6 * cl
        drift_comp = max(-4.0, min(4.0, drift_comp))

        skew_unit = 0.18 * drift_comp  # [-0.72..0.72]

        bid_sp = base * (1.0 + skew_unit)
        ask_sp = base * (1.0 - skew_unit)

        bid_sp = max(0.8, min(self.max_spread_ticks + 0.7, bid_sp))
        ask_sp = max(0.8, min(self.max_spread_ticks + 0.7, ask_sp))

        return base, bid_sp, ask_sp

    def _risk_level(self, mid: float) -> float:
        mid = float(mid)
        sig1 = self.vol.sigma1_bps()
        inv_notional = abs(self.inventory * mid)
        pnl_var = self.pnl_var_ema.value
        tox = max(0.0, self.tox_ema.value)
        chain = self.trade_chain_ema.value

        inv_term  = min(2.0, inv_notional / (self.capital * 0.15 + 1e-6))
        vol_term  = max(0.0, (sig1 - 4.0) / 20.0)
        tox_term  = tox
        chain_term = min(1.5, chain / 5.0)
        var_term   = min(1.3, math.sqrt(pnl_var) / (0.006 * self.capital + 1e-6))

        raw = (
            0.7 * inv_term +
            0.6 * vol_term +
            0.7 * tox_term +
            0.6 * chain_term +
            0.5 * var_term
        )

        if self.regime == "panic":
            raw *= 1.2
        elif self.regime == "calm":
            raw *= 0.8

        r = 1.0 - math.exp(-max(0.0, raw))
        return max(0.05, min(0.97, r))

    # ──────────────────────────────────────────────────────────
    #  Latency / TTL / size
    # ──────────────────────────────────────────────────────────

    def _latency_profile(self, near: bool, level: int) -> (int, int):
        """
        Возвращает (latency_ms, jitter_ms)
        """
        base = 12 if near else 25
        if level >= 3:
            base += 10

        if self.regime == "calm":
            base += 5
        elif self.regime == "trend":
            base += 12
        elif self.regime == "volatile":
            base += 18
        else:  # panic
            base += 25

        jitter = 5 if near else 10
        jitter += 5 * max(0, level - 1)

        return base, jitter

    def _ttl_ms(self, near: bool, risk: float, level: int) -> int:
        if near:
            lo, hi = 350, 950
        else:
            lo, hi = 700, 2000

        k = 1.0 - 0.7 * risk
        k = max(0.25, min(1.0, k))

        span = hi - lo
        base = lo + span * 0.6 * k
        jitter = random.uniform(-0.3, 0.3) * span

        return int(max(lo, min(hi, base + jitter)))

    def _size_for_level(self, px: float, mid: float, spread_ticks: float, risk: float, near: bool, level: int) -> (float, bool, Dict[str, Any]):
        """
        Возвращает (size, is_iceberg, iceberg_meta)
        iceberg_meta: {'display_qty': ..., 'reserve_qty': ..., 'chunk': ..., 'replenish_ms': ...}
        """
        dist_ticks = abs(px - mid) / max(self.tick, 1e-6)

        # базовый notional ~0.08% капитала на дальнем уровне
        base_notional_far = self.capital * 0.002

        # уровень-фактор
        if level == 1:
            lvl_factor = 0.20
        elif level == 2:
            lvl_factor = 0.30
        elif level == 3:
            lvl_factor = 0.65
        else:
            lvl_factor = 0.85


        depth_scale = 1.0 + 0.10 * max(0.0, dist_ticks - 1.0)

        notional = base_notional_far * lvl_factor * depth_scale

        notional *= max(0.25, 1.0 - 0.6 * risk)
        notional *= self.session_liq_mult

        inv_notional = self.inventory * mid
        if inv_notional > 0.0 and px < mid:
            notional *= 0.45
        elif inv_notional < 0.0 and px > mid:
            notional *= 0.45

        if self.regime == "calm":
            notional *= 0.8
        elif self.regime == "volatile":
            notional *= 1.1
        elif self.regime == "panic":
            notional *= 1.25

        size = notional / max(px, 1e-6)
        size *= random.uniform(0.8, 1.25)

        # жёстный триммер
        size = max(MIN_ORDER_QTY, min(size, 220_000.0))

        # решаем, будет ли это айсберг
        is_iceberg = False
        iceberg_md: Dict[str, Any] = {}
        if level >= 3 and not near and size > 40_000:
            is_iceberg = True
            # видимая часть — 25–40%
            visible = size * random.uniform(0.25, 0.40)
            reserve = max(0.0, size - visible)
            chunk = visible * random.uniform(0.4, 0.8)
            repl_ms = random.randint(180, 420)
            iceberg_md = {
                "display_qty": visible,
                "reserve_qty": reserve,
                "chunk": chunk,
                "replenish_ms": repl_ms,
            }
            size = visible  # видим только дисплей
        else:
            iceberg_md = {}

        return float(size), is_iceberg, iceberg_md

    # ──────────────────────────────────────────────────────────
    #  Управление активными ордерами
    # ──────────────────────────────────────────────────────────

    def _cancel_all(self) -> List[Order]:
        out: List[Order] = []
        for oid, meta in list(self.active.items()):
            out.append(Order(
                order_id=oid,
                agent_id=self.agent_id,
                side=meta["side"],
                price=meta["price"],
                volume=0.0,
                order_type=OrderType.CANCEL,
                ttl=0.0
            ))
            self.queue.forget(oid)
            self.active.pop(oid, None)
        return out

    def _cancel_expired(self, now_ms: float) -> List[Order]:
        out: List[Order] = []
        for oid, meta in list(self.active.items()):
            if now_ms - meta["ts_ms"] > meta["ttl_ms"]:
                out.append(Order(
                    order_id=oid,
                    agent_id=self.agent_id,
                    side=meta["side"],
                    price=meta["price"],
                    volume=0.0,
                    order_type=OrderType.CANCEL,
                    ttl=0.0
                ))
                self.queue.forget(oid)
                self.active.pop(oid, None)
        return out

    def _cancel_some(self, now_ms: float) -> List[Order]:
        out: List[Order] = []

        for oid, meta in list(self.active.items()):
            age = now_ms - meta["ts_ms"]
            ttl = meta["ttl_ms"]

            # вероятность удаления зависит от "старости"
            life_ratio = age / max(ttl, 1)

            p = 0.0

            # near обновляем чаще, но не все сразу
            if meta["near"]:
                p = 0.15 * life_ratio
            else:
                p = 0.05 * life_ratio

            # чуть шума
            p *= random.uniform(0.7, 1.3)

            if random.random() < p:
                out.append(Order(
                    order_id=oid,
                    agent_id=self.agent_id,
                    side=meta["side"],
                    price=meta["price"],
                    volume=0.0,
                    order_type=OrderType.CANCEL,
                    ttl=0.0
                ))
                self.queue.forget(oid)
                self.active.pop(oid, None)

        return out

    # ──────────────────────────────────────────────────────────
    #  Генерация уровней
    # ──────────────────────────────────────────────────────────

    def _levels_from_mid(self, mid: float, bid_spread: float, ask_spread: float,
                         bid: Optional[float] = None, ask: Optional[float] = None) -> List[tuple]:
        """
        Возвращает список кортежей (price, near, level, side)
        с частичным квотированием, создающим дыры в стакане.
        """
        out: List[tuple] = []

        # [FIX] Level-0: ставим AT текущий BBO, не mid±0.2*tick.
        # mid=100.000 -> quant(100.000±0.002)=100.000 на обеих сторонах
        # -> spread=0, locked book, немедленный self-match, 27% нулевого спреда.
        if bid is not None and ask is not None and bid < ask:
            near_bid = quant(bid)
            near_ask = quant(ask)
        else:
            near_bid = quant(mid - self.tick)
            near_ask = quant(mid + self.tick)
            if near_bid >= near_ask:
                near_ask = quant(near_bid + self.tick)

        out.append((near_bid, True, 0, OrderSide.BID))
        out.append((near_ask, True, 0, OrderSide.ASK))

        for idx, off in enumerate(LEVEL_OFFSETS, start=1):
            # bid/ask в "спред-единицах"
            bid_px = quant(mid - off * bid_spread * self.tick)
            ask_px = quant(mid + off * ask_spread * self.tick)

            jitter = random.uniform(-0.15, 0.15) * self.tick

            bid_px = quant(bid_px + jitter)
            ask_px = quant(ask_px + jitter)

            if bid_px >= ask_px:
                bid_px = quant(mid - (0.4 + 0.2 * idx) * self.tick)
                ask_px = quant(mid + (0.4 + 0.2 * idx) * self.tick)

            near = (idx == 1)

            # вероятность квотирования уровня
            if near:
                p_bid = 0.95
                p_ask = 0.95
            else:
                # чуть чаще в тренде/панике
                base_p = 0.30
                if self.regime in ("volatile", "panic"):
                    base_p = 0.40
                p_bid = base_p
                p_ask = base_p

            if random.random() < p_bid:
                out.append((bid_px, near, idx, OrderSide.BID))
            if random.random() < p_ask:
                out.append((ask_px, near, idx, OrderSide.ASK))

        return out

    # ──────────────────────────────────────────────────────────
    #  Public API: generate_orders
    # ──────────────────────────────────────────────────────────

    def generate_orders(self, *args, **kwargs) -> List[Order]:
        """
        Вызывается сервером каждые ~300мс.
        Может принимать:
          - generate_orders(order_book)
          - generate_orders(order_book, market_context=...)
        Возвращает плоский список Order (лимит + cancel).
        """
        ob = None
        mid = kwargs.get("mid")
        bid = kwargs.get("bid")
        ask = kwargs.get("ask")

        market_context = kwargs.get("market_context")
        if len(args) >= 1 and ob is None:
            ob = args[0]
        if len(args) >= 2 and market_context is None:
            market_context = args[1]

        now = _ctx_now(market_context)
        self._now_ts = now
        if market_context is not None:
            self.session_liq_mult = float(market_context.get("session_liq_mult", 1.0))
            self.session_spread_bias = float(market_context.get("session_spread_bias", 0.0))

        if args:
            first = args[0]
            if hasattr(first, "_best_bid_price") and hasattr(first, "_best_ask_price"):
                ob = first
                try:
                    b = first._best_bid_price()
                    a = first._best_ask_price()
                except Exception:
                    b = a = None
                if b is not None:
                    bid = float(b)
                if a is not None:
                    ask = float(a)
                if bid is not None and ask is not None:
                    mid = (bid + ask) / 2.0

        if mid is None:
            mid = self.local_mid if self.local_mid is not None else DEFAULT_ANCHOR_MID
        mid = float(mid)

        # LP в fade-out?
        now = getattr(self, "_now_ts", time.time())
        if now < self.lp_block_until:
            return []

        now_ms = now * 1000.0

        if self.local_mid is None:
            self.local_mid = mid

        self._update_state(mid, ob)
        self.local_mid = mid

        # можно слегка якорить mid, если улетел слишком далеко
        if abs(mid - DEFAULT_ANCHOR_MID) > 50.0:
            mid = DEFAULT_ANCHOR_MID + 0.7 * (mid - DEFAULT_ANCHOR_MID)
            self.local_mid = mid

        cancels: List[Order] = []
        cancels.extend(self._cancel_expired(now_ms))
        cancels.extend(self._cancel_some(now_ms))

        spread_ticks, bid_spread, ask_spread = self._compute_spread_and_skew(mid)
        risk = self._risk_level(mid)

        # fade-out триггеры
        if (
            risk > 0.88
            or self.tox_ema.value > 0.7
            or self.trade_chain_ema.value > 4.5
            or self.regime == "panic" and random.random() < 0.10
        ):
            # LP уходит из стакана на 0.4–1.0 сек
            self.lp_block_until = now + random.uniform(0.4, 1.0)
            cancels.extend(self._cancel_all())
            return cancels

        # полная перестройка книги не каждый тик


        levels = self._levels_from_mid(mid, bid_spread, ask_spread, bid=bid, ask=ask)

        new_orders: List[Order] = []
        for px, near, lvl, side in levels:
            size, is_iceberg, iceberg_md = self._size_for_level(px, mid, spread_ticks, risk, near, lvl)
            ttl_ms = self._ttl_ms(near, risk, lvl)
            lat_ms, jitter_ms = self._latency_profile(near, lvl)

            oid = uuid.uuid4().hex

            meta = {
                "side": side,
                "price": px,
                "volume": size,
                "ts_ms": now_ms,
                "ttl_ms": ttl_ms,
                "near": near,
                "level": lvl,
                "iceberg": is_iceberg,
            }
            self.active[oid] = meta
            self.queue.register(oid, 3.0 + lvl)

            # metadata для движка (latency / TTL / iceberg)
            md: Dict[str, Any] = {
                "latency_ms": lat_ms,
                "latency_jitter_ms": jitter_ms,
                "ttl_ms": ttl_ms,
            }
            if is_iceberg:
                md.update(iceberg_md)

            order = Order(
                order_id=oid,
                agent_id=self.agent_id,
                side=side,
                price=px,
                volume=size,
                order_type=OrderType.LIMIT,
                ttl=ttl_ms / 1000.0,
            )
            # вешаем metadata поверх Order — движок это использует
            setattr(order, "metadata", md)

            new_orders.append(order)

        return cancels + new_orders

    # ──────────────────────────────────────────────────────────
    #  Обработка исполнений / PnL
    # ──────────────────────────────────────────────────────────
    def fill(self, side: OrderSide, price: float, qty: float):
        price = float(price)
        qty = float(qty)
        signed = qty if side == OrderSide.BID else -qty
        self.inventory += signed
        self.cash -= signed * price

    def on_order_filled(self, order_id: str, price: float, qty: float, side: OrderSide) -> None:
        qty = float(qty)
        price = float(price)
        if qty <= 0.0:
            return

        # 1) ЕДИНСТВЕННЫЙ учёт позиции — через fill()
        # (убираем ручные self.cash/self.inventory, иначе будет x2)
        self.fill(side, price, qty)

        # 2) Синхронизируем внутреннее состояние active (частичные/полные)
        meta = self.active.get(order_id)
        if meta is not None:
            try:
                rem = float(meta.get("volume", 0.0)) - qty
            except Exception:
                rem = -1.0

            if rem <= 1e-9:
                self.active.pop(order_id, None)
                self.queue.forget(order_id)
            else:
                meta["volume"] = rem

        # 3) queue-rank — ок, оставляем
        if order_id in self.queue.ranks:
            self.queue.ranks[order_id] = max(0.0, self.queue.ranks[order_id] - 2.0)

    def mark_to_market(self, mid: float) -> float:
        return self.cash + self.inventory * float(mid) - self.capital