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

from tier1_ubs             import Tier1UBSBank
from backend.agents.customer_aggregator_flow import CustomerAggregatorFlow
from corporate_flow_manager   import CorporateFlowManager
from order                    import OrderSide


# ══════════════════════════════════════════════════════════════════════════════
#  TIER-1 UBS VARIANTS
#  Оригинал: fast τ=3s, slow τ=45s, dec 0.10–0.28s, max_inv 0.8%
# ══════════════════════════════════════════════════════════════════════════════

class Tier1UBS_Scalper2(Tier1UBSBank):
    """
    HFT-режим: очень быстрый ритм, маленький инвентарь, агрессивные волны.
    Добавляет высокочастотный шум: много мелких ударов в обе стороны.

    Отличия от оригинала:
      - decision interval 0.05–0.15s  (в 2× быстрее)
      - mid_fast_tau = 1.5s           (ловит самые короткие движения)
      - mid_slow_tau = 20s            (менее "долгая" память)
      - max_inv_frac  = 0.004         (вдвое меньше — агрессивно хеджирует)
      - flow_sigma    = 0.09          (больше "всплесков" активности)
      - wave_end продолжительность 4–14s (короткие волны)
    """
    def __init__(self, agent_id: str, capital: float):
        super().__init__(agent_id, capital)
        self.dec_interval_min = 0.05
        self.dec_interval_max = 0.15
        self.mid_fast_tau     = 1.5
        self.mid_slow_tau     = 20.0
        self.max_inv_frac     = 0.004
        self.max_gross_notional_frac = 0.0010
        self.flow_mu    = 0.32
        self.flow_sigma = 0.09
        self._wave_dur_lo = 4.0
        self._wave_dur_hi = 14.0

    def _maybe_update_wave(self, feat, flow, inv_frac, tape_bias, depth_state, now):
        if self.wave_dir != 0 and now < self.wave_end_ts:
            return
        self.wave_dir = 0

        mid    = feat["mid"]
        trend  = feat["trend"]
        vel    = feat["velocity"]
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

        self.wave_dir    = direction
        self.wave_end_ts = now + random.uniform(self._wave_dur_lo, self._wave_dur_hi)

        if self.last_wave_dir == direction:
            self.same_dir_wave_count += 1
        else:
            self.last_wave_dir        = direction
            self.same_dir_wave_count  = 1


class Tier1UBS_Macro2(Tier1UBSBank):
    """
    Макро-режим: медленный и крупный. Реагирует на долгосрочные дрейфы,
    выставляет большие стены и редко, но мощно хеджируется.

    Добавляет низкочастотное "сопротивление" трендам (mean-reversion давление).

    Отличия от оригинала:
      - decision interval 0.35–0.90s  (медленнее — реже мешает HFT)
      - mid_fast_tau = 8s, slow τ = 120s  (ловит среднесрочный дрейф)
      - max_inv_frac  = 0.014         (терпит крупную позицию дольше)
      - base_notional × 1.8           (крупнее ордера)
      - flow_mu = 0.22                (в среднем менее активен)
      - anti-trend wall — агрессивнее (срабатывает при stretch > 5)
    """
    def __init__(self, agent_id: str, capital: float):
        super().__init__(agent_id, capital)
        self.dec_interval_min = 0.35
        self.dec_interval_max = 0.90
        self.mid_fast_tau     = 8.0
        self.mid_slow_tau     = 120.0
        self.max_inv_frac     = 0.014
        self.max_gross_notional_frac = 0.0030
        self.flow_mu    = 0.22
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
        """Срабатывает раньше — при stretch > 5 (оригинал: 8)."""
        mid    = feat["mid"]
        spread = max(feat["spread"], 1e-6)
        trend  = feat["trend"]

        if mid <= 0 or self.mid_ema_slow is None:
            return []

        stretch = (mid - self.mid_ema_slow) / spread

        if abs(trend) < 0.25 and abs(self.trend_mem) < 0.20:
            return []
        if abs(stretch) < 5.0 and abs(tape_bias) < 0.35:
            return []

        return super()._anti_trend_wall_orders(feat, inv_frac, tape_bias)


class Tier1UBS_Noise2(Tier1UBSBank):
    """
    "Шумовой" LP: постоянно цитирует маленькими объёмами с случайным jitter,
    без выраженных волн. Добавляет tick-by-tick неравномерность без тренда.

    Отличия от оригинала:
      - decision interval 0.08–0.20s
      - mid_fast_tau = 2s, slow τ = 30s
      - max_inv_frac  = 0.005
      - flow_mu = 0.45, sigma = 0.11  (всегда "немного активен")
      - волны выключены (p → 0)
      - micro noise × 3.5 (было 2.5)
    """
    def __init__(self, agent_id: str, capital: float):
        super().__init__(agent_id, capital)
        self.dec_interval_min = 0.08
        self.dec_interval_max = 0.20
        self.mid_fast_tau     = 2.0
        self.mid_slow_tau     = 30.0
        self.max_inv_frac     = 0.005
        self.max_gross_notional_frac = 0.0008
        self.flow_mu    = 0.45
        self.flow_sigma = 0.11

    def _maybe_update_wave(self, feat, flow, inv_frac, tape_bias, depth_state, now):
        self.wave_dir = 0

    def _micro_noise_orders(self, feat, flow, inv_frac):
        orders = super()._micro_noise_orders(feat, flow, inv_frac)
        for o in orders:
            o.volume = max(1.0, o.volume * 3.5)   # было 2.5
        return orders


# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOMER AGGREGATOR FLOW VARIANTS
#  Оригинал: retail 0.55, corp 0.12, algo 0.35
# ══════════════════════════════════════════════════════════════════════════════

class CustomerAgg_AlgoHeavy2(CustomerAggregatorFlow):
    """
    Доминируют algo-crumbs: много мелких, быстрых market-ударов.
    Создаёт высокочастотное давление с быстрым переключением сторон.

    retail 0.30 | corp 0.08 | algo 0.80
    regime_mult 0.60–1.90
    max_orders_per_call = 22
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
        self._regime_mult    = self._rng.uniform(self._regime_mult_lo, self._regime_mult_hi)
        # FIX: режим меняется быстрее → больше переходов → шум
        self._next_regime_ts = now + self._rng.uniform(5.0, 20.0)   # было 12-40


class CustomerAgg_CorpBurst2(CustomerAggregatorFlow):
    """
    Корпоративные пакеты с редкими, но крупными всплесками.
    Создаёт периодические "удары" — эффект скачков цены.

    retail 0.20 | corp 0.35 | algo 0.15
    max_orders_per_call = 10
    regime (0.20–3.00) — большой диапазон, значит будут тихие и бурные периоды
    deadline corp: 3–20s (короче = выше urgency)
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
        self._max_orders_per_call  = 10
        self._max_active_tickets   = 16

    def _roll_regime(self, now: float):
        if now < self._next_regime_ts:
            return
        # FIX: шире диапазон + короче удержание → резкие всплески чаще сменяются тишиной
        self._regime_mult    = self._rng.uniform(0.20, 3.00)         # было 0.30–2.50
        self._next_regime_ts = now + self._rng.uniform(8.0, 30.0)    # было 30–90s

    def _maybe_spawn_ticket(self, now, best_bid, best_ask, bids, asks):
        before_ids = {id(t) for t in self._tickets}
        super()._maybe_spawn_ticket(now, best_bid, best_ask, bids, asks)
        for t in self._tickets:
            if id(t) not in before_ids and t.style == "corp":
                # FIX: дедлайн ещё короче → urgency выше → более резкие удары
                t.deadline_ts = now + self._rng.uniform(3.0, 20.0)   # было 5–30s


class CustomerAgg_RetailBias2(CustomerAggregatorFlow):
    """
    Преобладает розничный поток: частые мелкие заявки с непредсказуемым направлением.
    Имитирует активность розницы в ключевые часы (открытие сессий и т.п.).

    retail 0.85 | corp 0.08 | algo 0.20
    rebalance_strength = 0.30 (слабый неттинг — розница не нетирует)
    max_orders_per_call = 14
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
        self._regime_mult    = self._rng.uniform(0.50, 1.60)
        # FIX: чуть быстрее → мелкие ритмические колебания активности
        self._next_regime_ts = now + self._rng.uniform(8.0, 30.0)    # было 15–45


# ══════════════════════════════════════════════════════════════════════════════
#  CORPORATE FLOW MANAGER VARIANTS
#  Оригинал: 8–25 flows, session london 14–25, dur 18–50min
# ══════════════════════════════════════════════════════════════════════════════

class CorporateFlowManager_Tactical2(CorporateFlowManager):
    """
    Тактический (краткосрочный) корп-поток.
    Много коротких исполнений → частая смена давления → шероховатость.

    FIX: возвращены более высокие flow-count и более частый next_check.
    """
    _side_max_imbalance = 2   # было 1 — слишком жёсткое чередование давало синтетику

    def __init__(self, agent_id: str, capital_pool: float):
        super().__init__(agent_id, capital_pool)
        # FIX: 3-6s вместо 8-15s → спавн потоков чаще
        self.next_check_ts  = time.time() + random.uniform(3.0, 6.0)
        self._side_counts   = {OrderSide.BID: 0, OrderSide.ASK: 0}

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
        before = len(self.active_agents)
        super()._cleanup()
        counts = {OrderSide.BID: 0, OrderSide.ASK: 0}
        for a in self.active_agents:
            s = getattr(a, "side_bias", None)
            if s in counts:
                counts[s] += 1
        self._side_counts = counts

    def _desired_flow_count(self, market_context, now: float):
        session = self._get_session(market_context, now)
        # FIX: восстановлены умеренно высокие значения (компромисс между оригиналом и нёрфом)
        if session == "asia":
            return random.randint(4, 8)     # было 2-4 (нёрф), оригинал 6-12
        if session == "london":
            return random.randint(8, 16)    # было 4-8 (нёрф), оригинал 16-28
        if session == "us":
            return random.randint(7, 14)    # было 4-7 (нёрф), оригинал 14-24
        return random.randint(4, 8)         # было 2-5 (нёрф), оригинал 8-16

    def _spawn_flow(self, market_context, now: float):
        session = self._get_session(market_context, now)

        # FIX: немного больше капитала на поток (но не оригинальные значения)
        if session == "asia":
            cap_frac = random.uniform(0.003, 0.015)   # было 0.002-0.010
        elif session == "london":
            cap_frac = random.uniform(0.005, 0.025)   # было 0.003-0.015
        elif session == "us":
            cap_frac = random.uniform(0.005, 0.020)   # было 0.003-0.012
        else:
            cap_frac = random.uniform(0.003, 0.012)   # было 0.002-0.008

        from backend.agents.corporate_flow_twap import CorporateFlowTWAP
        from backend.core.order import OrderSide

        twap_capital = self.capital_pool * cap_frac
        side         = self._pick_direction()

        agent = CorporateFlowTWAP(
            agent_id=f"{self.agent_id}_tact_{random.randint(1000, 9999)}",
            capital=twap_capital,
        )

        target_notional = twap_capital * random.uniform(0.01, 0.05)

        # Короткие сессии — быстрее истекают, меньше накапливается
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
            # FIX: 3-6s вместо 8-15s
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
        return all_orders


class CorporateFlowManager_Strategic2(CorporateFlowManager):
    """
    Стратегический (долгосрочный) корп-поток.
    Мало, но долгих исполнений → стабильный фоновый дрейф разных сторон.

    FIX: немного больше потоков + умеренный капитал.
    """
    _side_max_imbalance = 1

    def __init__(self, agent_id: str, capital_pool: float):
        super().__init__(agent_id, capital_pool)
        self.next_check_ts = time.time() + random.uniform(10.0, 18.0)
        self._side_counts  = {OrderSide.BID: 0, OrderSide.ASK: 0}

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
        # FIX: чуть больше потоков чем в нёрфе
        if session == "asia":
            return random.randint(2, 4)    # было 1-2
        if session == "london":
            return random.randint(3, 6)    # было 2-4
        if session == "us":
            return random.randint(3, 5)    # было 2-3
        return random.randint(2, 3)        # было 1-2

    def _spawn_flow(self, market_context, now: float):
        session = self._get_session(market_context, now)

        if session == "asia":
            cap_frac = random.uniform(0.015, 0.045)   # было 0.010-0.030
        elif session == "london":
            cap_frac = random.uniform(0.020, 0.060)   # было 0.015-0.045
        elif session == "us":
            cap_frac = random.uniform(0.018, 0.055)   # было 0.012-0.040
        else:
            cap_frac = random.uniform(0.012, 0.035)   # было 0.008-0.025

        from backend.agents.corporate_flow_twap import CorporateFlowTWAP

        twap_capital = self.capital_pool * cap_frac
        side         = self._pick_direction()

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
        return all_orders


# ══════════════════════════════════════════════════════════════════════════════
#  NEW: RandomPulseAgent
#  Чистый источник несистематического шума.
#  Генерирует случайные market-ордера с log-normal объёмом без какого-либо сигнала.
#  Это главный инструмент для добавления tick-шума на 1m свечах.
# ══════════════════════════════════════════════════════════════════════════════

class RandomPulseAgent2:
    """
    Агент-пульсатор: каждые pulse_interval_lo..hi секунд выбирает случайную
    сторону (50/50) и отправляет ордер с lognormal-объёмом.

    Параметры:
      pulse_interval_lo / hi  — интервал между импульсами (сек)
      base_notional_frac      — базовый нотионал как доля капитала
      lognormal_sigma         — σ log-normal (чем выше — тем больше редкие крупные удары)
      burst_prob              — вероятность "burst": 3-7 ударов подряд в одну сторону
      burst_size              — сколько ударов в burst

    Рекомендуемое использование:
      RandomPulseAgent("rpa_1", capital * 0.20, pulse_interval_lo=0.3, lognormal_sigma=0.8)
      RandomPulseAgent("rpa_2", capital * 0.15, pulse_interval_lo=1.0, lognormal_sigma=1.2)
    """

    # Режимы активности агента
    _QUIET  = "quiet"   # тихий: редкие мелкие удары — имитирует консолидацию
    _ACTIVE = "active"  # активный: нормальные удары + burst — имитирует импульс

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
        # Режимы: QUIET / ACTIVE
        quiet_dur_lo: float = 20.0,   # мин. длительность тихого периода (сек)
        quiet_dur_hi: float = 90.0,   # макс.
        active_dur_lo: float = 5.0,   # мин. длительность активного периода (сек)
        active_dur_hi: float = 25.0,  # макс.
        quiet_interval_mult: float = 6.0,   # во сколько раз реже стреляет в QUIET
        quiet_volume_mult: float = 0.25,    # во сколько раз меньше объём в QUIET
    ):
        self.agent_id          = agent_id
        self.capital           = capital
        self.pulse_interval_lo = pulse_interval_lo
        self.pulse_interval_hi = pulse_interval_hi
        self.base_notional     = capital * base_notional_frac
        self.lognormal_sigma   = lognormal_sigma
        self.burst_prob        = burst_prob
        self.burst_size_lo     = burst_size_lo
        self.burst_size_hi     = burst_size_hi
        self.quiet_dur_lo      = quiet_dur_lo
        self.quiet_dur_hi      = quiet_dur_hi
        self.active_dur_lo     = active_dur_lo
        self.active_dur_hi     = active_dur_hi
        self.quiet_interval_mult = quiet_interval_mult
        self.quiet_volume_mult   = quiet_volume_mult

        # Начинаем в тихом режиме — не с импульса
        self._regime           = self._QUIET
        self._regime_end_ts    = time.time() + random.uniform(quiet_dur_lo, quiet_dur_hi)
        self._next_pulse_ts    = time.time() + random.uniform(pulse_interval_lo * quiet_interval_mult,
                                                               pulse_interval_hi * quiet_interval_mult)
        self._burst_remaining  = 0
        self._burst_side       = None

    def _switch_regime(self, now: float) -> None:
        """Проверяем и при необходимости переключаем режим."""
        if now < self._regime_end_ts:
            return
        if self._regime == self._QUIET:
            self._regime       = self._ACTIVE
            self._regime_end_ts = now + random.uniform(self.active_dur_lo, self.active_dur_hi)
        else:
            self._regime       = self._QUIET
            self._regime_end_ts = now + random.uniform(self.quiet_dur_lo, self.quiet_dur_hi)
            # При выходе из активного режима сбрасываем burst
            self._burst_remaining = 0

    def on_order_filled(self, order_id: str, price: float, volume: float, side) -> None:
        pass

    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = float(getattr(market_context, "now_ts", None) or time.time())

        self._switch_regime(now)

        if now < self._next_pulse_ts:
            return []

        # Планируем следующий удар с учётом режима
        if self._regime == self._QUIET:
            self._next_pulse_ts = now + random.uniform(
                self.pulse_interval_lo * self.quiet_interval_mult,
                self.pulse_interval_hi * self.quiet_interval_mult,
            )
        else:
            self._next_pulse_ts = now + random.uniform(self.pulse_interval_lo, self.pulse_interval_hi)

        # burst mode (только в ACTIVE)
        if self._regime == self._ACTIVE and self._burst_remaining > 0:
            side = self._burst_side
            self._burst_remaining -= 1
        else:
            side = OrderSide.BID if random.random() < 0.50 else OrderSide.ASK
            if self._regime == self._ACTIVE and random.random() < self.burst_prob:
                self._burst_remaining = random.randint(self.burst_size_lo, self.burst_size_hi) - 1
                self._burst_side      = side

        # log-normal volume (в QUIET сильно меньше)
        vol_mult = self.quiet_volume_mult if self._regime == self._QUIET else 1.0
        notional = self.base_notional * vol_mult * math.exp(
            random.gauss(0.0, self.lognormal_sigma)
        )
        notional = max(notional, 1.0)

        best_bid = order_book._best_bid_price()
        best_ask = order_book._best_ask_price()
        if best_bid is None or best_ask is None:
            return []
        mid    = (best_bid + best_ask) / 2.0
        price  = mid if mid > 0 else 1.0
        volume = max(1.0, notional / price)

        from backend.core.order import Order
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
#  Краткосрочный momentum-follower с жёстким стоп-реверсом.
#  Создаёт "зигзаги": разгоняет движение, потом резко разворачивается.
#  Типичный источник "wick" и fake-out на 1m свечах.
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  NEW: MomentumReversalAgent2
# ══════════════════════════════════════════════════════════════════════════════

class MomentumReversalAgent2:
    """
    ZigZag / fakeout taker, версия для agent_variants2.py.

    Задача агента — НЕ тащить рынок линейно в одну сторону, а после появления
    направленного движения создать серию локальных continuation/pullback-волн:

        impulse -> продолжение -> откат -> новая попытка -> shakeout -> откат

    Эта версия самодостаточная для agent_variants2.py:
      - не использует _get_best_bid_ask()
      - не использует _is_bid_side()
      - работает только через order_book._best_bid_price() / _best_ask_price()
      - импорт Order делает локально внутри _make_market_order()
    """

    def __init__(
        self,
        agent_id: str,
        capital: float,
        entry_tau: float = 14.0,
        entry_threshold: float = 2.6,
        hold_duration_lo: float = 22.0,
        hold_duration_hi: float = 90.0,
        reversal_frac: float = 0.60,
        notional_frac: float = 0.00085,
        dec_interval: float = 0.70,
        # ── signal scaling ────────────────────────────────────────────────
        min_signal_scale: float = 0.005,
        signal_spread_mult: float = 5.0,
        vol_scale_mult: float = 3.0,
        velocity_tau: float = 10.0,
        # ── episode / zigzag control ──────────────────────────────────────
        leg_duration_lo: float = 2.5,
        leg_duration_hi: float = 8.0,
        pulse_interval_lo: float = 0.55,
        pulse_interval_hi: float = 1.80,
        trend_leg_probability: float = 0.55,
        force_alternate_probability: float = 0.80,
        max_same_side_hits: int = 3,
        cooldown_lo: float = 10.0,
        cooldown_hi: float = 35.0,
        # ── size / risk ───────────────────────────────────────────────────
        continuation_size_mult: float = 1.10,
        pullback_size_mult: float = 1.00,
        shock_probability: float = 0.08,
        shock_mult_lo: float = 1.6,
        shock_mult_hi: float = 2.8,
        max_child_notional_frac: float = 0.00070,
        max_inventory_notional_frac: float = 0.0035,
    ):
        self.agent_id = agent_id
        self.capital = float(capital)

        # Старые настройки, оставлены для совместимости с фабрикой
        self.entry_tau = float(entry_tau)
        self.entry_threshold = float(entry_threshold)
        self.hold_duration_lo = float(hold_duration_lo)
        self.hold_duration_hi = float(hold_duration_hi)
        self.reversal_frac = float(reversal_frac)
        self.notional = self.capital * float(notional_frac)
        self.dec_interval = float(dec_interval)

        # Новые настройки сигнала
        self.min_signal_scale = float(min_signal_scale)
        self.signal_spread_mult = float(signal_spread_mult)
        self.vol_scale_mult = float(vol_scale_mult)
        self.velocity_tau = float(velocity_tau)

        # Episode duration берём из старых hold_duration_*,
        # чтобы старые параметры продолжали влиять на поведение.
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

        # ── Price state ───────────────────────────────────────────────────
        self._ema = None
        self._last_mid = None
        self._last_state_ts = None

        self._vel_ema = 0.0
        self._abs_move_ema = 0.0

        # ── Episode state ─────────────────────────────────────────────────
        self._active = False
        self._trend_dir = 0
        self._leg_dir = 0

        self._episode_end_ts = 0.0
        self._leg_end_ts = 0.0
        self._next_pulse_ts = 0.0
        self._cooldown_until = 0.0

        # Virtual inventory — не бухгалтерия биржи, а внутренний ограничитель,
        # чтобы агент не стал ещё одним однонаправленным толкателем.
        self._virtual_inventory = 0.0

        self._last_order_dir = 0
        self._same_side_hits = 0

        self._next_dec_ts = time.time() + random.uniform(0.0, self.dec_interval)

    def on_order_filled(self, order_id: str, price: float, volume: float, side) -> None:
        # Сейчас virtual inventory обновляется в момент отправки MARKET-ордера.
        # Если позже появится частичное исполнение, можно перенести обновление сюда.
        pass

    def _best_bid_ask(self, order_book):
        try:
            best_bid = order_book._best_bid_price()
            best_ask = order_book._best_ask_price()
            return best_bid, best_ask
        except Exception:
            return None, None

    def _sign_to_side(self, direction: int):
        return OrderSide.BID if direction > 0 else OrderSide.ASK

    def _update_state(self, mid: float, now: float) -> None:
        if self._ema is None:
            self._ema = mid
            self._last_mid = mid
            self._last_state_ts = now
            return

        dt = max(now - (self._last_state_ts or now), 1e-3)
        prev_mid = self._last_mid if self._last_mid is not None else mid
        move = mid - prev_mid

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

        # В старой версии сигнал делился только на spread.
        # При spread около 0.001 это делало входы слишком частыми.
        # Теперь scale учитывает:
        #   - spread * multiplier
        #   - recent absolute movement
        #   - минимальный нижний порог
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

        # Первый leg чаще в сторону импульса, но не всегда.
        # Это даёт поведение:
        #   impulse continuation -> pullback -> fakeout -> continuation
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
        # Главное: агент не держит одну сторону долго.
        # Он чередует legs и тем самым делает движение похожим на синюю линию.
        if self._leg_dir == 0:
            self._leg_dir = self._trend_dir or random.choice([-1, 1])
        else:
            if random.random() < self.force_alternate_probability:
                self._leg_dir *= -1
            else:
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

    def _child_notional(self, direction: int) -> float:
        # lognormal-разброс нужен, чтобы удары были не одинаковыми.
        n = self.notional * math.exp(random.gauss(0.0, 0.35))

        if direction == self._trend_dir:
            n *= self.continuation_size_mult
        else:
            n *= self.pullback_size_mult

        # Редкий более крупный удар — wick / fakeout.
        if random.random() < self.shock_probability:
            n *= random.uniform(self.shock_mult_lo, self.shock_mult_hi)

        return max(1.0, min(n, self.max_child_notional))

    def _make_market_order(
        self,
        direction: int,
        mid: float,
        best_bid: float,
        best_ask: float,
        notional: float,
    ):
        from backend.core.order import Order

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

        # Виртуально считаем, что MARKET исполнился.
        self._virtual_inventory += direction * volume

        if self._last_order_dir == direction:
            self._same_side_hits += 1
        else:
            self._same_side_hits = 1

        self._last_order_dir = direction

        return o

    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = float(getattr(market_context, "now_ts", None) or time.time())

        if now < self._next_dec_ts:
            return []

        self._next_dec_ts = now + self.dec_interval * random.uniform(0.75, 1.25)

        best_bid, best_ask = self._best_bid_ask(order_book)

        if best_bid is None or best_ask is None:
            return []

        if best_bid >= best_ask:
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

        # ── Завершение episode ────────────────────────────────────────────
        if now >= self._episode_end_ts:
            # Частично гасим virtual inventory, чтобы агент не оставлял
            # после себя сильный однонаправленный перекос.
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

        # ── Переключение leg ──────────────────────────────────────────────
        if now >= self._leg_end_ts:
            self._roll_next_leg(now)

        if now < self._next_pulse_ts:
            return []

        self._next_pulse_ts = now + random.uniform(
            self.pulse_interval_lo,
            self.pulse_interval_hi,
        )

        direction = self._leg_dir or self._trend_dir or (1 if signal > 0 else -1)

        # Нельзя долго долбить одну сторону.
        # Это главный предохранитель против линейных проходов.
        if direction == self._last_order_dir and self._same_side_hits >= self.max_same_side_hits:
            direction *= -1
            self._leg_dir = direction

            self._leg_end_ts = now + random.uniform(
                self.leg_duration_lo,
                self.leg_duration_hi,
            )

            self._same_side_hits = 0

        # Inventory guard: если агент сам накопил перекос,
        # следующий удар обязан быть против этого перекоса.
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
#  ФАБРИКА — удобный способ создать весь набор вариантов разом
# ══════════════════════════════════════════════════════════════════════════════

def build_variant_agents2(base_capital: float = 100_000_000.0) -> list:
    """
    Возвращает список всех агентов-вариантов с уже прописанными agent_id.
    base_capital — ориентировочный капитал базового UBS-агента.

    Рекомендуемый порядок добавления в симулятор (вместе с оригиналами):
      оригиналы + build_variant_agents(capital)
    """
    agents = [
        # ── UBS variants ──────────────────────────────────────────────────
        Tier1UBS_Scalper2("ubs_scalper_3", base_capital * 0.70),
        Tier1UBS_Scalper2("ubs_scalper_4", base_capital * 0.55),
        Tier1UBS_Macro2  ("ubs_macro_2",   base_capital * 1.20),
        Tier1UBS_Noise2  ("ubs_noise_4",   base_capital * 0.45),
        Tier1UBS_Noise2  ("ubs_noise_5",   base_capital * 0.40),
        Tier1UBS_Noise2  ("ubs_noise_6",   base_capital * 0.35),   # NEW: 3й noise agent

        # ── CustomerAgg variants ──────────────────────────────────────────
        CustomerAgg_AlgoHeavy2 ("caf_algo_3",   base_capital * 3.0),
        CustomerAgg_AlgoHeavy2 ("caf_algo_4",   base_capital * 2.0),   # NEW: 2й algo
        CustomerAgg_CorpBurst2 ("caf_corp_2",   base_capital * 3.0),
        CustomerAgg_RetailBias2("caf_retail_2", base_capital * 3.0),

        # ── CorporateFlow variants ────────────────────────────────────────
        # FIX: capital multiplier 1.5 → 3.5 (был занижен)
        CorporateFlowManager_Tactical2 ("corp_tact_2",  base_capital * 3.5),
        CorporateFlowManager_Strategic2("corp_strat_2", base_capital * 3.5),

        # ── NEW: RandomPulseAgents ────────────────────────────────────────
        # rpa_fast: высокочастотный мелкий шум (0.3-1.5s между ударами)
        RandomPulseAgent2(
            "rpa_fast_3",
            base_capital * 0.25,
            pulse_interval_lo=0.3,
            pulse_interval_hi=1.5,
            base_notional_frac=0.00025,
            lognormal_sigma=0.8,
            burst_prob=0.10,
        ),
        RandomPulseAgent2(
            "rpa_fast_4",
            base_capital * 0.20,
            pulse_interval_lo=0.4,
            pulse_interval_hi=2.0,
            base_notional_frac=0.00020,
            lognormal_sigma=0.7,
            burst_prob=0.08,
        ),
        # rpa_slow: редкие но крупные удары (2-8s, lognormal σ=1.3)
        RandomPulseAgent2(
            "rpa_slow_2",
            base_capital * 0.30,
            pulse_interval_lo=2.0,
            pulse_interval_hi=8.0,
            base_notional_frac=0.00040,
            lognormal_sigma=1.3,
            burst_prob=0.06,
            burst_size_lo=2,
            burst_size_hi=4,
        ),

        # ── NEW: MomentumReversalAgents ───────────────────────────────────
        # mra_3 быстрее и активнее: делает локальную пилу внутри импульса.
        # MomentumReversalAgent2(
        #     "mra_3",
        #     base_capital * 0.40,
        #     entry_tau=14.0,
        #    entry_threshold=2.5,
        #    hold_duration_lo=24.0,
        #    hold_duration_hi=85.0,
        #    reversal_frac=0.60,
        #    notional_frac=0.00100,
        #     dec_interval=0.60,
        #    leg_duration_lo=2.2,
        #    leg_duration_hi=6.5,
        #    pulse_interval_lo=0.45,
        #    pulse_interval_hi=1.50,
        #    max_same_side_hits=3,
        #    shock_probability=0.10,
        # ),

        # # mra_4 медленнее и крупнее по структуре: делает более длинные откаты/выносы.
        # MomentumReversalAgent2(
        #    "mra_4",
        #    base_capital * 0.32,
        #    entry_tau=28.0,
        #    entry_threshold=3.2,
        #    hold_duration_lo=40.0,
        #     hold_duration_hi=130.0,
        #     reversal_frac=0.65,
        #     notional_frac=0.00080,
        #    dec_interval=1.10,
        #    leg_duration_lo=4.0,
        #    leg_duration_hi=11.0,
        #    pulse_interval_lo=0.85,
        #     pulse_interval_hi=2.40,
        #     max_same_side_hits=3,
        #     shock_probability=0.07,
        # ),
    ]
    return agents