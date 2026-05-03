import time
import math
import random
import uuid
from collections import deque
from enum import Enum
from typing import List, Optional, Tuple

from backend.core.order import Order, OrderSide, OrderType, TICK


def _ctx_now(ctx) -> float:
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _safe(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _linreg_slope_r2(pts: list) -> tuple:
    """
    Линейная регрессия по списку (ts, price).
    Возвращает (slope_ticks_per_sec, R²).
    R² = насколько движение линейно (1 = идеальный тренд).
    """
    n = len(pts)
    if n < 6:
        return 0.0, 0.0
    xs = [p[0] - pts[0][0] for p in pts]
    ys = [p[1] for p in pts]
    sx = sum(xs); sy = sum(ys)
    sxx = sum(x*x for x in xs)
    sxy = sum(x*y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return 0.0, 0.0
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    ss_res = sum((y - (slope*x + intercept))**2 for x,y in zip(xs, ys))
    ss_tot = sum((y - sy/n)**2 for y in ys)
    r2 = _clip(1.0 - ss_res / max(ss_tot, 1e-18), 0.0, 1.0)
    return abs(slope) / max(TICK, 1e-12), r2


class MandateType(Enum):
    REBALANCE   = "rebalance"    # СВФ квартальный ребаланс — направление случайное
    HEDGE_CLOSE = "hedge_close"  # корпоратив закрывает хедж — против тренда
    LEVEL_DEF   = "level_def"    # ЦБ-стиль защита уровня — против тренда
    MOMENTUM    = "momentum"     # поздний CTA — по тренду, разгоняет перед сломом


class Phase(Enum):
    DORMANT    = "dormant"
    WATCHING   = "watching"
    PREPARING  = "preparing"
    STRIKING   = "striking"
    SUSTAINING = "sustaining"
    UNWINDING  = "unwinding"
    COOLDOWN   = "cooldown"


class RegimeShockDisruptor:
    """
    Mandate-driven Disruptor — агент режимного слома.

    ═══════════════════════════════════════════════════════════
    КОНЦЕПТ
    ═══════════════════════════════════════════════════════════
    На форекс всегда есть участники с МАНДАТОМ, не со спекуляцией:

    • REBALANCE (СВФ/пенсфонд): плановый ребаланс по расписанию.
      Направление из внутренней allocation model — не из рынка.
      Может совпасть с трендом или идти против — не важно.

    • HEDGE_CLOSE (корпоратив): закрытие форвардного хеджа.
      Рынок рос → хедж был шорт → закрытие = покупка (против тренда).
      Рынок падал → хедж был лонг → закрытие = продажа (против тренда).

    • LEVEL_DEF (ЦБ-стиль): защита уровня поддержки/сопротивления.
      Бьёт против движения когда цена "пробивает" таргет.

    • MOMENTUM (поздний CTA): входит В НАПРАВЛЕНИИ тренда после паузы.
      Создаёт финальный импульс — перед разворотом.

    Общее у всех: их объём НЕСОРАЗМЕРЕН текущей ликвидности.
    Это дестабилизирует адаптивных агентов → смена режима → волнообразность.

    Ключевые отличия от трендового агента:
    1. НЕ входит сразу при появлении тренда — ждёт пока агенты адаптируются
    2. Направление НЕ из momentum сигнала, а из типа мандата
    3. Удар делается быстро (2-5 волн подряд) — шок, а не постепенный набор
    4. После удара — фаза давления чтобы "сломать" адаптацию других
    ═══════════════════════════════════════════════════════════
    """

    def __init__(
            self,
            agent_id: str,
            capital: float,
            regime_stable_min_s: float = 80.0,
            regime_stable_max_s: float = 220.0,
            regime_window_s: float = 120.0,
            regime_slope_min: float = 0.08,
            regime_r2_min: float = 0.50,
            mandate_weights: Optional[dict] = None,
            prepare_min_s: float = 3.0,
            prepare_max_s: float = 35.0,
            thin_book_threshold: float = 0.70,
            strike_frac_min: float = 0.15,
            strike_frac_max: float = 0.42,
            strike_waves_min: int = 2,
            strike_waves_max: int = 5,
            strike_wave_interval_min_s: float = 0.3,
            strike_wave_interval_max_s: float = 2.5,
            sustain_min_s: float = 25.0,
            sustain_max_s: float = 100.0,
            sustain_vol_frac: float = 0.35,
            sustain_interval_min_s: float = 3.0,
            sustain_interval_max_s: float = 18.0,
            unwind_min_s: float = 20.0,
            unwind_max_s: float = 90.0,
            stop_loss_frac: float = 0.04,
            max_position_frac: float = 0.50,
            cooldown_min_s: float = 120.0,
            cooldown_max_s: float = 400.0,
    ):
        self.agent_id = str(agent_id)
        self.capital = float(capital)

        self.regime_stable_min_s = float(regime_stable_min_s)
        self.regime_stable_max_s = float(regime_stable_max_s)
        self.regime_window_s = float(regime_window_s)
        self.regime_slope_min = float(regime_slope_min)
        self.regime_r2_min = float(regime_r2_min)

        self.mandate_weights = mandate_weights or {
            MandateType.REBALANCE:   0.30,
            MandateType.HEDGE_CLOSE: 0.30,
            MandateType.LEVEL_DEF:   0.20,
            MandateType.MOMENTUM:    0.20,
        }

        self.prepare_min_s = float(prepare_min_s)
        self.prepare_max_s = float(prepare_max_s)
        self.thin_book_threshold = float(thin_book_threshold)
        self.strike_frac_min = float(strike_frac_min)
        self.strike_frac_max = float(strike_frac_max)
        self.strike_waves_min = int(strike_waves_min)
        self.strike_waves_max = int(strike_waves_max)
        self.strike_wave_interval_min_s = float(strike_wave_interval_min_s)
        self.strike_wave_interval_max_s = float(strike_wave_interval_max_s)
        self.sustain_min_s = float(sustain_min_s)
        self.sustain_max_s = float(sustain_max_s)
        self.sustain_vol_frac = float(sustain_vol_frac)
        self.sustain_interval_min_s = float(sustain_interval_min_s)
        self.sustain_interval_max_s = float(sustain_interval_max_s)
        self.unwind_min_s = float(unwind_min_s)
        self.unwind_max_s = float(unwind_max_s)
        self.stop_loss_frac = float(stop_loss_frac)
        self.max_position_frac = float(max_position_frac)
        self.cooldown_min_s = float(cooldown_min_s)
        self.cooldown_max_s = float(cooldown_max_s)

        # состояние
        self.phase = Phase.DORMANT
        self._phase_start_ts: float = 0.0
        self._phase_end_ts: float = 0.0

        self.mandate_type: Optional[MandateType] = None
        self.mandate_dir: int = 0
        self.mandate_level: Optional[float] = None

        self._regime_detected_ts: float = 0.0
        self._regime_required_s: float = 0.0
        self._regime_slope: float = 0.0
        self._regime_r2: float = 0.0
        self._regime_dir: int = 0

        self.position_qty: float = 0.0
        self.avg_entry: Optional[float] = None
        self.realized_pnl: float = 0.0

        self._strike_total_qty: float = 0.0
        self._strike_waves_total: int = 0
        self._strike_waves_done: int = 0
        self._strike_qty_per_wave: float = 0.0
        self._next_strike_ts: float = 0.0

        self._sustain_total_qty: float = 0.0
        self._sustain_done_qty: float = 0.0
        self._next_sustain_ts: float = 0.0

        self._next_unwind_ts: float = 0.0
        self._dormant_until: float = 0.0

        self._mid_hist: deque = deque(maxlen=3000)
        self._liq_hist: deque = deque(maxlen=200)
        self.phase_log: deque = deque(maxlen=80)

    # ── стакан ────────────────────────────────────────────────

    def _book_top(self, order_book):
        try:
            snap = order_book.get_order_book_snapshot(depth=3)
            bids = snap.get("bids") or []
            asks = snap.get("asks") or []
        except Exception:
            return None, None, 0.0, TICK, 0.0
        best_bid = _safe(bids[0]["price"]) if bids else None
        best_ask = _safe(asks[0]["price"]) if asks else None
        if best_bid is None and best_ask is None:
            return None, None, 0.0, TICK, 0.0
        if best_bid is not None and best_ask is not None:
            mid = 0.5 * (best_bid + best_ask)
            spread = max(best_ask - best_bid, TICK)
        elif best_bid is not None:
            mid, spread = best_bid, TICK
        else:
            mid, spread = best_ask, TICK
        top_liq = (_safe(bids[0].get("volume", 0.0)) if bids else 0.0) + \
                  (_safe(asks[0].get("volume", 0.0)) if asks else 0.0)
        return best_bid, best_ask, mid, spread, top_liq

    def _update_hist(self, now: float, mid: float, top_liq: float) -> None:
        if not self._mid_hist or now - self._mid_hist[-1][0] >= 0.05:
            self._mid_hist.append((now, mid))
        if top_liq > 0:
            self._liq_hist.append(top_liq)

    # ── детектор режима ───────────────────────────────────────

    def _assess_regime(self, now: float):
        cutoff = now - self.regime_window_s
        pts = [(ts, p) for ts, p in self._mid_hist if ts >= cutoff]
        if len(pts) < 10:
            return 0.0, 0.0, 0
        slope_ticks, r2 = _linreg_slope_r2(pts)
        direction = 1 if pts[-1][1] > pts[0][1] else -1
        return slope_ticks, r2, direction

    def _book_is_thin(self, top_liq: float) -> bool:
        if len(self._liq_hist) < 20:
            return False
        avg_liq = sum(self._liq_hist) / len(self._liq_hist)
        return top_liq < avg_liq * self.thin_book_threshold

    # ── выбор мандата ─────────────────────────────────────────

    def _pick_mandate(self, now: float, mid: float):
        types = list(self.mandate_weights.keys())
        weights = [self.mandate_weights[t] for t in types]
        r = random.uniform(0, sum(weights))
        cumulative = 0.0
        mandate = types[0]
        for t, w in zip(types, weights):
            cumulative += w
            if r <= cumulative:
                mandate = t
                break

        level = None
        if mandate == MandateType.REBALANCE:
            # СВФ: мандат не знает куда идёт рынок — случайное направление
            direction = random.choice([-1, 1])

        elif mandate == MandateType.HEDGE_CLOSE:
            # Закрытие хеджа — против текущего тренда
            direction = -self._regime_dir if self._regime_dir != 0 else random.choice([-1, 1])

        elif mandate == MandateType.LEVEL_DEF:
            # ЦБ: возврат к уровню — против текущего движения
            direction = -self._regime_dir if self._regime_dir != 0 else random.choice([-1, 1])
            level = round(mid * (1.0 + random.uniform(-0.003, 0.003)), 5)

        elif mandate == MandateType.MOMENTUM:
            # Поздний CTA: входит по тренду — разгоняет перед сломом
            direction = self._regime_dir if self._regime_dir != 0 else random.choice([-1, 1])

        else:
            direction = random.choice([-1, 1])

        return mandate, direction, level

    # ── переход фаз ───────────────────────────────────────────

    def _transition(self, new_phase: Phase, now: float, mid: float = 0.0) -> None:
        self.phase_log.append({
            "from": self.phase.value, "to": new_phase.value,
            "ts": now, "mid": round(mid, 5),
            "mandate": self.mandate_type.value if self.mandate_type else None,
            "dir": self.mandate_dir,
        })
        self.phase = new_phase
        self._phase_start_ts = now

        if new_phase == Phase.WATCHING:
            self._regime_detected_ts = 0.0
            self._regime_required_s = random.uniform(self.regime_stable_min_s, self.regime_stable_max_s)

        elif new_phase == Phase.PREPARING:
            self._phase_end_ts = now + random.uniform(self.prepare_min_s, self.prepare_max_s)

        elif new_phase == Phase.STRIKING:
            frac = random.uniform(self.strike_frac_min, self.strike_frac_max)
            total_notional = self.capital * frac
            self._strike_total_qty = max(1.0, total_notional / max(mid, 1e-9))
            self._strike_waves_total = random.randint(self.strike_waves_min, self.strike_waves_max)
            self._strike_waves_done = 0
            self._strike_qty_per_wave = self._strike_total_qty / self._strike_waves_total
            self._next_strike_ts = now

        elif new_phase == Phase.SUSTAINING:
            self._phase_end_ts = now + random.uniform(self.sustain_min_s, self.sustain_max_s)
            self._sustain_total_qty = self._strike_total_qty * self.sustain_vol_frac
            self._sustain_done_qty = 0.0
            self._next_sustain_ts = now + random.uniform(self.sustain_interval_min_s, self.sustain_interval_max_s)

        elif new_phase == Phase.UNWINDING:
            self._phase_end_ts = now + random.uniform(self.unwind_min_s, self.unwind_max_s)
            self._next_unwind_ts = now + random.uniform(1.0, 4.0)

        elif new_phase == Phase.COOLDOWN:
            self._dormant_until = now + random.uniform(self.cooldown_min_s, self.cooldown_max_s)
            self.mandate_type = None
            self.mandate_dir = 0
            self.mandate_level = None
            self._regime_detected_ts = 0.0
            self._regime_dir = 0

    # ── PnL ──────────────────────────────────────────────────

    def _apply_fill(self, side: OrderSide, price: float, qty: float) -> None:
        if qty <= 0.0:
            return
        if side == OrderSide.BID:
            new_pos = self.position_qty + qty
            if self.avg_entry is None or self.position_qty == 0:
                self.avg_entry = price
            elif self.position_qty > 0:
                self.avg_entry = (self.avg_entry * self.position_qty + price * qty) / max(new_pos, 1e-12)
            else:
                close = min(qty, -self.position_qty)
                if self.avg_entry is not None:
                    self.realized_pnl += (self.avg_entry - price) * close
                self.avg_entry = price if (qty - close) > 1e-9 else (None if new_pos == 0 else self.avg_entry)
            self.position_qty = new_pos
        else:
            new_pos = self.position_qty - qty
            if self.avg_entry is None or self.position_qty == 0:
                self.avg_entry = price
            elif self.position_qty < 0:
                self.avg_entry = (self.avg_entry * (-self.position_qty) + price * qty) / max(-new_pos, 1e-12)
            else:
                close = min(qty, self.position_qty)
                if self.avg_entry is not None:
                    self.realized_pnl += (price - self.avg_entry) * close
                self.avg_entry = price if (qty - close) > 1e-9 else (None if new_pos == 0 else self.avg_entry)
            self.position_qty = new_pos

    def _stop_triggered(self, mid: float) -> bool:
        if self.avg_entry is None or self.position_qty == 0:
            return False
        unrealized = (mid - self.avg_entry) * self.position_qty
        return unrealized < -self.capital * self.stop_loss_frac

    # ── фазы ──────────────────────────────────────────────────

    def _orders_dormant(self, now: float) -> List[Order]:
        if now < self._dormant_until:
            return []
        if random.random() < 0.0003:
            self._transition(Phase.WATCHING, now)
        return []

    def _orders_watching(self, now: float, mid: float) -> List[Order]:
        """
        Ждём устойчивого режима.
        Высокий R² = все агенты адаптировались и движутся синхронно.
        Именно тогда один крупный нестандартный поток даёт максимальный эффект.
        """
        slope, r2, direction = self._assess_regime(now)
        self._regime_slope = slope
        self._regime_r2 = r2
        self._regime_dir = direction

        regime_ok = slope >= self.regime_slope_min and r2 >= self.regime_r2_min and direction != 0

        if regime_ok:
            if self._regime_detected_ts == 0.0:
                self._regime_detected_ts = now
            elif now - self._regime_detected_ts >= self._regime_required_s:
                mandate, direction, level = self._pick_mandate(now, mid)
                self.mandate_type = mandate
                self.mandate_dir = direction
                self.mandate_level = level
                self._transition(Phase.PREPARING, now, mid)
        else:
            self._regime_detected_ts = 0.0

        if now - self._phase_start_ts > self.regime_stable_max_s * 2.5:
            self._transition(Phase.COOLDOWN, now, mid)
        return []

    def _orders_preparing(self, now: float, mid: float, top_liq: float) -> List[Order]:
        """
        Мандат активирован, ищем "окно":
        - тонкий стакан (максимальный ценовой эффект нашего объёма)
        - или дедлайн (мандат есть мандат, нельзя ждать вечно)
        """
        if now >= self._phase_end_ts or self._book_is_thin(top_liq):
            self._transition(Phase.STRIKING, now, mid)
        return []

    def _orders_striking(self, now: float, mid: float,
                         best_bid, best_ask, top_liq) -> List[Order]:
        """
        ОСНОВНОЙ УДАР — быстрые волны подряд.
        Рынок не успевает "переварить" первую волну до прихода второй.
        Именно это создаёт большие свечи с резким смещением цены.
        """
        orders = []
        if self._stop_triggered(mid):
            self._transition(Phase.UNWINDING, now, mid)
            return orders
        if self._strike_waves_done >= self._strike_waves_total:
            self._transition(Phase.SUSTAINING, now, mid)
            return orders
        if now < self._next_strike_ts:
            return orders

        is_last = self._strike_waves_done == self._strike_waves_total - 1
        if is_last:
            wave_qty = max(1.0, self._strike_total_qty - self._strike_qty_per_wave * self._strike_waves_done)
        else:
            wave_qty = max(1.0, self._strike_qty_per_wave * random.uniform(0.70, 1.30))

        if top_liq > 0:
            wave_qty = min(wave_qty, top_liq * random.uniform(0.40, 0.65))
            wave_qty = max(1.0, wave_qty)

        side = OrderSide.BID if self.mandate_dir > 0 else OrderSide.ASK
        orders.append(Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            volume=float(wave_qty),
            price=None,
            order_type=OrderType.MARKET,
            ttl=None,
            metadata={
                "role": "regime_disruptor",
                "phase": "striking",
                "mandate": self.mandate_type.value if self.mandate_type else "unknown",
                "wave": f"{self._strike_waves_done + 1}/{self._strike_waves_total}",
                "regime_r2": round(self._regime_r2, 3),
            },
        ))
        self._strike_waves_done += 1
        self._next_strike_ts = now + random.uniform(
            self.strike_wave_interval_min_s, self.strike_wave_interval_max_s
        )
        return orders

    def _orders_sustaining(self, now: float, mid: float, best_bid, best_ask) -> List[Order]:
        """
        ДАВЛЕНИЕ — небольшие регулярные ордера чтобы адаптивные агенты
        не успели "перекалиброваться" обратно на старый режим.
        Иногда — limit-ордера как видимая "стена" в стакане.
        """
        orders = []
        if self._stop_triggered(mid):
            self._transition(Phase.UNWINDING, now, mid)
            return orders
        pos_notional = abs(self.position_qty) * mid
        if pos_notional > self.capital * self.max_position_frac:
            self._transition(Phase.UNWINDING, now, mid)
            return orders
        if now >= self._phase_end_ts or self._sustain_done_qty >= self._sustain_total_qty:
            self._transition(Phase.UNWINDING, now, mid)
            return orders
        if now < self._next_sustain_ts:
            return orders

        remaining = self._sustain_total_qty - self._sustain_done_qty
        qty = max(1.0, remaining * random.uniform(0.10, 0.30))
        side = OrderSide.BID if self.mandate_dir > 0 else OrderSide.ASK

        use_limit = random.random() < 0.30
        if use_limit and best_bid is not None and best_ask is not None:
            px = (best_bid + TICK * random.randint(0, 1)) if side == OrderSide.BID else \
                 (best_ask - TICK * random.randint(0, 1))
            orders.append(Order(
                order_id=str(uuid.uuid4()), agent_id=self.agent_id,
                side=side, volume=float(qty), price=float(round(px, 5)),
                order_type=OrderType.LIMIT, ttl=random.randint(4, 12),
                metadata={"role": "regime_disruptor", "phase": "sustaining", "type": "wall"},
            ))
        else:
            orders.append(Order(
                order_id=str(uuid.uuid4()), agent_id=self.agent_id,
                side=side, volume=float(qty), price=None,
                order_type=OrderType.MARKET, ttl=None,
                metadata={"role": "regime_disruptor", "phase": "sustaining", "type": "pressure"},
            ))

        self._sustain_done_qty += qty
        self._next_sustain_ts = now + random.uniform(self.sustain_interval_min_s, self.sustain_interval_max_s)
        return orders

    def _orders_unwinding(self, now: float, mid: float, best_bid, best_ask, top_liq) -> List[Order]:
        """TWAP-стиль выход. Стоп — единственное исключение."""
        orders = []
        remaining_qty = abs(self.position_qty)
        if remaining_qty < 1.0:
            self._transition(Phase.COOLDOWN, now, mid)
            return orders

        if self._stop_triggered(mid):
            side = OrderSide.ASK if self.position_qty > 0 else OrderSide.BID
            orders.append(Order(
                order_id=str(uuid.uuid4()), agent_id=self.agent_id,
                side=side, volume=float(remaining_qty), price=None,
                order_type=OrderType.MARKET, ttl=None,
                metadata={"role": "regime_disruptor", "phase": "stop_exit"},
            ))
            self._transition(Phase.COOLDOWN, now, mid)
            return orders

        if now < self._next_unwind_ts:
            return orders

        urgency = _clip(1.0 - max(1.0, self._phase_end_ts - now) / max(1.0, self.unwind_max_s), 0.0, 1.0)
        slice_frac = _clip(0.06 + 0.40 * urgency + random.uniform(0.0, 0.08), 0.05, 0.60)
        slice_qty = max(1.0, remaining_qty * slice_frac)
        if top_liq > 0:
            slice_qty = min(slice_qty, top_liq * random.uniform(0.20, 0.45))
            slice_qty = max(1.0, slice_qty)

        side = OrderSide.ASK if self.position_qty > 0 else OrderSide.BID
        use_limit = random.random() < (0.40 - 0.30 * urgency)
        if use_limit and best_bid is not None and best_ask is not None:
            px = (best_ask - TICK * random.randint(0, 1)) if side == OrderSide.ASK else \
                 (best_bid + TICK * random.randint(0, 1))
            orders.append(Order(
                order_id=str(uuid.uuid4()), agent_id=self.agent_id,
                side=side, volume=float(slice_qty), price=float(round(px, 5)),
                order_type=OrderType.LIMIT, ttl=random.randint(5, 15),
                metadata={"role": "regime_disruptor", "phase": "unwind_limit"},
            ))
        else:
            orders.append(Order(
                order_id=str(uuid.uuid4()), agent_id=self.agent_id,
                side=side, volume=float(slice_qty), price=None,
                order_type=OrderType.MARKET, ttl=None,
                metadata={"role": "regime_disruptor", "phase": "unwind_market"},
            ))

        self._next_unwind_ts = now + max(0.5, random.uniform(
            1.5 * (1 - 0.6 * urgency), max(2.0, 15.0 * (1 - 0.7 * urgency))
        ))
        if now >= self._phase_end_ts and abs(self.position_qty) > 1.0:
            side = OrderSide.ASK if self.position_qty > 0 else OrderSide.BID
            orders.append(Order(
                order_id=str(uuid.uuid4()), agent_id=self.agent_id,
                side=side, volume=float(abs(self.position_qty) * 0.6), price=None,
                order_type=OrderType.MARKET, ttl=None,
                metadata={"role": "regime_disruptor", "phase": "unwind_forced"},
            ))
        return orders

    # ── публичный API ─────────────────────────────────────────

    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        now = _ctx_now(market_context)
        best_bid, best_ask, mid, spread, top_liq = self._book_top(order_book)
        if mid <= 0.0:
            return []

        self._update_hist(now, mid, top_liq)

        if self.position_qty != 0 and self._stop_triggered(mid):
            if self.phase not in (Phase.UNWINDING, Phase.COOLDOWN, Phase.DORMANT):
                self._transition(Phase.UNWINDING, now, mid)

        if self.phase == Phase.DORMANT:
            return self._orders_dormant(now)
        elif self.phase == Phase.WATCHING:
            return self._orders_watching(now, mid)
        elif self.phase == Phase.PREPARING:
            return self._orders_preparing(now, mid, top_liq)
        elif self.phase == Phase.STRIKING:
            return self._orders_striking(now, mid, best_bid, best_ask, top_liq)
        elif self.phase == Phase.SUSTAINING:
            return self._orders_sustaining(now, mid, best_bid, best_ask)
        elif self.phase == Phase.UNWINDING:
            return self._orders_unwinding(now, mid, best_bid, best_ask, top_liq)
        elif self.phase == Phase.COOLDOWN:
            if now >= self._dormant_until:
                self._transition(Phase.DORMANT, now, mid)
            return []
        return []

    def on_order_filled(self, order_id, price, qty, side, slippage: float = 0.0):
        try:
            self._apply_fill(side, _safe(price), _safe(qty))
        except Exception:
            pass

    def perceive_market(self, market_context) -> str:
        return (
            f"regime_disruptor:{self.phase.value}"
            f":mandate={self.mandate_type.value if self.mandate_type else 'none'}"
            f":dir={self.mandate_dir}:r2={round(self._regime_r2, 2)}"
        )

    def restore_capital(self) -> None:
        pass

    def status(self) -> dict:
        return {
            "phase": self.phase.value,
            "mandate": self.mandate_type.value if self.mandate_type else None,
            "mandate_dir": self.mandate_dir,
            "regime_slope_ticks_s": round(self._regime_slope, 4),
            "regime_r2": round(self._regime_r2, 3),
            "regime_dir": self._regime_dir,
            "regime_stable_s": round(self._phase_start_ts - self._regime_detected_ts, 1)
                if self._regime_detected_ts > 0 else 0.0,
            "position_qty": round(self.position_qty, 2),
            "avg_entry": round(self.avg_entry, 5) if self.avg_entry else None,
            "realized_pnl": round(self.realized_pnl, 2),
            "strike_progress": f"{self._strike_waves_done}/{self._strike_waves_total}"
                if self.phase == Phase.STRIKING else None,
            "sustain_progress": round(
                self._sustain_done_qty / max(self._sustain_total_qty, 1.0), 3
            ) if self.phase == Phase.SUSTAINING else None,
        }