import numpy as np
from collections import deque
import random
import math
from dataclasses import dataclass


@dataclass
class MarketPhase:
    name: str
    confidence: float
    reason: dict


class MarketContextAdvanced:
    """
    Централизованный контекст рынка.
    - update_clock(): один раз на тик задаём wall-clock (UTC epoch) и monotonic (для dt)
    - update(): обновление статистик/фаз/сессий на основе tick(mid/snapshot)
    """

    def __init__(self, maxlen_short=100, maxlen_medium=500, maxlen_long=2000, seed=None):
        # ----------------------------
        # Clock (централизованное время)
        # ----------------------------
        self.now_ts = None          # wall-clock epoch seconds (UTC)
        self.mono_ts = None         # monotonic seconds
        self.dt = 0.0
        self._last_mono_ts = None

        self.current_utc_time = 0   # seconds in day [0..86399]
        self.utc_hour = 0
        self.utc_minute = 0
        self.utc_second = 0
        self._day_id = None         # int(now_ts)//86400

        self.last_tick = None

        # ----------------------------
        # RNG (рандом есть, но управляемый)
        # ----------------------------
        self.run_seed = seed if seed is not None else random.randrange(1 << 30)
        self.rng = random.Random(self.run_seed)
        self.np_rng = np.random.default_rng(self.run_seed)

        # ----------------------------
        # State buffers
        # ----------------------------
        self.mid_prices_short = deque(maxlen=maxlen_short)
        self.mid_prices_medium = deque(maxlen=maxlen_medium)
        self.mid_prices_long = deque(maxlen=maxlen_long)

        self.snapshots = deque(maxlen=maxlen_long)
        self.sweep_events = deque(maxlen=maxlen_medium)

        self.phase = MarketPhase("undefined", 0.0, {})
        self.previous_phase_name = None
        self.phase_persistence = 0

        self.micro_trends = deque(maxlen=200)
        self.cluster_imbalances = deque(maxlen=200)
        self.volatility_levels = deque(maxlen=200)
        self.structural_swings = deque(maxlen=200)

        self.market_memory_trace = deque(maxlen=10)  # Память рыночных дней
        self._last_trace_bucket = None               # чтобы не спамить trace

        self.current_session = "asia"
        self.session_progress_ratio = 0.0

        self.bank_flow_pressure = 0.0
        self.institutional_bias = 0.0
        self.macro_bias_vector = 0.0

        self.volatility_regime = "normal"
        self.session_type = "average"
        self.rollover_flag = False

        self.exhaustion_signal = False
        self.sweep_detected = False
        self.cluster_disbalance = 0.0

        # daily randomized profile (но стабильный внутри дня)
        self.daily_macro_scenario = None
        self.session_type_profile = None
        self.volatility_amplitude = 1.0
        self.daily_phase_shift = 0.0
        self.volatility_noise_factor = 1.0
        self.current_activity_level = 1.0

        self.demand_supply_skew = 0.0
        self.sticky_price_zone = None
        self.event_shock_level = 0.0

        # инициализация профиля "на сейчас"
        self._roll_daily_profile(force=True)

    # market_context.py

    def update_clock(self, now_ts: float, mono_ts=None, **_):
        now_ts = float(now_ts)
        self.now_ts = now_ts
        self.now_ms = int(now_ts * 1000)

        # dt считаем только если пришёл mono_ts (монотонные часы)
        if mono_ts is None:
            return

        mono_ts = float(mono_ts)
        last = getattr(self, "_last_mono_ts", None)

        if last is None:
            self.dt = 0.0
        else:
            dt = mono_ts - last
            if dt < 0.0:
                dt = 0.0
            if dt > 5.0:
                dt = 5.0
            self.dt = dt

        self.mono_ts = mono_ts
        self._last_mono_ts = mono_ts

    def get(self, key, default=None):
        return getattr(self, key, default)

    def _roll_daily_profile(self, force=False):
        """
        Рандомизатор дня/сессий.
        Важно: он не должен "дёргаться" внутри одного прогона/дня.
        """
        # если force=False можно оставить правило, но сейчас проще всегда роллить при смене дня
        self.daily_macro_scenario = self.rng.choice([
            "neutral_day", "fed_expectation", "ecb_dovish", "eur_surge", "usd_liquidity_drain"
        ])
        self.session_type_profile = {
            "asia": self.rng.choice(["quiet", "volatile", "spiky"]),
            "london": self.rng.choice(["trending", "reversal", "balanced"]),
            "newyork": self.rng.choice(["reactive", "continuation", "range"])
        }
        self.volatility_amplitude = self.rng.uniform(0.8, 1.5)

        self.daily_phase_shift = self.rng.uniform(0, 2 * math.pi)
        self.volatility_noise_factor = float(self.np_rng.normal(1.0, 0.2))
        self.current_activity_level = 1.0

        self.institutional_bias = 0.0
        self.event_shock_level = 0.0
        self.sweep_detected = False

    def update(self, tick=None, mid_price=None, snapshot=None, sweep=None):
        if tick is None:
            return

        tick_f = float(tick)

        # Если сервер уже вызвал update_clock(now_ts, mono_ts) — не ломаем это.
        # Фоллбек нужен только если ctx.update() вызвали отдельно без update_clock().
        if getattr(self, "now_ts", None) is None or abs(float(self.now_ts) - tick_f) > 1e-6:
            self.update_clock(tick_f)

        # UTC time-of-day строго из epoch seconds (секундная точность для фаз/сессий ок)
        tick_sod = int(tick_f) % (24 * 60 * 60)
        self.last_tick = tick_sod
        self.current_utc_time = tick_sod

        if mid_price is not None:
            self._update_price_memory(mid_price)

        if snapshot is not None:
            self._update_cluster_signals(snapshot)

        self._update_sessions()
        self._update_volatility()
        self._update_macro_micro_logic()
        self._update_activity_cycle(self.current_utc_time)
        self._update_phase()
        self._update_memory_trace()

    def _update_sessions(self):
        utc_hour = (self.current_utc_time // 3600) % 24
        if 0 <= utc_hour < 6:
            self.current_session = "asia"
        elif 6 <= utc_hour < 13:
            self.current_session = "london"
        else:
            self.current_session = "newyork"

        self.session_progress_ratio = (self.current_utc_time % 3600) / 3600.0
        self.rollover_flag = 22 <= utc_hour <= 23

        # лёгкий "drift" институционального bias, но через seeded RNG
        if self.current_session == "london" and self.session_type_profile["london"] == "trending":
            self.institutional_bias += float(self.np_rng.normal(0.001, 0.0005))
        elif self.current_session == "newyork" and self.session_type_profile["newyork"] == "reversal":
            self.institutional_bias -= float(self.np_rng.normal(0.001, 0.0005))

    def _update_price_memory(self, price):
        self.mid_prices_short.append(price)
        self.mid_prices_medium.append(price)
        self.mid_prices_long.append(price)

        if len(self.mid_prices_short) > 10:
            delta = self.mid_prices_short[-1] - self.mid_prices_short[0]
            self.micro_trends.append(delta)

    def _update_volatility(self):
        if len(self.mid_prices_short) >= 10:
            # PERF: np работает напрямую с deque — list() создавал лишнюю копию
            volatility = np.std(self.mid_prices_short) * self.volatility_amplitude * self.volatility_noise_factor
            self.volatility_levels.append(float(volatility))

            if volatility < 0.01:
                self.volatility_regime = "low"
            elif volatility < 0.05:
                self.volatility_regime = "normal"
            else:
                self.volatility_regime = "high"

    def _update_cluster_signals(self, snapshot):
        if not snapshot:
            return

        # PERF: было два прохода (сначала сырой list, потом levels_to_dict).
        # Теперь один проход через levels_to_dict с ранним выходом.
        def levels_to_dict(levels):
            # dict: {price: qty}
            if isinstance(levels, dict):
                out = {}
                for p, q in levels.items():
                    try:
                        pf = float(p); qf = float(q)
                    except Exception:
                        continue
                    out[pf] = out.get(pf, 0.0) + qf
                return out

            out = {}
            if not levels:
                return out

            if isinstance(levels, list):
                for lvl in levels:
                    p = q = None
                    if isinstance(lvl, dict):
                        p = lvl.get("price")
                        q = lvl.get("volume", lvl.get("qty", lvl.get("quantity", lvl.get("size"))))
                    elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                        p, q = lvl[0], lvl[1]

                    if p is None or q is None:
                        continue
                    try:
                        pf = float(p); qf = float(q)
                    except Exception:
                        continue
                    out[pf] = out.get(pf, 0.0) + qf
            return out

        bids = levels_to_dict(snapshot.get("bids"))
        asks = levels_to_dict(snapshot.get("asks"))

        if not bids or not asks:
            return

        best_bid = max(bids.keys())
        best_ask = min(asks.keys())

        bid_volume = float(bids.get(best_bid, 0.0))
        ask_volume = float(asks.get(best_ask, 0.0))
        denom = max(bid_volume + ask_volume, 1e-6)

        imbalance = (bid_volume - ask_volume) / denom
        self.cluster_imbalances.append(float(imbalance))
        self.cluster_disbalance = float(imbalance)

        if abs(imbalance) > 0.7:
            self.sweep_detected = True

    def _update_activity_cycle(self, tick):
        activity_cycle = math.sin((tick / 3600) * math.pi * 2 + self.daily_phase_shift)
        self.current_activity_level = float(np.clip(activity_cycle * self.volatility_amplitude, 0.2, 2.0))

    def _update_phase(self):
        # PERF: np.mean/np.std принимают любой iterable — list() был лишним
        trend = float(np.mean(self.micro_trends)) if self.micro_trends else 0.0
        vol = float(np.mean(self.volatility_levels)) if self.volatility_levels else 0.0

        transition_probs = {
            "flat": {"trend_up": 0.1, "trend_down": 0.1, "volatile": 0.2},
            "trend_up": {"volatile": 0.3, "flat": 0.1},
            "trend_down": {"volatile": 0.3, "flat": 0.1},
            "volatile": {"flat": 0.4, "trend_up": 0.2, "trend_down": 0.2},
        }

        current = self.phase.name if self.phase.name in transition_probs else "flat"

        if vol < 0.01:
            next_phase = "flat"
        elif abs(trend) > 0.05:
            next_phase = "trend_up" if trend > 0 else "trend_down"
        else:
            next_phase = "volatile"

        p = transition_probs.get(current, {}).get(next_phase, 0.2)
        if self.rng.random() < p:
            self.phase = MarketPhase(
                next_phase,
                min(1.0, abs(trend) * 20),
                {"volatility": vol, "trend": trend, "cluster_disbalance": self.cluster_disbalance},
            )

    def _update_macro_micro_logic(self):
        bias = 0.0

        if self.daily_macro_scenario == "fed_expectation":
            bias -= 0.002
            if self.session_progress_ratio > 0.8:
                self.event_shock_level += 0.01
        elif self.daily_macro_scenario == "ecb_dovish":
            bias += 0.002

        if self.session_type_profile[self.current_session] == "spiky":
            bias += self.rng.uniform(-0.0015, 0.0015)

        self.macro_bias_vector = bias + self.event_shock_level
        self.demand_supply_skew = (
            self.cluster_disbalance + self.institutional_bias + self.macro_bias_vector
        ) * self.current_activity_level

        if self.rollover_flag:
            self.demand_supply_skew *= 0.3

    def _update_memory_trace(self):
        # раз в 3 часа (по bucket), один раз, без спама
        bucket = self.current_utc_time // (60 * 60 * 3)
        if self._last_trace_bucket is None:
            self._last_trace_bucket = bucket
            return

        if bucket != self._last_trace_bucket:
            self._last_trace_bucket = bucket
            trace = {
                "volatility": float(np.mean(self.volatility_levels)) if self.volatility_levels else 0.0,
                "trend": float(np.mean(self.micro_trends)) if self.micro_trends else 0.0,
                "skew": float(np.mean(self.cluster_imbalances)) if self.cluster_imbalances else 0.0,
                "seed": self.run_seed,
                "scenario": self.daily_macro_scenario,
            }
            self.market_memory_trace.append(trace)

    def get_session(self):
        return self.current_session

    def get_volatility_boost(self):
        return {"low": 0.7, "normal": 1.0, "high": 1.4}.get(self.volatility_regime, 1.0)

    def get_macro_bias(self):
        return self.macro_bias_vector

    def get_demand_supply_skew(self):
        return self.demand_supply_skew

    def get_liquidity_state(self):
        if abs(self.cluster_disbalance) > 0.8:
            return "toxic"
        if self.volatility_regime == "low":
            return "stable"
        if self.volatility_regime == "high":
            return "shaky"
        return "normal"

    def is_fixing_time(self):
        minute = (self.current_utc_time // 60) % 60
        hour = (self.current_utc_time // 3600) % 24
        return (hour == 9 and 30 <= minute < 45) or (hour == 15 and 30 <= minute < 45)

    def get_pressure_map(self):
        return {"macro": self.macro_bias_vector, "institutional": self.institutional_bias, "cluster": self.cluster_disbalance}

    def is_ready(self) -> bool:
        return len(self.mid_prices_short) >= 10 and len(self.volatility_levels) >= 5