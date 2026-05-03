# macro_campaign_executor.py
#
# MacroCampaignExecutor — долгосрочное исполнение крупного клиентского parent-order (MARKET-only),
# рассчитанное на влияние на 5m/15m структуру через "кампанию" 30..180 минут.
#
# ВАЖНО: агент намеренно исполняет "кампанию" в двух режимах:
#   - CAUTIOUS: аккуратное исполнение (низкая частота/размер, больше пауз, уступает рынку)
#   - BURST:   "хуярит" волной (высокая частота/размер, допускает sweep глубже L1)
#
# Это даёт структуру: осторожен → хуярит → осторожен → хуярит (с откатами/рефиллом между волнами),
# но без тупого runaway: при чрезмерном импакте он ставит PAUSE и откатывается в CAUTIOUS.
#
# Совместимость:
#   - Order / OrderSide / OrderType / TICK из order.py
#   - OrderBook.get_order_book_snapshot(depth=1)
#   - OrderBook.trade_history (deque) с dict trade, где есть keys: price/volume/ts...
#
import time
import uuid
import random
from collections import deque
from typing import Dict, Any, List, Optional, Tuple

from backend.core.order import Order, OrderSide, OrderType, TICK


def _ctx_now(ctx) -> float:
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _safe_float(x, default: Optional[float] = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default) if default is not None else 0.0


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


def _trade_ts(t: Dict[str, Any]) -> float:
    for k in ("ts", "timestamp", "time", "t"):
        if k in t:
            v = t.get(k)
            try:
                v = float(v)
                if v > 1e12:  # похоже на ms
                    return v / 1000.0
                return v
            except Exception:
                pass
    return time.time()


def _trade_vol(t: Dict[str, Any]) -> float:
    for k in ("volume", "qty", "size"):
        if k in t:
            try:
                return float(t.get(k) or 0.0)
            except Exception:
                pass
    return 0.0


class MacroCampaignExecutor:
    supports_conn = False

    def __init__(
        self,
        agent_id: str,
        capital: float,
        *,
        # --- оценка режима (диагностика) ---
        regime_window_s: Tuple[float, float] = (900.0, 1800.0),  # 15..30 минут
        regime_min_samples: int = 120,
        regime_flat_range_ticks: int = 42,
        regime_trend_efficiency: float = 0.55,

        # --- приход parent-order ---
        base_arrival_lambda_per_min: float = 0.010,  # ~0.6/час
        cooldown_s: Tuple[float, float] = (600.0, 1800.0),  # 10..30 мин между кампаниями
        prefer_range_liquidity: float = 1.8,

        # --- размер и длительность кампании ---
        parent_qty_min: float = 700_000.0,
        parent_qty_max: float = 2_000_000.0,
        campaign_duration_s: Tuple[float, float] = (1800.0, 10800.0),  # 30..180 мин

        # --- волны (BURST on/off) ---
        wave_on_s: Tuple[float, float] = (120.0, 420.0),   # бурст 2..7 мин
        wave_off_s: Tuple[float, float] = (180.0, 900.0),  # осторожно 3..15 мин

        # --- базовые слайсы ---
        slice_frac_of_l1: Tuple[float, float] = (0.55, 1.35),  # базовый диапазон, фазы масштабируют
        min_slice_qty: float = 5_000.0,
        max_slice_qty: float = 140_000.0,

        child_min_dt_s: float = 1.8,
        child_max_dt_s: float = 10.5,

        # --- контроль импакта/отката ---
        impact_pause_ticks: int = 55,               # базовый порог для PAUSE (фазы масштабируют)
        retrace_ticks: Tuple[int, int] = (16, 44),  # базовый диапазон отката для RESUME
        pause_minmax_s: Tuple[float, float] = (45.0, 240.0),  # 0.75..4 мин (фазы масштабируют)
        max_wait_retrace_s: float = 900.0,          # 15 мин макс ждать откат
        refill_need_frac: float = 0.58,             # L1 должен восстановиться хотя бы до 58% baseline

        # --- participation cap ---
        part_window_s: float = 300.0,     # 5 минут
        part_cap_frac: float = 0.55,      # базовый cap, фазы масштабируют
        part_min_tape_vol: float = 10_000.0,  # если tape низкий, ослабляем (иначе застынем)
        part_floor_from_l1_frac: float = 1.25,  # пол: разрешить хотя бы ~L1*1.25 за окно, иначе не продавит

        # --- стоп-условия по микроусловиям ---
        max_spread_ticks: int = 8,
        min_l1_qty: float = 1_750.0,

        # --- урдженси к дедлайну ---
        urgency_ramp_last_frac: float = 0.14,     # последние 14% времени — ускоряемся
        urgency_part_cap_boost: float = 1.55,     # cap*1.55 под дедлайн
        urgency_pause_shrink: float = 0.55,       # паузы короче под дедлайн

        # --- шум/рандомизация ---
        side_bias: float = 0.0,  # -1..+1
        debug: bool = True,
    ):
        self.agent_id = agent_id
        self.capital = float(capital)
        self.loop_interval = 0.8

        self.regime_window_s = regime_window_s
        self.regime_min_samples = int(regime_min_samples)
        self.regime_flat_range_ticks = int(regime_flat_range_ticks)
        self.regime_trend_efficiency = float(regime_trend_efficiency)

        self.base_arrival_lambda_per_min = float(base_arrival_lambda_per_min)
        self.cooldown_s = cooldown_s
        self.prefer_range_liquidity = float(prefer_range_liquidity)

        self.parent_qty_min = float(parent_qty_min)
        self.parent_qty_max = float(parent_qty_max)
        self.campaign_duration_s = campaign_duration_s

        self.wave_on_s = wave_on_s
        self.wave_off_s = wave_off_s

        self.slice_frac_of_l1 = slice_frac_of_l1
        self.min_slice_qty = float(min_slice_qty)
        self.max_slice_qty = float(max_slice_qty)

        self.child_min_dt_s = float(child_min_dt_s)
        self.child_max_dt_s = float(child_max_dt_s)

        self.impact_pause_ticks = int(impact_pause_ticks)
        self.retrace_ticks = (int(retrace_ticks[0]), int(retrace_ticks[1]))
        self.pause_minmax_s = pause_minmax_s
        self.max_wait_retrace_s = float(max_wait_retrace_s)
        self.refill_need_frac = float(refill_need_frac)

        self.part_window_s = float(part_window_s)
        self.part_cap_frac = float(part_cap_frac)
        self.part_min_tape_vol = float(part_min_tape_vol)
        self.part_floor_from_l1_frac = float(part_floor_from_l1_frac)

        self.max_spread_ticks = int(max_spread_ticks)
        self.min_l1_qty = float(min_l1_qty)

        self.urgency_ramp_last_frac = float(urgency_ramp_last_frac)
        self.urgency_part_cap_boost = float(urgency_part_cap_boost)
        self.urgency_pause_shrink = float(urgency_pause_shrink)

        self.side_bias = float(side_bias)
        self.debug = bool(debug)

        # --- state ---
        self._mid_hist = deque(maxlen=8000)  # (ts, mid)

        self._cooldown_until = 0.0
        self._active = False
        self._side = OrderSide.BID
        self._dir = +1  # +1 buy, -1 sell

        self._parent_qty_total = 0.0
        self._parent_qty_left = 0.0
        self._start_ts = 0.0
        self._deadline_ts = 0.0

        # wave/phase
        self._wave_active = False
        self._wave_end_ts = 0.0
        self._next_wave_ts = 0.0
        self._phase = "CAUTIOUS"  # CAUTIOUS or BURST

        self._next_child_ts = 0.0
        self._last_child_submit_ts = 0.0

        # impact/pause controls
        self._paused = False
        self._pause_until = 0.0
        self._pause_started_ts = 0.0
        self._baseline_mid = None
        self._baseline_l1 = 0.0
        self._impact_wait_start = 0.0
        self._resume_retrace_ticks = 0

        # our execution history for participation accounting
        self._our_exec_hist = deque(maxlen=6000)  # (ts, qty)

        # stats
        self._exec_qty = 0.0
        self._exec_notional = 0.0

        self._last_progress_print = 0.0

        # --- phase multipliers (tuned for your market scale) ---
        # CAUTIOUS: мягкий поток, но НЕ ноль. BURST: доминирующий поток (инициатор фазы).
        self._phase_cfg = {
            "CAUTIOUS": {
                "dt_mult": 1.35,
                "desired_mult": (0.55, 0.95),
                "l1_mult": (0.45, 0.95),
                "part_mult": 0.65,
                "max_slice_mult": 0.55,
                "min_slice_mult": 0.90,
                "pause_ticks_mult": 0.95,
                "pause_time_mult": 1.25,
                "force_off_on_pause": False,
            },
            "BURST": {
                "dt_mult": 0.65,
                "desired_mult": (1.15, 1.95),
                "l1_mult": (0.95, 1.85),  # допускаем sweep глубже L1
                "part_mult": 1.35,
                "max_slice_mult": 1.10,
                "min_slice_mult": 1.10,
                "pause_ticks_mult": 1.20,  # в бурсте терпим больше импакта, но если пробили — отступаем
                "pause_time_mult": 0.65,
                "force_off_on_pause": True,  # важное: бурст -> осторожно после паузы
            },
        }

    # ---------- regime ----------
    def _update_mid_hist(self, now: float, mid: Optional[float]) -> None:
        if mid is None:
            return
        if self._mid_hist and now <= self._mid_hist[-1][0]:
            return
        self._mid_hist.append((now, float(mid)))

    def _calc_regime(self, now: float, tick: float) -> str:
        if len(self._mid_hist) < self.regime_min_samples:
            return "neutral"

        w = random.uniform(self.regime_window_s[0], self.regime_window_s[1])
        t0 = now - w

        mids = []
        for ts, m in reversed(self._mid_hist):
            if ts < t0:
                break
            mids.append(m)
        if len(mids) < self.regime_min_samples:
            return "neutral"

        mids.reverse()
        mn = min(mids)
        mx = max(mids)
        rng_ticks = (mx - mn) / max(tick, 1e-9)

        net = abs(mids[-1] - mids[0])
        tot = 0.0
        prev = mids[0]
        for m in mids[1:]:
            tot += abs(m - prev)
            prev = m
        er = (net / tot) if tot > 1e-12 else 0.0

        if rng_ticks <= self.regime_flat_range_ticks and er < self.regime_trend_efficiency:
            return "range"
        if er >= self.regime_trend_efficiency and rng_ticks > (0.60 * self.regime_flat_range_ticks):
            return "trend"
        return "neutral"

    # ---------- tape volume ----------
    def _recent_tape_vol(self, order_book, now: float) -> float:
        th = getattr(order_book, "trade_history", None)
        if not th:
            return 0.0
        t0 = now - self.part_window_s
        vol = 0.0
        for t in reversed(th):
            ts = _trade_ts(t)
            if ts < t0:
                break
            vol += _trade_vol(t)
        return max(0.0, vol)

    def _recent_our_exec(self, now: float) -> float:
        t0 = now - self.part_window_s
        v = 0.0
        for ts, qty in reversed(self._our_exec_hist):
            if ts < t0:
                break
            v += float(qty)
        return max(0.0, v)

    # ---------- campaign ----------
    def _maybe_start_campaign(self, order_book, ctx, now: float, tick: float) -> None:
        if self._active:
            return
        if now < self._cooldown_until:
            return

        snap = order_book.get_order_book_snapshot(depth=1)
        _, _, mid, _, _, _ = _best_from_snapshot(snap)
        if mid is None:
            return

        regime = self._calc_regime(now, tick)
        lam = self.base_arrival_lambda_per_min / 60.0  # per sec
        if regime == "range":
            lam *= self.prefer_range_liquidity

        dt = max(0.2, float(getattr(self, "loop_interval", 1.0)))
        p = 1.0 - pow(2.718281828, -lam * dt)
        if random.random() > p:
            return

        r = random.random() + 0.5 * _clip(self.side_bias, -1.0, 1.0)
        is_buy = (r >= 0.5)
        self._side = OrderSide.BID if is_buy else OrderSide.ASK
        self._dir = +1 if is_buy else -1

        qty = random.uniform(self.parent_qty_min, self.parent_qty_max)
        dur = random.uniform(self.campaign_duration_s[0], self.campaign_duration_s[1])

        self._active = True
        self._parent_qty_total = qty
        self._parent_qty_left = qty
        self._start_ts = now
        self._deadline_ts = now + dur

        # start with CAUTIOUS, then burst
        self._phase = "CAUTIOUS"
        self._wave_active = False
        self._next_wave_ts = now + random.uniform(10.0, 45.0)
        self._next_child_ts = now + random.uniform(0.8, 2.5)
        self._last_child_submit_ts = 0.0

        self._paused = False
        self._pause_until = 0.0
        self._baseline_mid = float(mid)
        self._baseline_l1 = 0.0
        self._impact_wait_start = 0.0
        self._resume_retrace_ticks = 0

        self._exec_qty = 0.0
        self._exec_notional = 0.0
        self._last_progress_print = now

        self._cooldown_until = now + random.uniform(self.cooldown_s[0], self.cooldown_s[1])

        if self.debug:
            side_s = "BUY" if is_buy else "SELL"
            print(f"[{self.agent_id}] NEW CAMPAIGN {side_s} qty={qty:,.0f} dur={dur/60.0:.1f}m regime={regime}")

    def _end_campaign(self, reason: str) -> None:
        if self.debug:
            done = self._parent_qty_total - self._parent_qty_left
            avg_px = (self._exec_notional / self._exec_qty) if self._exec_qty > 0 else None
            print(f"[{self.agent_id}] END CAMPAIGN reason={reason} done={done:,.0f}/{self._parent_qty_total:,.0f} avg_px={avg_px}")
        self._active = False
        self._wave_active = False
        self._paused = False

    def _urgency(self, now: float) -> float:
        total = max(1.0, self._deadline_ts - self._start_ts)
        left = max(0.0, self._deadline_ts - now)
        frac_left = left / total
        if frac_left <= self.urgency_ramp_last_frac:
            k = (self.urgency_ramp_last_frac - frac_left) / max(1e-9, self.urgency_ramp_last_frac)
            return 1.0 + 1.2 * _clip(k, 0.0, 1.0)
        return 1.0

    # ---------- phase helpers ----------
    def _phase_cfg_val(self, phase: str, key: str, default):
        cfg = self._phase_cfg.get(phase)
        if not cfg:
            return default
        return cfg.get(key, default)

    def _phase_dt_mult(self, phase: str) -> float:
        return float(self._phase_cfg_val(phase, "dt_mult", 1.0))

    def _phase_desired_mult(self, phase: str) -> Tuple[float, float]:
        return tuple(self._phase_cfg_val(phase, "desired_mult", (1.0, 1.0)))

    def _phase_l1_mult(self, phase: str) -> Tuple[float, float]:
        return tuple(self._phase_cfg_val(phase, "l1_mult", (1.0, 1.0)))

    def _phase_part_mult(self, phase: str) -> float:
        return float(self._phase_cfg_val(phase, "part_mult", 1.0))

    def _phase_max_slice(self, phase: str) -> float:
        mult = float(self._phase_cfg_val(phase, "max_slice_mult", 1.0))
        return max(1.0, self.max_slice_qty * mult)

    def _phase_min_slice(self, phase: str) -> float:
        mult = float(self._phase_cfg_val(phase, "min_slice_mult", 1.0))
        return max(1.0, self.min_slice_qty * mult)

    def _phase_pause_ticks(self, phase: str, base: float) -> float:
        mult = float(self._phase_cfg_val(phase, "pause_ticks_mult", 1.0))
        return base * mult

    def _phase_pause_time_mult(self, phase: str) -> float:
        return float(self._phase_cfg_val(phase, "pause_time_mult", 1.0))

    def _phase_force_off_on_pause(self, phase: str) -> bool:
        return bool(self._phase_cfg_val(phase, "force_off_on_pause", False))

    # ---------- waves/phase transitions ----------
    def _maybe_start_or_stop_wave(self, now: float, urgency: float) -> None:
        # Мы используем wave как переключатель фаз:
        #   wave_active=True  => BURST
        #   wave_active=False => CAUTIOUS (но торгуем мягко!)
        if not self._wave_active:
            if now < self._next_wave_ts:
                self._phase = "CAUTIOUS"
                return

            on = random.uniform(self.wave_on_s[0], self.wave_on_s[1])
            if urgency > 1.0:
                on *= (1.0 + 0.15 * (urgency - 1.0))

            self._wave_active = True
            self._wave_end_ts = now + on
            self._phase = "BURST"
            if self.debug:
                print(f"[{self.agent_id}] phase=BURST ON for {on:.1f}s left={self._parent_qty_left:,.0f}")
            return

        # wave active -> stop
        if now >= self._wave_end_ts:
            self._wave_active = False
            off = random.uniform(self.wave_off_s[0], self.wave_off_s[1])
            if urgency > 1.0:
                off *= self.urgency_pause_shrink

            self._next_wave_ts = now + off
            self._phase = "CAUTIOUS"
            if self.debug:
                print(f"[{self.agent_id}] phase=CAUTIOUS for {off:.1f}s")
            return

        self._phase = "BURST"

    # ---------- participation / caps ----------
    def _allowed_by_participation(self, tape_vol: float, our_vol: float, urgency: float, phase: str, l1_qty: float) -> float:
        # базовый cap
        cap = self.part_cap_frac * self._phase_part_mult(phase)
        if urgency > 1.0:
            cap *= self.urgency_part_cap_boost

        cap = _clip(cap, 0.05, 0.92)

        # если tape слишком маленький — иначе застынем
        if tape_vol < self.part_min_tape_vol:
            cap = max(cap, 0.62)

        # сколько нам "разрешено" в окне от внешнего tape
        allowed = tape_vol * cap

        # floor: минимум в окне должен позволять хотя бы ~L1*floor
        allowed_floor = max(0.0, float(l1_qty) * self.part_floor_from_l1_frac)

        # вычитаем то, что мы уже сделали в этом окне
        left_in_window = max(0.0, max(allowed, allowed_floor) - our_vol)

        return left_in_window

    # ---------- impact / pause ----------
    def _impact_check_and_pause(self, now: float, mid: float, tick: float, l1_qty: float, urgency: float, phase: str) -> None:
        if self._baseline_mid is None:
            return

        # direction-normalized move from baseline: BUY => + when mid up, SELL => + when mid down
        delta_ticks = ((mid - float(self._baseline_mid)) / max(tick, 1e-9)) * float(self._dir)

        base_pause_ticks = float(self.impact_pause_ticks)
        pause_ticks = self._phase_pause_ticks(phase, base_pause_ticks)

        if urgency > 1.0:
            pause_ticks *= (1.0 + 0.30 * max(0.0, urgency - 1.0))

        if (not self._paused) and (delta_ticks >= pause_ticks):
            self._paused = True

            base_pause = random.uniform(self.pause_minmax_s[0], self.pause_minmax_s[1])
            base_pause *= self._phase_pause_time_mult(phase)

            if urgency > 1.0:
                base_pause *= self.urgency_pause_shrink

            self._pause_started_ts = now
            self._pause_until = now + base_pause
            self._impact_wait_start = now

            self._baseline_l1 = max(self._baseline_l1, float(l1_qty))
            self._resume_retrace_ticks = random.randint(self.retrace_ticks[0], self.retrace_ticks[1])

            # ВАЖНО: если мы в BURST и получили PAUSE — принудительно сходим в CAUTIOUS после паузы.
            if self._phase_force_off_on_pause(phase):
                self._wave_active = False
                # немного отодвинем следующий бурст, чтобы дать рынку восстановиться
                extra = random.uniform(30.0, 120.0)
                self._next_wave_ts = max(self._next_wave_ts, now + base_pause + extra)
                self._phase = "CAUTIOUS"

            if self.debug:
                print(
                    f"[{self.agent_id}] PAUSE phase={phase} impact={delta_ticks:.1f}t >= {pause_ticks:.1f}t "
                    f"wait={base_pause:.1f}s retrace_need={self._resume_retrace_ticks}t"
                )

    def _can_resume_after_pause(self, now: float, mid: float, tick: float, l1_qty: float) -> bool:
        if not self._paused:
            return True
        if now < self._pause_until:
            return False

        if self._baseline_mid is None:
            self._paused = False
            return True

        delta_ticks = ((mid - float(self._baseline_mid)) / max(tick, 1e-9)) * float(self._dir)

        # resume when price comes "back" (delta smaller than retrace threshold) and L1 refilled enough
        if delta_ticks <= float(self._resume_retrace_ticks):
            if self._baseline_l1 <= 1e-9:
                self._paused = False
                self._baseline_mid = float(mid)  # re-anchor
                return True
            if float(l1_qty) >= self.refill_need_frac * float(self._baseline_l1):
                self._paused = False
                self._baseline_mid = float(mid)  # re-anchor
                if self.debug:
                    print(
                        f"[{self.agent_id}] RESUME delta={delta_ticks:.1f}t "
                        f"l1={float(l1_qty):.0f} baseline_l1={float(self._baseline_l1):.0f}"
                    )
                return True

        # timeout: resume by refill even without retrace
        if (now - self._impact_wait_start) >= self.max_wait_retrace_s:
            if self._baseline_l1 <= 1e-9 or float(l1_qty) >= self.refill_need_frac * float(self._baseline_l1):
                self._paused = False
                self._baseline_mid = float(mid)  # re-anchor
                if self.debug:
                    print(f"[{self.agent_id}] RESUME by timeout/refill")
                return True

        return False

    # ---------- public API ----------
    def generate_orders(self, order_book, market_context=None, conn=None) -> List[Order]:
        ctx = market_context if market_context is not None else getattr(order_book, "market_context", None)
        now = _ctx_now(ctx)

        tick = float(getattr(order_book, "tick_size", None) or TICK or 0.01)
        tick = max(tick, 1e-6)

        snap = order_book.get_order_book_snapshot(depth=1)
        best_bid, best_ask, mid, spread, bid_l1, ask_l1 = _best_from_snapshot(snap)
        self._update_mid_hist(now, mid)

        # start campaign probabilistically
        self._maybe_start_campaign(order_book, ctx, now, tick)

        if not self._active:
            return []

        # termination checks
        if self._parent_qty_left <= 0.0:
            self._end_campaign("filled")
            return []
        if now >= self._deadline_ts:
            self._end_campaign("deadline")
            return []

        if mid is None or best_bid is None or best_ask is None:
            return []

        spread_ticks = (spread / tick) if tick > 0 else 0.0
        if spread_ticks > float(self.max_spread_ticks):
            return []

        urgency = self._urgency(now)

        # phase management
        self._maybe_start_or_stop_wave(now, urgency)
        phase = self._phase

        # choose L1 for our taker side
        l1_qty = float(ask_l1) if self._side == OrderSide.BID else float(bid_l1)
        if l1_qty < float(self.min_l1_qty):
            return []

        # pause trigger/resume
        self._impact_check_and_pause(now, float(mid), tick, l1_qty, urgency, phase)
        if not self._can_resume_after_pause(now, float(mid), tick, l1_qty):
            return []

        # schedule child submissions (phase-aware)
        if now < float(self._next_child_ts):
            return []

        dt = random.uniform(self.child_min_dt_s, self.child_max_dt_s)
        dt *= self._phase_dt_mult(phase)

        if urgency > 1.0:
            dt *= (1.0 / (0.85 + 0.35 * (urgency - 1.0)))
            dt = _clip(dt, 0.65, self.child_max_dt_s)

        self._next_child_ts = now + dt

        # target rate: remaining / remaining_time
        rem_t = max(1.0, self._deadline_ts - now)
        target_rate = self._parent_qty_left / rem_t  # qty per sec

        # desired qty for dt
        desired = target_rate * dt
        noise_lo, noise_hi = self._phase_desired_mult(phase)
        desired *= random.uniform(noise_lo, noise_hi)

        # cap by l1 fraction (phase-aware, can sweep > 1.0)
        l1_lo, l1_hi = self._phase_l1_mult(phase)
        frac_lo = self.slice_frac_of_l1[0] * l1_lo
        frac_hi = self.slice_frac_of_l1[1] * l1_hi
        l1_frac = random.uniform(frac_lo, frac_hi)
        by_l1 = l1_qty * max(0.05, l1_frac)

        # participation cap (tape- & our- volume aware)
        tape_vol = float(self._recent_tape_vol(order_book, now))
        our_vol = float(self._recent_our_exec(now))
        by_part = float(self._allowed_by_participation(tape_vol, our_vol, urgency, phase, l1_qty))

        # capital-based cap (safety) — still allow big slices but not infinite
        # keep it loose: we cap by 0.9% capital in BURST, 0.45% in CAUTIOUS
        cap_frac = 0.0090 if phase == "BURST" else 0.0045
        max_notional = self.capital * cap_frac
        by_cap = max_notional / max(float(mid), 1e-9)

        # phase-specific max/min
        max_slice = self._phase_max_slice(phase)
        min_slice = self._phase_min_slice(phase)

        qty = min(desired, by_l1, by_part, by_cap, max_slice)
        qty = max(0.0, qty)

        if qty < min_slice:
            return []

        qty = min(qty, self._parent_qty_left)

        # re-anchor baseline right before our hit (so impact checks measure this micro-push)
        if not self._paused:
            self._baseline_mid = float(mid)
            self._baseline_l1 = max(self._baseline_l1, float(l1_qty))

        o = Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=self._side,
            volume=float(qty),
            price=None,
            order_type=OrderType.MARKET,
            ttl=None,
        )

        # progress print (раз в ~60 секунд)
        if self.debug and (now - self._last_progress_print) >= 60.0:
            done = self._parent_qty_total - self._parent_qty_left
            frac = 100.0 * (done / max(1.0, self._parent_qty_total))
            print(f"[{self.agent_id}] progress done={done:,.0f} ({frac:.1f}%) phase={phase} tape5m={tape_vol:,.0f} our5m={our_vol:,.0f}")
            self._last_progress_print = now

        self._last_child_submit_ts = now
        return [o]

    def on_order_filled(self, order_id: str, price: float, volume: float, side: OrderSide):
        v = float(volume)
        p = float(price)
        if v <= 0.0:
            return

        # участие в tape считаем всегда, даже если кампания уже закончилась
        now = time.time()
        self._our_exec_hist.append((now, v))

        if not self._active:
            return

        self._parent_qty_left = max(0.0, self._parent_qty_left - v)
        self._exec_qty += v
        self._exec_notional += v * p

        if self._parent_qty_left <= 0.0:
            self._end_campaign("filled")
