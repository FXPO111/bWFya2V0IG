# tier1_ubs.py — UBS_v7 (усиленный LP с инвентарным хеджем и anti-trend стенами)

import uuid
import random
import time
import math
from collections import deque
from typing import Dict, Any, Optional

import numpy as np

from order import Order, OrderSide, OrderType, TICK

def _ctx_now(ctx):
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()

def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x

def _post_only_price(side, price: float, best_bid: float | None, best_ask: float | None) -> float:
    if best_bid is None or best_ask is None:
        return price

    is_buy = (side == OrderSide.BID) or (isinstance(side, str) and side.lower() in ("buy", "bid"))
    if is_buy:
        return min(price, best_ask - TICK)
    else:
        return max(price, best_bid + TICK)


class Tier1UBSBank:
    """
    UBS — тонкий Tier-1 LP.

    Поведение:
      * базово даёт BBO-ликвидность малым объёмом;
      * запускает краткосрочные направленные волны (micro-legs);
      * поддерживает небольшой шумовый поток;
      * И ВАЖНОЕ ДОБАВЛЕНИЕ:
          - активный инвентарный хедж (market/агрессивные limit против позиции),
          - anti-trend стены при односторонней ленте и растяжённом тренде.
    """

    LIQ_THIN = -1
    LIQ_NORMAL = 0
    LIQ_THICK = 1

    def __init__(self, agent_id: str, capital: float):
        self.agent_id = str(agent_id)
        self.capital = float(capital)
        self.cash = float(capital)
        self.position = 0.0

        # --- память по mid-price ---
        self.mid_ema_fast: Optional[float] = None
        self.mid_ema_slow: Optional[float] = None
        self.mid_fast_tau = 3.0
        self.mid_slow_tau = 45.0
        self.last_mid_ts = 0.0

        # velocity / acceleration
        self.prev_mid: Optional[float] = None
        self.prev_velocity: float = 0.0

        # --- активность/flow UBS как OU-процесс ---
        self.flow_level = 0.30
        self.flow_mu = 0.28
        self.flow_phi = 0.96
        self.flow_sigma = 0.06
        self.last_flow_ts = 0.0

        # --- волновой режим (micro-leg) ---
        self.wave_dir = 0          # -1: sell-wave, +1: buy-wave, 0: нет
        self.wave_end_ts = 0.0

        self.last_wave_dir = 0
        self.same_dir_wave_count = 0

        # --- tape imbalance ---
        self.tape_window = deque(maxlen=80)

        # --- режим ликвидности стакана ---
        self.liq_regime = self.LIQ_NORMAL
        self.liq_regime_ts = 0.0
        self.liq_regime_hold_min = 30.0
        self.liq_regime_hold_max = 120.0

        # --- память по тренду/воле/flow ---
        self.trend_mem = 0.0
        self.sigma_mem = 0.0
        self.flow_mem = 0.0

        # --- risk tension (0..1) ---
        self.risk_tension = 0.0

        # --- реакция на провалы глубины ---
        self.last_top_bid_vol: float = 0.0
        self.last_top_ask_vol: float = 0.0

        # --- client flow события ---
        self.client_flow_side: int = 0   # -1 sell, +1 buy, 0 none
        self.client_flow_end_ts: float = 0.0
        self.next_client_flow_ts: float = 0.0

        # CALIB: EUR Tier-1 LP inventory limit ~0.5-1% capital notional. Was 1.5%.
        self.max_inv_frac = 0.008           # CALIB: was 0.015
        self.max_gross_notional_frac = 0.0015  # CALIB: was 0.0025

        # CALIB: EUR/USD UBS decision interval 0.35-0.9s → 0.10-0.28s (3x faster).
        # UBS is a thin Tier-1 LP: fast refresh rate is core to EUR liquidity microstructure.
        self.dec_interval_min = 0.10   # was 0.35
        self.dec_interval_max = 0.28   # was 0.90

        # история PnL / mid
        self.pnl_hist = deque(maxlen=600)

        # теги ордеров (core / wave / micro / hedge / wall / client)
        self._order_tag: Dict[str, str] = {}

        self.next_decision_ts = 0.0
        self.last_flow_ts = 0.0

    # ============================================================
    # Вспомогательные
    # ============================================================

    def _inv_frac(self, mid: Optional[float]) -> float:
        if mid is None or self.capital <= 0:
            return 0.0
        return _clip((self.position * mid) / self.capital, -1.0, 1.0)

    def _update_mid_ema_and_kinematics(self, mid: float, now: float) -> Dict[str, float]:
        if mid is None or not math.isfinite(mid):
            return {"velocity": 0.0, "accel": 0.0}

        if self.prev_mid is None:
            self.prev_mid = mid
            self.prev_velocity = 0.0

        dt = max(now - self.last_mid_ts, 1e-3)
        self.last_mid_ts = now

        velocity = (mid - self.prev_mid) / max(TICK * dt, 1e-6)
        accel = velocity - self.prev_velocity
        self.prev_mid = mid
        self.prev_velocity = velocity

        if self.mid_ema_fast is None:
            self.mid_ema_fast = mid
            self.mid_ema_slow = mid
        else:
            def alpha(dt_: float, tau_: float) -> float:
                if tau_ <= 0:
                    return 1.0
                return 1.0 - math.exp(-dt_ / tau_)

            a_fast = alpha(dt, self.mid_fast_tau)
            a_slow = alpha(dt, self.mid_slow_tau)

            self.mid_ema_fast = (1.0 - a_fast) * self.mid_ema_fast + a_fast * mid
            self.mid_ema_slow = (1.0 - a_slow) * self.mid_ema_slow + a_slow * mid

        return {"velocity": float(velocity), "accel": float(accel)}

    def _update_flow_level(self, feat: Dict[str, Any], now: float) -> None:
        dt = max(now - self.last_flow_ts, 1e-3)
        self.last_flow_ts = now
        dt = min(dt, 8.0)

        z = random.gauss(0.0, 1.0)
        act = self.flow_level
        act = self.flow_mu + self.flow_phi * (act - self.flow_mu) + self.flow_sigma * math.sqrt(dt) * z

        spread = feat["spread"]
        sigma = feat["sigma"]
        trend_mag = abs(feat["trend"])

        act += 1.5 * sigma + 0.4 * trend_mag - 0.4 * spread / max(TICK, 1e-6)
        self.flow_level = _clip(act, 0.0, 1.0)

    def _compute_tape_imbalance(self, order_book) -> float:
        trades = []
        try:
            trades = list(getattr(order_book, "trade_history", []))[-80:]
        except Exception:
            trades = []

        buy_vol = 0.0
        sell_vol = 0.0

        for t in trades:
            vol = float(t.get("volume") or t.get("qty") or t.get("size") or 0.0)
            if vol <= 0:
                continue
            side = t.get("side") or t.get("taker_side") or t.get("aggressor_side")

            is_buy = False
            is_sell = False

            if isinstance(side, OrderSide):
                if side == OrderSide.BID:
                    is_buy = True
                elif side == OrderSide.ASK:
                    is_sell = True
            elif isinstance(side, str):
                s = side.lower()
                if "buy" in s or "bid" in s or "b" == s:
                    is_buy = True
                elif "sell" in s or "ask" in s or "s" == s:
                    is_sell = True

            if is_buy:
                buy_vol += vol
            elif is_sell:
                sell_vol += vol

        if buy_vol <= 0 and sell_vol <= 0:
            return 0.0

        ratio = buy_vol / max(sell_vol, 1e-9)
        ratio = _clip(ratio, 0.2, 5.0)
        bias = (ratio - 1.0) / (ratio + 1.0)
        return float(_clip(bias, -1.0, 1.0))

    def _update_liq_regime(self, feat: Dict[str, Any], tape_bias: float, now: float) -> None:
        if now - self.liq_regime_ts < random.uniform(self.liq_regime_hold_min, self.liq_regime_hold_max):
            return

        spread = feat["spread"]
        bid_vol = feat["top_bid_vol"]
        ask_vol = feat["top_ask_vol"]
        sigma = feat["sigma"]

        depth = bid_vol + ask_vol

        score_thin = 0.0
        score_thick = 0.0

        score_thin += 0.6 * _clip(spread / (3 * TICK), 0.0, 2.0)
        score_thin += 0.5 * _clip(sigma / 0.001, 0.0, 2.0)
        score_thin += 0.3 * (1.0 - math.tanh(depth / 500000.0))
        score_thin += 0.2 * abs(tape_bias)

        score_thick += 0.7 * _clip((2 * TICK) / max(spread, 1e-6), 0.0, 2.0)
        score_thick += 0.5 * (1.0 - _clip(sigma / 0.001, 0.0, 1.5))
        score_thick += 0.4 * math.tanh(depth / 500000.0)

        score_thin += random.uniform(-0.1, 0.1)
        score_thick += random.uniform(-0.1, 0.1)

        if score_thin > score_thick and score_thin > 0.7:
            self.liq_regime = self.LIQ_THIN
        elif score_thick > score_thin and score_thick > 0.7:
            self.liq_regime = self.LIQ_THICK
        else:
            self.liq_regime = self.LIQ_NORMAL

        self.liq_regime_ts = now

    def _update_temporal_memory(self, feat: Dict[str, Any]) -> None:
        alpha = 0.03
        self.trend_mem = (1.0 - alpha) * self.trend_mem + alpha * float(feat["trend"])
        self.sigma_mem = (1.0 - alpha) * self.sigma_mem + alpha * float(feat["sigma"])
        self.flow_mem = (1.0 - alpha) * self.flow_mem + alpha * float(self.flow_level)

    def _update_risk_tension(self, feat: Dict[str, Any], inv_frac: float,
                             tape_bias: float, velocity: float) -> None:
        base = 0.0
        base += 1.4 * abs(inv_frac / max(self.max_inv_frac, 1e-6))
        base += 0.8 * _clip(abs(feat["trend"]), 0.0, 1.5)
        base += 0.6 * _clip(feat["sigma"] / 0.001, 0.0, 2.0)
        base += 0.4 * _clip(abs(velocity) / 5.0, 0.0, 2.0)
        base += 0.3 * abs(tape_bias)
        base += 0.5 * abs(self.trend_mem)

        base = _clip(base / 6.0, 0.0, 1.5)
        alpha = 0.05
        self.risk_tension = _clip((1.0 - alpha) * self.risk_tension + alpha * base, 0.0, 1.0)

    def _update_depth_reactivity_state(self, feat: Dict[str, Any]) -> Dict[str, bool]:
        bid_vol = feat["top_bid_vol"]
        ask_vol = feat["top_ask_vol"]

        drop_bid = (self.last_top_bid_vol > 0 and bid_vol < self.last_top_bid_vol * 0.3)
        drop_ask = (self.last_top_ask_vol > 0 and ask_vol < self.last_top_ask_vol * 0.3)

        self.last_top_bid_vol = bid_vol
        self.last_top_ask_vol = ask_vol

        return {"drop_bid": drop_bid, "drop_ask": drop_ask}

    def _update_client_flow(self, feat: Dict[str, Any], tape_bias: float, now: float) -> None:
        if self.client_flow_side != 0 and now >= self.client_flow_end_ts:
            self.client_flow_side = 0

        if now < self.next_client_flow_ts:
            return

        self.next_client_flow_ts = now + random.uniform(90.0, 240.0)

        base_p = 0.15
        base_p += 0.15 * abs(tape_bias)
        base_p += 0.10 * abs(self.trend_mem)
        base_p += 0.10 * self.flow_level
        base_p = _clip(base_p, 0.0, 0.45)

        if random.random() > base_p:
            return

        score_up = 0.0
        score_dn = 0.0
        trend = feat["trend"]

        score_up += max(trend, 0.0)
        score_dn += max(-trend, 0.0)

        if tape_bias > 0:
            score_up += 0.6 * abs(tape_bias)
        elif tape_bias < 0:
            score_dn += 0.6 * abs(tape_bias)

        if self.liq_regime == self.LIQ_THIN and feat["spread"] > 2 * TICK:
            if trend > 0:
                score_up += 0.3
            elif trend < 0:
                score_dn += 0.3

        if score_up <= 0 and score_dn <= 0:
            return

        self.client_flow_side = 1 if score_up >= score_dn else -1
        self.client_flow_end_ts = now + random.uniform(4.0, 12.0)

    def _extract_features(self, order_book) -> Optional[Dict[str, Any]]:
        snap = order_book.get_order_book_snapshot(depth=3)
        bids = snap.get("bids") or []
        asks = snap.get("asks") or []
        if not bids or not asks:
            return None

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        mid = 0.5 * (best_bid + best_ask)
        spread = best_ask - best_bid

        top_bid_vol = float(bids[0].get("volume", 0.0)) if bids else 0.0
        top_ask_vol = float(asks[0].get("volume", 0.0)) if asks else 0.0

        now = getattr(self, "_now_ts", time.time())
        kin = self._update_mid_ema_and_kinematics(mid, now)

        if self.mid_ema_fast is None or self.mid_ema_slow is None:
            sigma = 0.0
            trend = 0.0
        else:
            diff = self.mid_ema_fast - self.mid_ema_slow
            trend = _clip(diff / max(TICK * 5.0, 1e-6), -1.5, 1.5)
            sigma = abs(diff) / max(self.mid_ema_slow, 1.0)

        features = {
            "mid": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "top_bid_vol": top_bid_vol,
            "top_ask_vol": top_ask_vol,
            "sigma": float(sigma),
            "trend": float(trend),
            "velocity": kin["velocity"],
            "accel": kin["accel"],
        }
        return features

    # ============================================================
    # Логика BBO-ликвидности, волн и шума
    # ============================================================

    def _core_liquidity_orders(self, feat: Dict[str, Any], flow: float,
                               inv_frac: float, tape_bias: float,
                               depth_state: Dict[str, bool]) -> list:
        mid = feat["mid"]
        bb = feat["best_bid"]
        ba = feat["best_ask"]
        spread = feat["spread"]
        bid_vol = feat["top_bid_vol"]
        ask_vol = feat["top_ask_vol"]

        orders = []

        # CALIB: old gate was spread <= 1 TICK → refused all quotes at tight spread.
        # After MM calibration spread sits at 0.35–0.5 TICK → UBS must quote there.
        # New gate: refuse only when spread <= 0.30 TICK (crossing territory) or >= 3 TICK.
        if spread <= 0.30 * TICK or spread >= 3.0 * TICK:
            return orders

        # CALIB: EUR UBS core notional ~0.05-0.2 lot = 562-2248 contracts.
        # At capital=100M: 100M * (0.00004 + 0.00020 * flow) = 4k-24k notional = 40-240 contracts.
        # Old: 0.00002+0.00010 gave 2k-12k = 20-120 contracts — 2x too small.
        base_notional = self.capital * (0.00004 + 0.00020 * flow)  # CALIB: was 0.00002+0.00010

        if self.liq_regime == self.LIQ_THICK:
            base_notional *= 1.25
        elif self.liq_regime == self.LIQ_THIN:
            base_notional *= 0.7

        base_notional *= (0.9 - 0.6 * self.risk_tension)
        base_notional = max(base_notional, 0.0)

        bias = 1.0 - 0.6 * abs(inv_frac)
        base_notional *= _clip(bias, 0.3, 1.0)

        if base_notional <= 0:
            return orders

        qty = max(1.0, base_notional / max(mid, 1.0))

        p_core = 0.25 + 0.45 * flow
        p_core *= (1.0 - 0.3 * self.risk_tension)
        if random.random() > _clip(p_core, 0.05, 0.9):
            return orders

        bid_weight = 1.0
        ask_weight = 1.0
        if tape_bias > 0:
            ask_weight += 0.2 * tape_bias
        elif tape_bias < 0:
            bid_weight += 0.2 * (-tape_bias)

        if depth_state["drop_bid"]:
            bid_weight *= 0.6
        if depth_state["drop_ask"]:
            ask_weight *= 0.6

        total_weight = bid_weight + ask_weight
        if total_weight <= 0:
            return orders

        # BID
        if random.random() < (bid_weight / total_weight):
            if inv_frac < self.max_inv_frac:
                price = bb
                if bid_vol < ask_vol * 0.4:
                    price = bb - random.choice([0, 1]) * TICK
                ttl = random.randint(4, 10)
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=OrderSide.BID,
                    volume=float(qty),
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=ttl,
                )
                orders.append(o)
                self._order_tag[o.order_id] = "core"

        # ASK
        if random.random() < (ask_weight / total_weight):
            if inv_frac > -self.max_inv_frac:
                price = ba
                if ask_vol < bid_vol * 0.4:
                    price = ba + random.choice([0, 1]) * TICK
                ttl = random.randint(4, 10)
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=OrderSide.ASK,
                    volume=float(qty),
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=ttl,
                )
                orders.append(o)
                self._order_tag[o.order_id] = "core"

        return orders

    def _maybe_update_wave(
        self,
        feat: Dict[str, Any],
        flow: float,
        inv_frac: float,
        tape_bias: float,
        depth_state: Dict[str, bool],
        now: float,
    ) -> None:
        if self.wave_dir != 0 and now < self.wave_end_ts:
            return

        self.wave_dir = 0

        mid = feat["mid"]
        trend = feat["trend"]
        vel = feat["velocity"]
        accel = feat["accel"]
        spread = max(feat["spread"], TICK)

        stretch = 0.0
        if self.mid_ema_slow is not None:
            stretch = (mid - self.mid_ema_slow) / spread

        if (
            abs(trend) < 0.20
            and abs(vel) < 0.7
            and abs(stretch) < 4.0
            and abs(self.trend_mem) < 0.15
        ):
            return

        p = 0.02 + 0.08 * flow
        p += 0.05 * abs(trend)
        p += 0.06 * _clip(abs(vel) / 4.0, 0.0, 1.5)
        p += 0.02 * abs(self.trend_mem)
        p += 0.03 * min(abs(stretch) / 8.0, 1.0)

        if self.liq_regime == self.LIQ_THIN:
            p *= 1.2
        elif self.liq_regime == self.LIQ_THICK:
            p *= 0.8

        p *= (1.0 - 0.6 * self.risk_tension)
        p *= (1.0 - 0.7 * abs(inv_frac))

        effective_trend_sign = 1 if trend > 0 else -1
        if self.last_wave_dir != 0 and self.last_wave_dir == effective_trend_sign:
            overuse = min(self.same_dir_wave_count / 3.0, 2.0)
            p *= max(0.35, 1.0 - 0.3 * overuse)

        p = _clip(p, 0.0, 0.45)

        if random.random() > p:
            return

        signal = (
            trend
            + 0.25 * tape_bias
            + 0.20 * _clip(stretch / 10.0, -1.5, 1.5)
        )
        if signal == 0.0:
            return

        direction = 1 if signal > 0 else -1

        if abs(stretch) > 10.0 and math.copysign(1.0, stretch) == direction:
            if random.random() < 0.6:
                direction *= -1

        self.wave_dir = direction
        self.wave_end_ts = now + random.uniform(8.0, 28.0)

        if self.last_wave_dir == direction:
            self.same_dir_wave_count += 1
        else:
            self.last_wave_dir = direction
            self.same_dir_wave_count = 1

    def _wave_orders(self, feat: Dict[str, Any], flow: float, inv_frac: float, tape_bias: float) -> list:
        if self.wave_dir == 0:
            return []

        mid = feat["mid"]
        bb = feat["best_bid"]
        ba = feat["best_ask"]
        spread = max(feat["spread"], TICK)

        orders = []

        stretch = 0.0
        if self.mid_ema_slow is not None:
            stretch = (mid - self.mid_ema_slow) / spread

        # CALIB: wave notional 2x. EUR UBS wave = 0.1-0.5 lot ≈ 1124-5620 contracts.
        # At 100M: 100M * (0.00004 + 0.00016 * flow) = 4k-20k notional = 40-200 contracts ✓
        base_notional = self.capital * (0.00004 + 0.00016 * flow)  # CALIB: was 0.00002+0.00008
        base_notional *= (1.0 - 0.7 * abs(inv_frac))
        base_notional *= (0.9 - 0.5 * self.risk_tension)
        base_notional = max(base_notional, 0.0)

        if (
            self.client_flow_side != 0
            and self.client_flow_side == self.wave_dir
            and abs(stretch) < 10.0
        ):
            base_notional *= 1.25

        dir_sign = 1 if self.wave_dir > 0 else -1
        if abs(stretch) > 6.0 and math.copysign(1.0, stretch) == dir_sign:
            k = 0.5 if abs(stretch) < 12.0 else 0.25
            base_notional *= k

        if abs(stretch) > 6.0 and math.copysign(1.0, stretch) == -dir_sign:
            base_notional *= 1.3

        if base_notional <= 0:
            return orders

        qty = max(1.0, base_notional / max(mid, 1.0))

        dir_up = self.wave_dir > 0

        mkt_p = 0.20 + 0.20 * flow
        if self.liq_regime == self.LIQ_THIN:
            mkt_p += 0.10
        if self.risk_tension > 0.6:
            mkt_p += 0.05
        if abs(stretch) > 8.0:
            mkt_p *= 0.6
        mkt_p = _clip(mkt_p, 0.10, 0.30)

        use_mkt = random.random() < mkt_p

        if dir_up:
            if inv_frac > self.max_inv_frac:
                return orders
            side = OrderSide.BID
            if use_mkt:
                price = None
                otype = OrderType.MARKET
            else:
                price = bb + random.choice([0, 1]) * TICK
                price = _post_only_price(side, price, bb, ba)
                otype = OrderType.LIMIT
        else:
            if inv_frac < -self.max_inv_frac:
                return orders
            side = OrderSide.ASK
            if use_mkt:
                price = None
                otype = OrderType.MARKET
            else:
                price = ba - random.choice([0, 1]) * TICK
                price = _post_only_price(side, price, bb, ba)
                otype = OrderType.LIMIT

        ttl = None if use_mkt else random.randint(3, 12)

        o = Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            volume=float(qty),
            price=None if price is None else float(round(price, 5)),
            order_type=otype,
            ttl=ttl,
        )
        tag = "wave"
        if self.client_flow_side != 0 and self.client_flow_side == self.wave_dir:
            tag = "client"
        self._order_tag[o.order_id] = tag
        orders.append(o)

        return orders

    def _micro_noise_orders(self, feat: Dict[str, Any], flow: float,
                            inv_frac: float) -> list:
        mid = feat["mid"]
        bb = feat["best_bid"]
        ba = feat["best_ask"]

        orders = []

        if flow < 0.15 and random.random() < 0.85:
            return orders

        base_notional = self.capital * random.uniform(0.000003, 0.00002)
        base_notional *= (1.0 - 0.6 * abs(inv_frac))
        base_notional *= (0.95 - 0.4 * self.risk_tension)
        base_notional = max(base_notional, 0.0)
        if base_notional <= 0:
            return orders

        qty = max(1.0, base_notional / max(mid, 1.0))

        side_bias = 0.5
        if self.trend_mem > 0:
            side_bias -= 0.05
        elif self.trend_mem < 0:
            side_bias += 0.05

        inv_k = 0.10 * _clip(abs(inv_frac) / max(self.max_inv_frac, 1e-6), 0.0, 1.0)
        if inv_frac > 0.0:
            side_bias -= inv_k
        elif inv_frac < 0.0:
            side_bias += inv_k

        side_bias = _clip(side_bias, 0.2, 0.8)

        side = OrderSide.BID if random.random() < side_bias else OrderSide.ASK

        if side == OrderSide.BID and inv_frac > self.max_inv_frac:
            return orders
        if side == OrderSide.ASK and inv_frac < -self.max_inv_frac:
            return orders

        if side == OrderSide.BID:
            price = bb - random.choice([0, 0, 1]) * TICK
        else:
            price = ba + random.choice([0, 0, 1]) * TICK

        ttl = random.randint(2, 8)

        o = Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            volume=float(qty),
            price=float(round(price, 5)),
            order_type=OrderType.LIMIT,
            ttl=ttl,
        )
        self._order_tag[o.order_id] = "micro"
        orders.append(o)

        return orders

    # ============================================================
    # НОВОЕ: инвентарный хедж и anti-trend стены
    # ============================================================

    def _inventory_hedge_orders(
        self,
        feat: Dict[str, Any],
        inv_frac: float,
        tape_bias: float,
        flow: float,
    ) -> list:
        """
        Активный хедж против накопленной позиции.
        UBS не только уменьшает новое выставление, но и реально разгружает дельту.
        """
        mid = feat["mid"]
        if mid <= 0:
            return []

        # базовый триггер: значимая дельта или высокая напряжённость риска
        inv_level = abs(inv_frac) / max(self.max_inv_frac, 1e-6)
        if inv_level < 0.4 and self.risk_tension < 0.4:
            return []

        # вероятность самого события хеджа
        p = 0.10 + 0.25 * _clip(inv_level, 0.0, 2.0)
        p += 0.10 * self.risk_tension
        p += 0.05 * abs(tape_bias)
        p += 0.05 * flow
        p = _clip(p, 0.0, 0.6)

        if random.random() > p:
            return []

        # объём: 0.005–0.03% капитала, с усилением по inv_level
        base_frac = random.uniform(0.00005, 0.00030)
        base_frac *= (0.7 + 0.6 * _clip(inv_level, 0.0, 2.0))
        base_frac *= (0.7 + 0.5 * self.risk_tension)
        notional = self.capital * base_frac

        # ограничиваем, чтобы не выходить больше, чем текущий размер позы
        max_notional_by_pos = abs(inv_frac) * self.capital
        notional = min(notional, max_notional_by_pos)
        if notional < mid * 0.2:
            return []

        qty = max(1.0, notional / mid)

        # направление хеджа — против позиции
        if inv_frac > 0:
            side = OrderSide.ASK
        else:
            side = OrderSide.BID

        # тип ордера: при сильном риске чаще MARKET
        mkt_p = 0.35 + 0.35 * self.risk_tension
        mkt_p *= (0.8 + 0.4 * flow)
        mkt_p = _clip(mkt_p, 0.10, 0.80)
        use_mkt = random.random() < mkt_p

        if use_mkt:
            price = None
            otype = OrderType.MARKET
            ttl = None
        else:
            bb = feat["best_bid"]
            ba = feat["best_ask"]
            if side == OrderSide.ASK:
                price = ba - random.choice([0, 1]) * TICK
            else:
                price = bb + random.choice([0, 1]) * TICK
            price = _post_only_price(side, price, bb, ba)
            ttl = random.randint(3, 15)
            otype = OrderType.LIMIT

        o = Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            volume=float(qty),
            price=None if price is None else float(round(price, 5)),
            order_type=otype,
            ttl=ttl,
        )
        self._order_tag[o.order_id] = "hedge"
        return [o]

    def _anti_trend_wall_orders(
        self,
        feat: Dict[str, Any],
        inv_frac: float,
        tape_bias: float,
    ) -> list:
        """
        Стены против направления когда рынок перекручен:
        - при сильном ап-тренде и buy-bias → ASK стены,
        - при сильном даун-тренде и sell-bias → BID стены.
        """
        mid = feat["mid"]
        bb = feat["best_bid"]
        ba = feat["best_ask"]
        spread = max(feat["spread"], TICK)
        trend = feat["trend"]

        if mid <= 0:
            return []

        # растяжение относительно медленной EMA
        if self.mid_ema_slow is None:
            return []
        stretch = (mid - self.mid_ema_slow) / spread

        # требуется выраженный тренд/лента и перерастяжение
        if abs(trend) < 0.35 and abs(self.trend_mem) < 0.25:
            return []
        if abs(stretch) < 8.0 and abs(tape_bias) < 0.4:
            return []

        orders = []

        # вероятность поставить стену зависит от risk_tension и растяжения
        base_p = 0.08
        base_p += 0.10 * _clip(abs(stretch) / 12.0, 0.0, 1.5)
        base_p += 0.10 * self.risk_tension
        base_p += 0.05 * abs(tape_bias)
        base_p = _clip(base_p, 0.0, 0.40)

        if random.random() > base_p:
            return orders

        # направление доминирующего движения
        dominant = trend
        if abs(self.trend_mem) > abs(dominant):
            dominant = self.trend_mem
        if abs(tape_bias) > 0.4 and abs(tape_bias) > abs(dominant):
            dominant = tape_bias

        if dominant == 0.0:
            return orders

        # если доминирует вверх — строим ASK стены выше рынка
        if dominant > 0:
            side = OrderSide.ASK
            ticks = random.randint(1, 3)
            price = ba + ticks * TICK
        else:
            side = OrderSide.BID
            ticks = random.randint(1, 3)
            price = bb - ticks * TICK

        # размер стены: зависит от капитала и inv_frac (сильнее, если позиция против движения)
        align = 1.0
        if dominant > 0 and inv_frac > 0:
            align = 1.3  # long против перекупленности → активнее продаём
        if dominant < 0 and inv_frac < 0:
            align = 1.3

        wall_frac = random.uniform(0.00005, 0.00025)
        wall_frac *= (0.7 + 0.6 * self.risk_tension)
        wall_frac *= align

        notional = self.capital * wall_frac
        qty = max(1.0, notional / price)

        o = Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            volume=float(qty),
            price=float(round(price, 5)),
            order_type=OrderType.LIMIT,
            ttl=random.randint(10, 45),
        )
        self._order_tag[o.order_id] = "wall"
        orders.append(o)
        return orders

    # ============================================================
    # Публичный API
    # ============================================================

    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = _ctx_now(market_context)
        self._now_ts = now

        if self.next_decision_ts <= 0.0:
            self.next_decision_ts = now + random.uniform(0.2, 0.8)
        if self.next_client_flow_ts <= 0.0:
            self.next_client_flow_ts = now + random.uniform(90.0, 240.0)
        if self.last_mid_ts <= 0.0:
            self.last_mid_ts = now
        if self.liq_regime_ts <= 0.0:
            self.liq_regime_ts = now

        feat = self._extract_features(order_book)
        if feat is None:
            self.next_decision_ts = now + 0.5
            return []

        tape_bias = self._compute_tape_imbalance(order_book)
        self._update_flow_level(feat, now)
        self._update_temporal_memory(feat)
        inv_frac = self._inv_frac(feat["mid"])
        depth_state = self._update_depth_reactivity_state(feat)
        self._update_risk_tension(feat, inv_frac, tape_bias, feat["velocity"])
        self._update_liq_regime(feat, tape_bias, now)
        self._update_client_flow(feat, tape_bias, now)

        flow = self.flow_level

        skip_p = 0.0
        if flow < 0.12:
            skip_p += 0.75
        elif flow < 0.30:
            skip_p += 0.45
        skip_p += 0.25 * self.risk_tension
        if random.random() < _clip(skip_p, 0.0, 0.95):
            self.next_decision_ts = now + random.uniform(self.dec_interval_min, self.dec_interval_max)
            return []

        self._maybe_update_wave(feat, flow, inv_frac, tape_bias, depth_state, now)

        orders = []
        orders += self._core_liquidity_orders(feat, flow, inv_frac, tape_bias, depth_state)
        orders += self._wave_orders(feat, flow, inv_frac, tape_bias)
        orders += self._micro_noise_orders(feat, flow, inv_frac)

        # НОВОЕ: активный инвентарный хедж и anti-trend стены
        orders += self._inventory_hedge_orders(feat, inv_frac, tape_bias, flow)
        orders += self._anti_trend_wall_orders(feat, inv_frac, tape_bias)

        # ограничение по общему notional
        if orders:
            mid = feat["mid"]
            total_notional = 0.0
            for o in orders:
                px = o.price if (o.price is not None) else mid
                total_notional += px * o.volume
            max_total = self.capital * self.max_gross_notional_frac
            if total_notional > max_total and total_notional > 0:
                scale = max_total / total_notional
                new_orders = []
                for o in orders:
                    new_vol = o.volume * scale
                    if new_vol >= 1.0:
                        o.volume = float(new_vol)
                        new_orders.append(o)
                    else:
                        self._order_tag.pop(o.order_id, None)
                orders = new_orders

        # маленький decay позиции
        decay_alpha = 0.0005
        self.position *= (1.0 - decay_alpha)

        self.next_decision_ts = now + random.uniform(self.dec_interval_min, self.dec_interval_max)
        return orders

    # ============================================================

    def on_order_filled(self, order_id, price, qty, side, slippage: float = 0.0):
        if price is None or qty <= 0:
            return

        notional = price * qty
        if side == OrderSide.BID:
            self.position += qty
            self.cash -= notional
        else:
            self.position -= qty
            self.cash += notional

        mtm = self.cash + self.position * price - self.capital
        self.pnl_hist.append(mtm)
        self._order_tag.pop(order_id, None)

    def perceive_market(self, market_context):
        return "neutral"

    def restore_capital(self):
        pass