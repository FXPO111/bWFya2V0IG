# agent_variants.py
#
# Варианты основных агентов с другими параметрами.
# Цель: добавить "шероховатость" цене, не усиливая давление в одну сторону,
# а создавая разные ритмы, разную чувствительность и разную агрессию.
#
# СТРУКТУРА:
#  - 3 варианта Tier1UBSBank  (разные τ EMA, flow, inventory)
#  - 3 варианта CustomerAggregatorFlow (разные ритмы/стили)
#  - 2 варианта CorporateFlowManager (разные длительности/интенсивности)
#  - 2 новых агента: RandomPulseAgent, MomentumReversalAgent
#
# Все классы наследуются от оригиналов и только переопределяют __init__.
# Никакой логики не дублируется — только другие стартовые параметры.

import random
import time
import math
import uuid

from tier1_ubs import Tier1UBSBank
from customer_aggregator_flow import CustomerAggregatorFlow
from corporate_flow_manager import CorporateFlowManager
from order import OrderSide, OrderType, quant


# ══════════════════════════════════════════════════════════════════════════════
#  SAFETY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _is_limit_order(order) -> bool:
    ot = getattr(order, "order_type", None)

    if ot == OrderType.LIMIT:
        return True

    s = str(ot).lower()
    return s == "limit" or s.endswith(".limit")


def _is_bid_side(side) -> bool:
    return side == OrderSide.BID or str(side).lower().endswith("bid")


def _is_ask_side(side) -> bool:
    return side == OrderSide.ASK or str(side).lower().endswith("ask")


def _get_best_bid_ask(order_book):
    try:
        best_bid = order_book._best_bid_price()
        best_ask = order_book._best_ask_price()
        return best_bid, best_ask
    except Exception:
        return None, None


def _sanitize_non_crossing_limits(order_book, orders):
    """
    Предохранитель для variant agents.

    Агент может рассчитать лимитку по старому L1 или агрессивному смещению.
    Здесь мы не даём таким лимиткам пересекать текущий стакан:

      BID LIMIT >= best_ask  -> ставим на best_bid
      ASK LIMIT <= best_bid  -> ставим на best_ask

    MARKET-ордера не трогаем.
    """
    if not orders:
        return []

    best_bid, best_ask = _get_best_bid_ask(order_book)

    if best_bid is None or best_ask is None:
        return orders

    # Если книга уже crossed, не пытаемся лечить это на уровне агента.
    # Это обязан чинить сам стакан.
    if best_bid >= best_ask:
        return orders

    for o in orders:
        try:
            if not _is_limit_order(o):
                continue

            if getattr(o, "price", None) is None:
                continue

            side = getattr(o, "side", None)
            px = float(o.price)

            if _is_bid_side(side) and px >= best_ask:
                o.price = quant(best_bid)

            elif _is_ask_side(side) and px <= best_bid:
                o.price = quant(best_ask)

        except Exception:
            continue

    return orders


def _safe_parent_generate_orders(parent, order_book, market_context=None, **kwargs):
    """
    Совместимость с разными сигнатурами generate_orders() у родительских агентов.
    """
    try:
        return parent.generate_orders(order_book, market_context, **kwargs)
    except TypeError:
        try:
            return parent.generate_orders(order_book, market_context)
        except TypeError:
            return parent.generate_orders(order_book)


# ══════════════════════════════════════════════════════════════════════════════
#  TIER-1 UBS VARIANTS
#  Оригинал: fast τ=3s, slow τ=45s, dec 0.10–0.28s, max_inv 0.8%
# ══════════════════════════════════════════════════════════════════════════════

class Tier1UBS_Scalper(Tier1UBSBank):
    """
    HFT-режим: очень быстрый ритм, маленький инвентарь, агрессивные волны.
    Добавляет высокочастотный шум: много мелких ударов в обе стороны.

    Отличия от оригинала:
      - decision interval 0.05–0.15s
      - mid_fast_tau = 1.5s
      - mid_slow_tau = 20s
      - max_inv_frac = 0.004
      - flow_sigma = 0.09
      - wave_end продолжительность 4–14s
    """
    def __init__(self, agent_id: str, capital: float):
        super().__init__(agent_id, capital)
        self.dec_interval_min = 0.05
        self.dec_interval_max = 0.15
        self.mid_fast_tau = 1.5
        self.mid_slow_tau = 20.0
        self.max_inv_frac = 0.004
        self.max_gross_notional_frac = 0.0010
        self.flow_mu = 0.32
        self.flow_sigma = 0.09
        self._wave_dur_lo = 4.0
        self._wave_dur_hi = 14.0

    def _maybe_update_wave(self, feat, flow, inv_frac, tape_bias, depth_state, now):
        if self.wave_dir != 0 and now < self.wave_end_ts:
            return
        self.wave_dir = 0

        mid = feat["mid"]
        trend = feat["trend"]
        vel = feat["velocity"]
        spread = max(feat["spread"], 1e-6)

        stretch = 0.0
        if self.mid_ema_slow is not None:
            stretch = (mid - self.mid_ema_slow) / spread

        if abs(trend) < 0.15 and abs(vel) < 0.5 and abs(stretch) < 3.0:
            return

        p = 0.04 + 0.10 * flow
        p += 0.06 * abs(trend)
        p += 0.07 * min(abs(vel) / 3.0, 1.5)
        p *= (1.0 - 0.5 * self.risk_tension)
        p *= (1.0 - 0.6 * abs(inv_frac))
        p = min(p, 0.55)

        if random.random() > p:
            return

        signal = trend + 0.20 * tape_bias
        if signal == 0.0:
            return

        direction = 1 if signal > 0 else -1

        if abs(stretch) > 8.0 and math.copysign(1.0, stretch) == direction:
            if random.random() < 0.65:
                direction *= -1

        self.wave_dir = direction
        self.wave_end_ts = now + random.uniform(self._wave_dur_lo, self._wave_dur_hi)

        if self.last_wave_dir == direction:
            self.same_dir_wave_count += 1
        else:
            self.last_wave_dir = direction
            self.same_dir_wave_count = 1

    def generate_orders(self, order_book, market_context=None, **kwargs):
        orders = _safe_parent_generate_orders(super(), order_book, market_context, **kwargs)
        return _sanitize_non_crossing_limits(order_book, orders)


class Tier1UBS_Macro(Tier1UBSBank):
    """
    Макро-режим: медленный и крупный. Реагирует на долгосрочные дрейфы,
    выставляет большие стены и редко, но мощно хеджируется.

    Добавляет низкочастотное "сопротивление" трендам.
    """
    def __init__(self, agent_id: str, capital: float):
        super().__init__(agent_id, capital)
        self.dec_interval_min = 0.35
        self.dec_interval_max = 0.90
        self.mid_fast_tau = 8.0
        self.mid_slow_tau = 120.0
        self.max_inv_frac = 0.014
        self.max_gross_notional_frac = 0.0030
        self.flow_mu = 0.22
        self.flow_sigma = 0.04
        self._size_mult = 1.8

    def _core_liquidity_orders(self, feat, flow, inv_frac, tape_bias, depth_state):
        orders = super()._core_liquidity_orders(feat, flow, inv_frac, tape_bias, depth_state)
        for o in orders:
            o.volume = max(1.0, o.volume * self._size_mult)
        return orders

    def _wave_orders(self, feat, flow, inv_frac, tape_bias):
        orders = super()._wave_orders(feat, flow, inv_frac, tape_bias)
        for o in orders:
            o.volume = max(1.0, o.volume * self._size_mult)
        return orders

    def _anti_trend_wall_orders(self, feat, inv_frac, tape_bias):
        """Срабатывает раньше — при stretch > 5."""
        mid = feat["mid"]
        spread = max(feat["spread"], 1e-6)
        trend = feat["trend"]

        if mid <= 0 or self.mid_ema_slow is None:
            return []

        stretch = (mid - self.mid_ema_slow) / spread

        if abs(trend) < 0.25 and abs(self.trend_mem) < 0.20:
            return []
        if abs(stretch) < 5.0 and abs(tape_bias) < 0.35:
            return []

        return super()._anti_trend_wall_orders(feat, inv_frac, tape_bias)

    def generate_orders(self, order_book, market_context=None, **kwargs):
        orders = _safe_parent_generate_orders(super(), order_book, market_context, **kwargs)
        return _sanitize_non_crossing_limits(order_book, orders)


class Tier1UBS_Noise(Tier1UBSBank):
    """
    "Шумовой" LP: постоянно цитирует маленькими объёмами с случайным jitter,
    без выраженных волн. Добавляет tick-by-tick неравномерность без тренда.
    """
    def __init__(self, agent_id: str, capital: float):
        super().__init__(agent_id, capital)
        self.dec_interval_min = 0.08
        self.dec_interval_max = 0.20
        self.mid_fast_tau = 2.0
        self.mid_slow_tau = 30.0
        self.max_inv_frac = 0.005
        self.max_gross_notional_frac = 0.0008
        self.flow_mu = 0.45
        self.flow_sigma = 0.11

    def _maybe_update_wave(self, feat, flow, inv_frac, tape_bias, depth_state, now):
        self.wave_dir = 0

    def _micro_noise_orders(self, feat, flow, inv_frac):
        orders = super()._micro_noise_orders(feat, flow, inv_frac)
        for o in orders:
            o.volume = max(1.0, o.volume * 3.5)
        return orders

    def generate_orders(self, order_book, market_context=None, **kwargs):
        orders = _safe_parent_generate_orders(super(), order_book, market_context, **kwargs)
        return _sanitize_non_crossing_limits(order_book, orders)


# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOMER AGGREGATOR FLOW VARIANTS
# ══════════════════════════════════════════════════════════════════════════════

class CustomerAgg_AlgoHeavy(CustomerAggregatorFlow):
    """
    Доминируют algo-crumbs: много мелких, быстрых market-ударов.
    Создаёт высокочастотное давление с быстрым переключением сторон.
    """
    def __init__(self, agent_id: str = "caf_algo", capital: float = 300_000_000.0, **kwargs):
        super().__init__(
            agent_id=agent_id,
            capital=capital,
            retail_rate=0.30,
            corp_rate=0.08,
            algo_rate=0.80,
            max_inventory_notional_frac=0.05,
            rebalance_strength=0.50,
            **kwargs,
        )
        self._max_orders_per_call = 22
        self._regime_mult_lo = 0.60
        self._regime_mult_hi = 1.90

    def _roll_regime(self, now: float):
        if now < self._next_regime_ts:
            return
        self._regime_mult = self._rng.uniform(self._regime_mult_lo, self._regime_mult_hi)
        self._next_regime_ts = now + self._rng.uniform(5.0, 20.0)

    def generate_orders(self, order_book, market_context=None, **kwargs):
        orders = _safe_parent_generate_orders(super(), order_book, market_context, **kwargs)
        return _sanitize_non_crossing_limits(order_book, orders)


class CustomerAgg_CorpBurst(CustomerAggregatorFlow):
    """
    Корпоративные пакеты с редкими, но крупными всплесками.
    Создаёт периодические "удары".
    """
    def __init__(self, agent_id: str = "caf_corp", capital: float = 300_000_000.0, **kwargs):
        super().__init__(
            agent_id=agent_id,
            capital=capital,
            retail_rate=0.20,
            corp_rate=0.35,
            algo_rate=0.15,
            max_inventory_notional_frac=0.07,
            rebalance_strength=0.40,
            **kwargs,
        )
        self._max_orders_per_call = 10
        self._max_active_tickets = 16

    def _roll_regime(self, now: float):
        if now < self._next_regime_ts:
            return
        self._regime_mult = self._rng.uniform(0.20, 3.00)
        self._next_regime_ts = now + self._rng.uniform(8.0, 30.0)

    def _maybe_spawn_ticket(self, now, best_bid, best_ask, bids, asks):
        before_ids = {id(t) for t in self._tickets}
        super()._maybe_spawn_ticket(now, best_bid, best_ask, bids, asks)
        for t in self._tickets:
            if id(t) not in before_ids and t.style == "corp":
                t.deadline_ts = now + self._rng.uniform(3.0, 20.0)

    def generate_orders(self, order_book, market_context=None, **kwargs):
        orders = _safe_parent_generate_orders(super(), order_book, market_context, **kwargs)
        return _sanitize_non_crossing_limits(order_book, orders)


class CustomerAgg_RetailBias(CustomerAggregatorFlow):
    """
    Преобладает розничный поток: частые мелкие заявки с непредсказуемым направлением.
    """
    def __init__(self, agent_id: str = "caf_retail", capital: float = 300_000_000.0, **kwargs):
        super().__init__(
            agent_id=agent_id,
            capital=capital,
            retail_rate=0.85,
            corp_rate=0.08,
            algo_rate=0.20,
            max_inventory_notional_frac=0.08,
            rebalance_strength=0.30,
            **kwargs,
        )
        self._max_orders_per_call = 14

    def _roll_regime(self, now: float):
        if now < self._next_regime_ts:
            return
        self._regime_mult = self._rng.uniform(0.50, 1.60)
        self._next_regime_ts = now + self._rng.uniform(8.0, 30.0)

    def generate_orders(self, order_book, market_context=None, **kwargs):
        orders = _safe_parent_generate_orders(super(), order_book, market_context, **kwargs)
        return _sanitize_non_crossing_limits(order_book, orders)


# ══════════════════════════════════════════════════════════════════════════════
#  CORPORATE FLOW MANAGER VARIANTS
# ══════════════════════════════════════════════════════════════════════════════

class CorporateFlowManager_Tactical(CorporateFlowManager):
    """
    Тактический краткосрочный корп-поток.
    Много коротких исполнений → частая смена давления → шероховатость.
    """
    _side_max_imbalance = 2

    def __init__(self, agent_id: str, capital_pool: float):
        super().__init__(agent_id, capital_pool)
        self.next_check_ts = time.time() + random.uniform(3.0, 6.0)
        self._side_counts = {OrderSide.BID: 0, OrderSide.ASK: 0}

    def _pick_direction(self):
        diff = self._side_counts[OrderSide.ASK] - self._side_counts[OrderSide.BID]

        if diff >= self._side_max_imbalance:
            side = OrderSide.BID
        elif -diff >= self._side_max_imbalance:
            side = OrderSide.ASK
        else:
            side = super()._pick_direction()

        self._side_counts[side] += 1
        return side

    def _cleanup(self):
        super()._cleanup()

        counts = {OrderSide.BID: 0, OrderSide.ASK: 0}
        for a in self.active_agents:
            s = getattr(a, "side_bias", None)
            if s in counts:
                counts[s] += 1

        self._side_counts = counts

    def _desired_flow_count(self, market_context, now: float):
        session = self._get_session(market_context, now)

        if session == "asia":
            return random.randint(4, 8)
        if session == "london":
            return random.randint(8, 16)
        if session == "us":
            return random.randint(7, 14)

        return random.randint(4, 8)

    def _spawn_flow(self, market_context, now: float):
        session = self._get_session(market_context, now)

        if session == "asia":
            cap_frac = random.uniform(0.003, 0.015)
        elif session == "london":
            cap_frac = random.uniform(0.005, 0.025)
        elif session == "us":
            cap_frac = random.uniform(0.005, 0.020)
        else:
            cap_frac = random.uniform(0.003, 0.012)

        from corporate_flow_twap import CorporateFlowTWAP

        twap_capital = self.capital_pool * cap_frac
        side = self._pick_direction()

        agent = CorporateFlowTWAP(
            agent_id=f"{self.agent_id}_tact_{random.randint(1000, 9999)}",
            capital=twap_capital,
        )

        target_notional = twap_capital * random.uniform(0.01, 0.05)

        if session == "london":
            dur = random.uniform(3.0, 8.0)
        elif session == "us":
            dur = random.uniform(3.0, 7.0)
        else:
            dur = random.uniform(2.0, 6.0)

        agent.configure(
            side_bias=side,
            target_notional=target_notional,
            session_minutes=dur,
            now=getattr(self, "_now_ts", None),
        )

        self.active_agents.append(agent)
        self._recompute_live_net()

    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = float(getattr(market_context, "now_ts", None) or time.time())
        self._now_ts = now

        if now >= self.next_check_ts:
            self.next_check_ts = now + random.uniform(3.0, 6.0)
            self._cleanup()

            want = self._desired_flow_count(market_context, now)
            while len(self.active_agents) < want and len(self.active_agents) < self.max_flows:
                self._spawn_flow(market_context, now)

        all_orders = []

        for agent in list(self.active_agents):
            try:
                orders = agent.generate_orders(order_book, market_context)
                if orders:
                    all_orders.extend(orders)
            except Exception:
                pass

        return _sanitize_non_crossing_limits(order_book, all_orders)


class CorporateFlowManager_Strategic(CorporateFlowManager):
    """
    Стратегический долгосрочный корп-поток.
    Мало, но долгих исполнений → стабильный фоновый дрейф разных сторон.
    """
    _side_max_imbalance = 1

    def __init__(self, agent_id: str, capital_pool: float):
        super().__init__(agent_id, capital_pool)
        self.next_check_ts = time.time() + random.uniform(10.0, 18.0)
        self._side_counts = {OrderSide.BID: 0, OrderSide.ASK: 0}

    def _pick_direction(self):
        diff = self._side_counts[OrderSide.ASK] - self._side_counts[OrderSide.BID]

        if diff >= self._side_max_imbalance:
            side = OrderSide.BID
        elif -diff >= self._side_max_imbalance:
            side = OrderSide.ASK
        else:
            side = super()._pick_direction()

        self._side_counts[side] += 1
        return side

    def _cleanup(self):
        super()._cleanup()

        counts = {OrderSide.BID: 0, OrderSide.ASK: 0}
        for a in self.active_agents:
            s = getattr(a, "side_bias", None)
            if s in counts:
                counts[s] += 1

        self._side_counts = counts

    def _desired_flow_count(self, market_context, now: float):
        session = self._get_session(market_context, now)

        if session == "asia":
            return random.randint(2, 4)
        if session == "london":
            return random.randint(3, 6)
        if session == "us":
            return random.randint(3, 5)

        return random.randint(2, 3)

    def _spawn_flow(self, market_context, now: float):
        session = self._get_session(market_context, now)

        if session == "asia":
            cap_frac = random.uniform(0.015, 0.045)
        elif session == "london":
            cap_frac = random.uniform(0.020, 0.060)
        elif session == "us":
            cap_frac = random.uniform(0.018, 0.055)
        else:
            cap_frac = random.uniform(0.012, 0.035)

        from corporate_flow_twap import CorporateFlowTWAP

        twap_capital = self.capital_pool * cap_frac
        side = self._pick_direction()

        agent = CorporateFlowTWAP(
            agent_id=f"{self.agent_id}_strat_{random.randint(1000, 9999)}",
            capital=twap_capital,
        )

        target_notional = twap_capital * random.uniform(0.015, 0.07)

        if session == "london":
            dur = random.uniform(12.0, 30.0)
        elif session == "us":
            dur = random.uniform(10.0, 25.0)
        else:
            dur = random.uniform(8.0, 20.0)

        agent.configure(
            side_bias=side,
            target_notional=target_notional,
            session_minutes=dur,
            now=getattr(self, "_now_ts", None),
        )

        self.active_agents.append(agent)
        self._recompute_live_net()

    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = float(getattr(market_context, "now_ts", None) or time.time())
        self._now_ts = now

        if now >= self.next_check_ts:
            self.next_check_ts = now + random.uniform(10.0, 18.0)
            self._cleanup()

            want = self._desired_flow_count(market_context, now)
            while len(self.active_agents) < want and len(self.active_agents) < self.max_flows:
                self._spawn_flow(market_context, now)

        all_orders = []

        for agent in list(self.active_agents):
            try:
                orders = agent.generate_orders(order_book, market_context)
                if orders:
                    all_orders.extend(orders)
            except Exception:
                pass

        return _sanitize_non_crossing_limits(order_book, all_orders)


# ══════════════════════════════════════════════════════════════════════════════
#  NEW: RandomPulseAgent
# ══════════════════════════════════════════════════════════════════════════════

class RandomPulseAgent:
    """
    Агент-пульсатор: каждые pulse_interval_lo..hi секунд выбирает случайную
    сторону и отправляет MARKET-ордер с lognormal-объёмом.
    """
    _QUIET = "quiet"
    _ACTIVE = "active"

    def __init__(
        self,
        agent_id: str,
        capital: float,
        pulse_interval_lo: float = 0.5,
        pulse_interval_hi: float = 2.5,
        base_notional_frac: float = 0.0003,
        lognormal_sigma: float = 0.9,
        burst_prob: float = 0.08,
        burst_size_lo: int = 3,
        burst_size_hi: int = 7,
        quiet_dur_lo: float = 20.0,
        quiet_dur_hi: float = 90.0,
        active_dur_lo: float = 5.0,
        active_dur_hi: float = 25.0,
        quiet_interval_mult: float = 6.0,
        quiet_volume_mult: float = 0.25,
    ):
        self.agent_id = agent_id
        self.capital = capital
        self.pulse_interval_lo = pulse_interval_lo
        self.pulse_interval_hi = pulse_interval_hi
        self.base_notional = capital * base_notional_frac
        self.lognormal_sigma = lognormal_sigma
        self.burst_prob = burst_prob
        self.burst_size_lo = burst_size_lo
        self.burst_size_hi = burst_size_hi
        self.quiet_dur_lo = quiet_dur_lo
        self.quiet_dur_hi = quiet_dur_hi
        self.active_dur_lo = active_dur_lo
        self.active_dur_hi = active_dur_hi
        self.quiet_interval_mult = quiet_interval_mult
        self.quiet_volume_mult = quiet_volume_mult

        self._regime = self._QUIET
        self._regime_end_ts = time.time() + random.uniform(quiet_dur_lo, quiet_dur_hi)
        self._next_pulse_ts = time.time() + random.uniform(
            pulse_interval_lo * quiet_interval_mult,
            pulse_interval_hi * quiet_interval_mult,
        )
        self._burst_remaining = 0
        self._burst_side = None

    def _switch_regime(self, now: float) -> None:
        if now < self._regime_end_ts:
            return

        if self._regime == self._QUIET:
            self._regime = self._ACTIVE
            self._regime_end_ts = now + random.uniform(self.active_dur_lo, self.active_dur_hi)
        else:
            self._regime = self._QUIET
            self._regime_end_ts = now + random.uniform(self.quiet_dur_lo, self.quiet_dur_hi)
            self._burst_remaining = 0

    def on_order_filled(self, order_id: str, price: float, volume: float, side) -> None:
        pass

    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = float(getattr(market_context, "now_ts", None) or time.time())

        self._switch_regime(now)

        if now < self._next_pulse_ts:
            return []

        if self._regime == self._QUIET:
            self._next_pulse_ts = now + random.uniform(
                self.pulse_interval_lo * self.quiet_interval_mult,
                self.pulse_interval_hi * self.quiet_interval_mult,
            )
        else:
            self._next_pulse_ts = now + random.uniform(
                self.pulse_interval_lo,
                self.pulse_interval_hi,
            )

        if self._regime == self._ACTIVE and self._burst_remaining > 0:
            side = self._burst_side
            self._burst_remaining -= 1
        else:
            side = OrderSide.BID if random.random() < 0.50 else OrderSide.ASK

            if self._regime == self._ACTIVE and random.random() < self.burst_prob:
                self._burst_remaining = random.randint(self.burst_size_lo, self.burst_size_hi) - 1
                self._burst_side = side

        vol_mult = self.quiet_volume_mult if self._regime == self._QUIET else 1.0

        notional = self.base_notional * vol_mult * math.exp(
            random.gauss(0.0, self.lognormal_sigma)
        )
        notional = max(notional, 1.0)

        best_bid, best_ask = _get_best_bid_ask(order_book)

        if best_bid is None or best_ask is None:
            return []

        mid = (best_bid + best_ask) / 2.0
        price = mid if mid > 0 else 1.0
        volume = max(1.0, notional / price)

        from order import Order

        o = Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            price=best_ask if side == OrderSide.BID else best_bid,
            volume=volume,
            order_type="market",
        )

        return [o]


# ══════════════════════════════════════════════════════════════════════════════
#  NEW: MomentumReversalAgent
# ══════════════════════════════════════════════════════════════════════════════

class MomentumReversalAgent:
    """
    ZigZag / fakeout taker.

    Задача агента — НЕ тащить рынок линейно в одну сторону, а после появления
    направленного движения создать серию локальных continuation/pullback-волн:

        impulse -> продолжение -> откат -> новая попытка -> shakeout -> откат

    Это ближе к синей линии на скрине: агент видит движение на 1m и начинает
    "ломать" его внутри, создавая зигзаги, wick-и и fakeout-участки.

    Старые параметры entry_tau / entry_threshold / hold_duration_* оставлены
    для совместимости с текущим build_variant_agents(). Новые параметры можно
    не передавать — дефолты уже рабочие.
    """

    def __init__(
        self,
        agent_id: str,
        capital: float,
        entry_tau: float = 12.0,
        entry_threshold: float = 2.2,
        hold_duration_lo: float = 18.0,
        hold_duration_hi: float = 70.0,
        reversal_frac: float = 0.55,
        notional_frac: float = 0.0010,
        dec_interval: float = 0.45,
        # ── signal scaling ────────────────────────────────────────────────
        min_signal_scale: float = 0.005,
        signal_spread_mult: float = 5.0,
        vol_scale_mult: float = 3.0,
        velocity_tau: float = 8.0,
        # ── episode / zigzag control ──────────────────────────────────────
        leg_duration_lo: float = 1.8,
        leg_duration_hi: float = 6.0,
        pulse_interval_lo: float = 0.35,
        pulse_interval_hi: float = 1.35,
        trend_leg_probability: float = 0.58,
        force_alternate_probability: float = 0.78,
        max_same_side_hits: int = 3,
        cooldown_lo: float = 8.0,
        cooldown_hi: float = 28.0,
        # ── size / risk ───────────────────────────────────────────────────
        continuation_size_mult: float = 1.15,
        pullback_size_mult: float = 0.95,
        shock_probability: float = 0.10,
        shock_mult_lo: float = 1.8,
        shock_mult_hi: float = 3.2,
        max_child_notional_frac: float = 0.00080,
        max_inventory_notional_frac: float = 0.0040,
    ):
        self.agent_id = agent_id
        self.capital = float(capital)

        # Старые настройки сигнала
        self.entry_tau = float(entry_tau)
        self.entry_threshold = float(entry_threshold)
        self.hold_duration_lo = float(hold_duration_lo)
        self.hold_duration_hi = float(hold_duration_hi)
        self.reversal_frac = float(reversal_frac)
        self.notional = self.capital * float(notional_frac)
        self.dec_interval = float(dec_interval)

        # Новые настройки
        self.min_signal_scale = float(min_signal_scale)
        self.signal_spread_mult = float(signal_spread_mult)
        self.vol_scale_mult = float(vol_scale_mult)
        self.velocity_tau = float(velocity_tau)

        self.episode_duration_lo = max(8.0, float(hold_duration_lo))
        self.episode_duration_hi = max(
            self.episode_duration_lo + 1.0,
            float(hold_duration_hi),
        )

        self.leg_duration_lo = float(leg_duration_lo)
        self.leg_duration_hi = float(leg_duration_hi)
        self.pulse_interval_lo = float(pulse_interval_lo)
        self.pulse_interval_hi = float(pulse_interval_hi)
        self.trend_leg_probability = float(trend_leg_probability)
        self.force_alternate_probability = float(force_alternate_probability)
        self.max_same_side_hits = max(1, int(max_same_side_hits))
        self.cooldown_lo = float(cooldown_lo)
        self.cooldown_hi = float(cooldown_hi)

        self.continuation_size_mult = float(continuation_size_mult)
        self.pullback_size_mult = float(pullback_size_mult)
        self.shock_probability = float(shock_probability)
        self.shock_mult_lo = float(shock_mult_lo)
        self.shock_mult_hi = float(shock_mult_hi)
        self.max_child_notional = self.capital * float(max_child_notional_frac)
        self.max_inventory_notional = self.capital * float(max_inventory_notional_frac)

        # Price state
        self._ema = None
        self._last_mid = None
        self._last_state_ts = None
        self._vel_ema = 0.0
        self._abs_move_ema = 0.0

        # Episode state
        self._active = False
        self._trend_dir = 0
        self._leg_dir = 0
        self._episode_end_ts = 0.0
        self._leg_end_ts = 0.0
        self._next_pulse_ts = 0.0
        self._cooldown_until = 0.0

        # Virtual inventory. Это не бухгалтерия биржи, а ограничитель, чтобы
        # агент не накопил гигантский directional bias.
        self._virtual_inventory = 0.0
        self._last_order_dir = 0
        self._same_side_hits = 0

        self._next_dec_ts = time.time() + random.uniform(0.0, self.dec_interval)

    def on_order_filled(self, order_id: str, price: float, volume: float, side) -> None:
        # В этом агенте virtual inventory обновляется при отправке MARKET-ордера.
        # Если позже сделаешь частичные исполнения, можно перенести обновление сюда.
        pass

    def _sign_to_side(self, direction: int):
        return OrderSide.BID if direction > 0 else OrderSide.ASK

    def _side_to_sign(self, side) -> int:
        return 1 if _is_bid_side(side) else -1

    def _update_state(self, mid: float, now: float) -> None:
        if self._ema is None:
            self._ema = mid
            self._last_mid = mid
            self._last_state_ts = now
            return

        dt = max(now - (self._last_state_ts or now), 1e-3)
        move = mid - (self._last_mid if self._last_mid is not None else mid)

        alpha_ema = 1.0 - math.exp(-dt / max(self.entry_tau, 1e-3))
        self._ema = alpha_ema * mid + (1.0 - alpha_ema) * self._ema

        alpha_v = 1.0 - math.exp(-dt / max(self.velocity_tau, 1e-3))
        inst_vel = move / dt

        self._vel_ema = alpha_v * inst_vel + (1.0 - alpha_v) * self._vel_ema
        self._abs_move_ema = alpha_v * abs(move) + (1.0 - alpha_v) * self._abs_move_ema

        self._last_mid = mid
        self._last_state_ts = now

    def _signal(self, mid: float, spread: float) -> float:
        if self._ema is None:
            return 0.0

        # Старый агент делил только на spread. При spread=0.001 это давало
        # слишком частые входы. Здесь есть нижний scale + волатильность.
        scale = max(
            spread * self.signal_spread_mult,
            self._abs_move_ema * self.vol_scale_mult,
            self.min_signal_scale,
        )

        displacement = (mid - self._ema) / scale
        velocity = (self._vel_ema * self.velocity_tau) / scale

        return 0.75 * displacement + 0.25 * velocity

    def _start_episode(self, now: float, signal: float) -> None:
        self._active = True
        self._trend_dir = 1 if signal > 0 else -1
        self._episode_end_ts = now + random.uniform(
            self.episode_duration_lo,
            self.episode_duration_hi,
        )

        # Первый удар чаще по тренду: сначала агент подхватывает impulse,
        # потом начинает ломать его откатами.
        if random.random() < self.trend_leg_probability:
            self._leg_dir = self._trend_dir
        else:
            self._leg_dir = -self._trend_dir

        self._leg_end_ts = now + random.uniform(
            self.leg_duration_lo,
            self.leg_duration_hi,
        )

        self._next_pulse_ts = now
        self._same_side_hits = 0
        self._last_order_dir = 0

    def _finish_episode(self, now: float) -> None:
        self._active = False
        self._trend_dir = 0
        self._leg_dir = 0
        self._episode_end_ts = 0.0
        self._leg_end_ts = 0.0
        self._next_pulse_ts = 0.0
        self._cooldown_until = now + random.uniform(
            self.cooldown_lo,
            self.cooldown_hi,
        )

        self._same_side_hits = 0
        self._last_order_dir = 0

    def _roll_next_leg(self, now: float) -> None:
        # Главное отличие от старого MRA: агент не держит одну сторону долго.
        # Он строит legs: trend -> countertrend -> trend -> shakeout и т.д.
        if self._leg_dir == 0:
            self._leg_dir = self._trend_dir or random.choice([-1, 1])
        else:
            if random.random() < self.force_alternate_probability:
                self._leg_dir *= -1
            else:
                # Иногда возвращаемся в сторону основного impulse.
                if random.random() < self.trend_leg_probability:
                    self._leg_dir = self._trend_dir
                else:
                    self._leg_dir = -self._trend_dir

        self._leg_end_ts = now + random.uniform(
            self.leg_duration_lo,
            self.leg_duration_hi,
        )

        self._next_pulse_ts = now + random.uniform(0.0, self.pulse_interval_hi)
        self._same_side_hits = 0

    def _make_market_order(
        self,
        direction: int,
        mid: float,
        best_bid: float,
        best_ask: float,
        notional: float,
    ):
        from order import Order

        side = self._sign_to_side(direction)
        price = best_ask if side == OrderSide.BID else best_bid
        volume = max(1.0, notional / max(mid, 1e-9))

        o = Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            price=price,
            volume=volume,
            order_type="market",
        )

        # Виртуально считаем, что MARKET исполнился. Это нужно только для
        # ограничения собственного directional bias.
        self._virtual_inventory += direction * volume

        if self._last_order_dir == direction:
            self._same_side_hits += 1
        else:
            self._same_side_hits = 1

        self._last_order_dir = direction

        return o

    def _child_notional(self, direction: int) -> float:
        n = self.notional * math.exp(random.gauss(0.0, 0.35))

        if direction == self._trend_dir:
            n *= self.continuation_size_mult
        else:
            n *= self.pullback_size_mult

        # Редкий крупный удар — wick / fakeout.
        if random.random() < self.shock_probability:
            n *= random.uniform(self.shock_mult_lo, self.shock_mult_hi)

        return max(1.0, min(n, self.max_child_notional))

    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = float(getattr(market_context, "now_ts", None) or time.time())

        if now < self._next_dec_ts:
            return []

        self._next_dec_ts = now + self.dec_interval * random.uniform(0.75, 1.25)

        best_bid, best_ask = _get_best_bid_ask(order_book)

        if best_bid is None or best_ask is None or best_bid >= best_ask:
            return []

        mid = (best_bid + best_ask) / 2.0
        spread = max(best_ask - best_bid, 1e-9)

        self._update_state(mid, now)

        orders = []
        signal = self._signal(mid, spread)

        # ── Запуск episode после заметного движения ───────────────────────
        if not self._active:
            if now < self._cooldown_until:
                return []

            if abs(signal) >= self.entry_threshold:
                self._start_episode(now, signal)
            else:
                return []

        # ── Завершение episode: стараемся частично погасить virtual inventory ─
        if now >= self._episode_end_ts:
            if abs(self._virtual_inventory) * mid > self.notional * 0.50:
                close_dir = -1 if self._virtual_inventory > 0 else 1
                close_notional = min(
                    abs(self._virtual_inventory) * mid,
                    self.max_child_notional,
                )

                orders.append(
                    self._make_market_order(
                        close_dir,
                        mid,
                        best_bid,
                        best_ask,
                        close_notional,
                    )
                )

            self._virtual_inventory = 0.0
            self._finish_episode(now)

            return orders

        # ── Переключение legs ─────────────────────────────────────────────
        if now >= self._leg_end_ts:
            self._roll_next_leg(now)

        if now < self._next_pulse_ts:
            return []

        self._next_pulse_ts = now + random.uniform(
            self.pulse_interval_lo,
            self.pulse_interval_hi,
        )

        direction = self._leg_dir or self._trend_dir or (1 if signal > 0 else -1)

        # Жёсткий ограничитель: нельзя долбить одну сторону слишком долго.
        if direction == self._last_order_dir and self._same_side_hits >= self.max_same_side_hits:
            direction *= -1
            self._leg_dir = direction
            self._leg_end_ts = now + random.uniform(
                self.leg_duration_lo,
                self.leg_duration_hi,
            )
            self._same_side_hits = 0

        # Inventory guard: если агент сам слишком перекосился, следующий удар
        # обязан быть против накопленного перекоса.
        inv_notional = abs(self._virtual_inventory) * mid

        if inv_notional > self.max_inventory_notional:
            direction = -1 if self._virtual_inventory > 0 else 1
            self._leg_dir = direction
            self._same_side_hits = 0

        notional = self._child_notional(direction)

        orders.append(
            self._make_market_order(
                direction,
                mid,
                best_bid,
                best_ask,
                notional,
            )
        )

        return orders


# ══════════════════════════════════════════════════════════════════════════════
#  ФАБРИКА
# ══════════════════════════════════════════════════════════════════════════════

def build_variant_agents(base_capital: float = 100_000_000.0) -> list:
    """
    Возвращает список всех агентов-вариантов с уже прописанными agent_id.
    base_capital — ориентировочный капитал базового UBS-агента.
    """
    agents = [
        # ── UBS variants ──────────────────────────────────────────────────
        Tier1UBS_Scalper("ubs_scalper_1", base_capital * 0.70),
        Tier1UBS_Scalper("ubs_scalper_2", base_capital * 0.55),
        Tier1UBS_Macro("ubs_macro_1", base_capital * 1.20),
        Tier1UBS_Noise("ubs_noise_1", base_capital * 0.45),
        Tier1UBS_Noise("ubs_noise_2", base_capital * 0.40),
        Tier1UBS_Noise("ubs_noise_3", base_capital * 0.35),

        # ── CustomerAgg variants ──────────────────────────────────────────
        CustomerAgg_AlgoHeavy("caf_algo_1", base_capital * 3.0),
        CustomerAgg_AlgoHeavy("caf_algo_2", base_capital * 2.0),
        CustomerAgg_CorpBurst("caf_corp_1", base_capital * 3.0),
        CustomerAgg_RetailBias("caf_retail_1", base_capital * 3.0),

        # ── CorporateFlow variants ────────────────────────────────────────
        CorporateFlowManager_Tactical("corp_tact_1", base_capital * 3.5),
        CorporateFlowManager_Strategic("corp_strat_1", base_capital * 3.5),

        # ── RandomPulseAgents ─────────────────────────────────────────────
        RandomPulseAgent(
            "rpa_fast_1",
            base_capital * 0.25,
            pulse_interval_lo=0.3,
            pulse_interval_hi=1.5,
            base_notional_frac=0.00025,
            lognormal_sigma=0.8,
            burst_prob=0.10,
        ),
        RandomPulseAgent(
            "rpa_fast_2",
            base_capital * 0.20,
            pulse_interval_lo=0.4,
            pulse_interval_hi=2.0,
            base_notional_frac=0.00020,
            lognormal_sigma=0.7,
            burst_prob=0.08,
        ),
        RandomPulseAgent(
            "rpa_slow_1",
            base_capital * 0.30,
            pulse_interval_lo=2.0,
            pulse_interval_hi=8.0,
            base_notional_frac=0.00040,
            lognormal_sigma=1.3,
            burst_prob=0.06,
            burst_size_lo=2,
            burst_size_hi=4,
        ),

        # ── MomentumReversalAgents ────────────────────────────────────────
        # MomentumReversalAgent(
        #    "mra_1",
        #    base_capital * 0.45,
        #     entry_tau=12.0,
        #     entry_threshold=2.2,
        #    hold_duration_lo=22.0,
        #     hold_duration_hi=75.0,
        #    reversal_frac=0.55,
        #    notional_frac=0.00120,
        #     dec_interval=0.45,
        #     leg_duration_lo=1.8,
        #    leg_duration_hi=5.0,
        #    pulse_interval_lo=0.35,
        #     pulse_interval_hi=1.20,
        #     max_same_side_hits=3,
    #     shock_probability=0.12,
        # ),

        # MomentumReversalAgent(
        #    "mra_2",
        #     base_capital * 0.35,
        #     entry_tau=24.0,
        #     entry_threshold=2.8,
        #     hold_duration_lo=35.0,
        #     hold_duration_hi=110.0,
        #     reversal_frac=0.60,
        #    notional_frac=0.00090,
        #    dec_interval=0.80,
        #    leg_duration_lo=3.0,
        #    leg_duration_hi=9.0,
        #    pulse_interval_lo=0.70,
        #     pulse_interval_hi=2.00,
        #     max_same_side_hits=3,
    #     shock_probability=0.08,
        # ),
    ]

    return agents