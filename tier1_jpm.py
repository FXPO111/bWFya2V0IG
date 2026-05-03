# =========================
#   Tier1 JPM — v7
# =========================
# УСИЛЕННЫЙ LP БАНК
# - настоящий инвентарный хедж
# - stress-unload
# - anti-trend sells при tape_bias
# - лимитные стены
# - многошаговые hedge-waves
# =========================

import uuid
import time
import random
import math
from collections import deque
from typing import Optional, Dict, Any

from order import Order, OrderSide, OrderType, TICK

def _ctx_now(ctx):
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()

def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


class Tier1JPMBank:
    def __init__(self, agent_id: str, capital: float):
        self.agent_id = str(agent_id)
        self.capital = float(capital)
        self.cash = float(capital)
        self.position = 0.0

        # EMA/Trend
        self.mid_ema_fast = None
        self.mid_ema_slow = None
        self.ret_var_ema = None
        self.fast_tau = 3.0
        self.slow_tau = 55.0
        self.last_mid_ts = 0.0
        self.prev_mid = None
        self.prev_vel = 0.0

        # OU flow
        self.flow_state = 0.0
        self.flow_mu = 0.0
        self.flow_phi = 0.96
        self.flow_sigma = 0.35
        self.last_flow_ts = 0.0

        # burst/wave
        self.wave_active = False
        self.wave_dir = 0
        self.wave_steps_left = 0
        self.wave_base_frac = 0.0

        # глобальный тайминг
        self.next_ts = 0.0
        # CALIB: 3x faster, EUR Tier-1 LP refresh rate is ~0.1-0.3s.
        self.dec_min = 0.10   # was 0.25
        self.dec_max = 0.32   # was 0.95

        # CALIB: EUR JPM max inventory ~0.5-1% capital. Old 6% way too large,
        # caused huge directional swings when stress-unloading.
        self.max_inv_frac = 0.010   # CALIB: was 0.06
        self.pnl_hist = deque(maxlen=600)

    # =====================================================
    # MID / TREND
    # =====================================================

    def _update_mid(self, mid: float, now: float):
        if self.prev_mid is None:
            self.prev_mid = mid
            self.mid_ema_fast = mid
            self.mid_ema_slow = mid
            self.ret_var_ema = 0.0
            return {"trend": 0.0, "sigma": 0.0}

        dt = max(now - self.last_mid_ts, 1e-3)
        self.last_mid_ts = now

        ret = (mid - self.prev_mid) / max(TICK, 1e-6)
        vel = ret / dt
        self.prev_mid = mid

        # vola
        alpha = 1 - math.exp(-dt / 30.0)
        if self.ret_var_ema is None:
            self.ret_var_ema = ret * ret
        else:
            self.ret_var_ema = (1 - alpha) * self.ret_var_ema + alpha * (ret * ret)

        sigma = math.sqrt(max(self.ret_var_ema, 0.0))
        sigma_scaled = _clip(sigma, 0, 2.0)

        # EMA
        def a(dt_, tau_):
            return 1 - math.exp(-dt_ / tau_)

        af = a(dt, self.fast_tau)
        as_ = a(dt, self.slow_tau)
        self.mid_ema_fast = (1 - af) * self.mid_ema_fast + af * mid
        self.mid_ema_slow = (1 - as_) * self.mid_ema_slow + as_ * mid

        diff = self.mid_ema_fast - self.mid_ema_slow
        trend = diff / max(6 * TICK, 1e-6)
        trend = _clip(trend, -1.5, 1.5)

        self.prev_vel = vel
        return {"trend": trend, "sigma": sigma_scaled}

    # =====================================================
    # Tape Bias
    # =====================================================

    def _tape_bias(self, order_book):
        try:
            trades = list(order_book.trade_history)[-80:]
        except Exception:
            return 0.0
        buy = sell = 0.0
        for t in trades:
            v = float(t.get("volume", 0))
            s = str(t.get("side", "")).lower()
            if "buy" in s or "bid" in s:
                buy += v
            elif "sell" in s or "ask" in s:
                sell += v
        if buy + sell == 0:
            return 0.0
        ratio = buy / max(sell, 1e-9)
        bias = (ratio - 1) / (ratio + 1)
        return _clip(bias, -1, 1)

    # =====================================================
    # Book
    # =====================================================

    def _book(self, ob):
        snap = ob.get_order_book_snapshot(depth=3)
        bids = snap["bids"]
        asks = snap["asks"]
        if not bids or not asks:
            return None
        bb = float(bids[0]["price"])
        ba = float(asks[0]["price"])
        mid = 0.5 * (bb + ba)
        bidv = sum(float(b["volume"]) for b in bids)
        askv = sum(float(a["volume"]) for a in asks)
        tot = bidv + askv
        imb = (bidv - askv) / tot if tot > 0 else 0
        spread = ba - bb
        return {
            "mid": mid,
            "bb": bb,
            "ba": ba,
            "imb": _clip(imb, -1, 1),
            "spread": spread
        }

    # =====================================================
    # Inventory
    # =====================================================

    def _inv_frac(self, mid):
        return _clip((self.position * mid) / max(self.capital, 1), -1.5, 1.5)

    def _inv_target(self, kin):
        trend = kin["trend"]
        sigma = kin["sigma"]
        base = 0.55 * self.max_inv_frac * self.flow_state
        shrink = 1 - 0.35 * abs(trend) - 0.30 * sigma
        shrink = _clip(shrink, 0.3, 1.0)
        return _clip(base * shrink, -self.max_inv_frac, self.max_inv_frac)

    # =====================================================
    # Hedge Wave
    # =====================================================

    def _start_wave(self, diff_frac, sigma, trend):
        # волна запускается если позиция значительно ушла от таргета
        urg = abs(diff_frac) / max(self.max_inv_frac, 1e-6)
        if urg < 0.6:
            return
        p = 0.15 + 0.4 * urg + 0.2 * sigma
        if random.random() > p:
            return
        self.wave_active = True
        self.wave_dir = 1 if diff_frac > 0 else -1
        self.wave_steps_left = random.randint(2, 4)
        # CALIB: EUR JPM wave slice 0.02-0.1 lot = 225-1124 contracts.
        # At 180M: 180M * 0.00070 = 126k = 1260 contracts = 0.11 lot ✓ (upper end is fine)
        self.wave_base_frac = random.uniform(0.00016, 0.00070)  # CALIB: was 0.00008-0.00035

    def _wave_orders(self, mid, diff_frac):
        if not self.wave_active or self.wave_steps_left <= 0:
            return []
        self.wave_steps_left -= 1
        side = OrderSide.BID if self.wave_dir > 0 else OrderSide.ASK

        max_to_target = abs(diff_frac) * self.capital
        notional = min(self.wave_base_frac * self.capital, max_to_target)
        if notional <= mid * 0.2:
            return []

        vol = max(1.0, notional / mid)
        return [
            Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=side,
                volume=float(vol),
                price=None,
                order_type=OrderType.MARKET,
                ttl=None,
            )
        ]

    # =====================================================
    # Regular Hedge
    # =====================================================

    def _hedge(self, mid, diff_frac, kin, book_imb):
        if abs(diff_frac) < 0.005:
            return []

        trend = kin["trend"]
        sigma = kin["sigma"]
        urg = abs(diff_frac) / max(self.max_inv_frac, 1e-6)

        # CALIB: EUR JPM regular hedge 0.01-0.08 lot = 112-900 contracts.
        # At 180M: 180M * 0.00044 = 79k = 790 contracts = 0.07 lot ✓
        frac = random.uniform(0.00006, 0.00044)   # CALIB: was 0.00003-0.00022
        frac *= (0.5 + 1.2 * urg)
        frac *= (1 - 0.25 * abs(trend) - 0.20 * sigma)
        frac = max(frac, 0.00001)

        # Если книга хороша в нашу сторону — позволяем больше
        if diff_frac > 0 and book_imb <= 0:
            frac *= 1.15
        if diff_frac < 0 and book_imb >= 0:
            frac *= 1.15

        notional = min(frac * self.capital, abs(diff_frac) * self.capital)
        if notional < mid * 0.15:
            return []

        vol = max(1.0, notional / mid)
        side = OrderSide.BID if diff_frac > 0 else OrderSide.ASK

        return [
            Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=side,
                volume=float(vol),
                price=None,
                order_type=OrderType.MARKET,
                ttl=None,
            )
        ]

    # =====================================================
    # Stress Unload
    # =====================================================

    def _stress_unload(self, mid, inv_frac, kin):
        """
        Когда инвентарь >70% лимита — банк обязан разгружаться.
        Делает маленькие но частые SELL/BID против позиции.
        """
        if abs(inv_frac) < self.max_inv_frac * 0.70:
            return []

        direction = -1 if inv_frac > 0 else 1
        sigma = kin["sigma"]

        # 0.005–0.03% капитала
        frac = random.uniform(0.00005, 0.00030)
        frac *= (1 + 0.7 * sigma)

        notional = frac * self.capital
        vol = max(1.0, notional / mid)
        side = OrderSide.ASK if direction < 0 else OrderSide.BID

        return [
            Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=side,
                volume=float(vol),
                price=None,
                order_type=OrderType.MARKET,
                ttl=None,
            )
        ]

    # =====================================================
    # Tape Bias Anti-Trend Response
    # =====================================================

    def _anti_trend(self, mid, trend, tape_bias):
        """
        Anti-trend response:
          - если тренд вверх и tape buy-heavy — ставим ASK wall
          - если тренд вниз и tape sell-heavy — ставим BID wall
        """

        # LONG / UP MOVE: давим сверху ask-стеной
        if trend > 0 and tape_bias > 0.35:
            side = OrderSide.ASK
            price = self.mid_ema_fast + random.randint(1, 3) * TICK

        # SHORT / DOWN MOVE: поддерживаем снизу bid-стеной
        elif trend < 0 and tape_bias < -0.35:
            side = OrderSide.BID
            price = self.mid_ema_fast - random.randint(1, 3) * TICK

        else:
            return []

        vol = max(1.0, self.capital * random.uniform(0.00003, 0.00012) / mid)

        return [
            Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=side,
                volume=float(vol),
                price=float(round(price, 5)),
                order_type=OrderType.LIMIT,
                ttl=random.randint(5, 25),
            )
        ]

    # =====================================================
    # MAIN
    # =====================================================

    def generate_orders(self, order_book, market_context=None, **kw):
        now = _ctx_now(market_context)
        if self.next_ts <= 0.0:
            self.next_ts = now + random.uniform(0.1, 0.5)

        if self.last_mid_ts <= 0.0:
            self.last_mid_ts = now

        book = self._book(order_book)
        if book is None:
            self.next_ts = now + 0.5
            return []

        mid = book["mid"]
        kin = self._update_mid(mid, now)
        tape_bias = self._tape_bias(order_book)

        inv_frac = self._inv_frac(mid)
        target_inv = self._inv_target(kin)
        diff_frac = target_inv - inv_frac

        orders = []

        # 1) Stress Unload (самая сильная контртрендовая часть)
        stress = self._stress_unload(mid, inv_frac, kin)
        if stress:
            orders.extend(stress)

        # 2) Hedge Wave
        if not self.wave_active:
            self._start_wave(diff_frac, kin["sigma"], kin["trend"])
        wave = self._wave_orders(mid, diff_frac)
        if wave:
            orders.extend(wave)

        # 3) Regular Hedge
        if not wave:
            hed = self._hedge(mid, diff_frac, kin, book["imb"])
            if hed:
                orders.extend(hed)

        # 4) Anti-trend limit walls
        anti = self._anti_trend(mid, kin["trend"], tape_bias)
        if anti:
            orders.extend(anti)

        # next tick scheduling
        urg = abs(diff_frac) / max(self.max_inv_frac, 1e-6)
        base_dt = self.dec_max - (self.dec_max - self.dec_min) * (0.35 + 0.65 * urg)
        dt = base_dt * random.uniform(0.75, 1.15)
        self.next_ts = now + _clip(dt, 0.18, 1.35)

        return orders

    # =====================================================
    # ON FILL
    # =====================================================

    def on_order_filled(self, order_id, price, qty, side, slippage=0.0):
        if price is None or qty <= 0:
            return
        if side == OrderSide.BID:
            self.position += qty
            self.cash -= price * qty
        else:
            self.position -= qty
            self.cash += price * qty
        mtm = self.cash + self.position * price - self.capital
        self.pnl_hist.append(mtm)

    def perceive_market(self, ctx):
        return "tier1_jpm_v7"

    def restore_capital(self):
        pass