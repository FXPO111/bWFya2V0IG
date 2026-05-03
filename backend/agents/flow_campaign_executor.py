# flow_campaign_executor.py
#
# FlowCampaignExecutor — исполнение клиентского parent-order (MARKET-only).
# Логика:
#   - "Клиентский ордер" приходит (buy/sell + qty) чаще в боковике.
#   - Исполнение: серия MARKET-слайсов.
#   - После удара измеряет adverse impact (в тиках). Если слишком резко — пауза и ждёт откат/рефилл.
#   - Поддерживает participation cap (не доминировать минутный поток).
#
# Совместимость:
#   - Order(...) / OrderSide / OrderType как в order.py :contentReference[oaicite:1]{index=1}
#   - OrderBook.add_order матчится MARKET сразу :contentReference[oaicite:2]{index=2}
#   - trade_history хранится в книге (deque) :contentReference[oaicite:3]{index=3}

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
    # пытаемся быть совместимыми с разными ключами
    for k in ("ts", "timestamp", "time", "t"):
        if k in t:
            v = t.get(k)
            try:
                v = float(v)
                # если похоже на ms
                if v > 1e12:
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


def _taker_side(t: Dict[str, Any]) -> str:
    v = str(t.get("taker_side", t.get("initiator_side", "")) or "").lower()
    if v in ("buy", "bid"):
        return "buy"
    if v in ("sell", "ask"):
        return "sell"
    return ""


class FlowCampaignExecutor:
    supports_conn = False

    def __init__(
        self,
        agent_id: str,
        capital: float,
        *,
        # детектор боковика
        flat_window_s: float = 900.0,       # сколько смотрим диапазон
        flat_range_ticks: int = 100,          # max range в тиках для "боковик"
        min_mid_samples: int = 140,

        flat_efficiency_max: float = 0.22,  # |net move| / path length (чем меньше, тем больше "пила")
        flat_slope_ticks_per_min: float = 7.0,  # max дрейф (тиков/мин) чтобы не считать тренд боковиком
        flat_flip_rate_min: float = 0.25,  # доля смен знака приращений (choppiness)
        flat_chop_max_range_ticks: int = 120,  # защитный потолок range для "пилы"

        # генерация клиентских parent-order
        base_arrival_lambda_per_min: float = 0.020,   # 1.2 ордера/час в среднем
        flat_boost: float = 3.0,                      # в боковике чаще
        cooldown_s: Tuple[float, float] = (60.0, 240.0),

        # размер parent-order
        parent_notional_frac: Tuple[float, float] = (0.06, 0.18),  # доля капитала в notional на кампанию
        parent_qty_min: float = 60_000.0,             # нижняя граница в qty (под 100 цену это 6M notional)
        parent_qty_max: float = 1_300_000.0,          # верхняя граница

        # исполнение (пульс/пауза)
        slice_frac_of_l1: Tuple[float, float] = (0.18, 0.38),
        min_slice_qty: float = 600.0,
        max_slice_qty: float = 12_000.0,
        max_slice_notional_frac: float = 0.00035,     # cap одного удара от капитала
        min_dt_s: float = 0.35,
        max_dt_s: float = 1.20,

        # контроль импакта/отката
        impact_pause_ticks: int = 10,         # если adverse >= этого — пауза
        retrace_ticks: Tuple[int, int] = (3, 7),
        max_wait_retrace_s: float = 9.0,      # максимум ждать отката, потом можно продолжить если рефилл есть
        refill_need_frac: float = 0.72,       # L1 должен вернуться хотя бы на 72% от L1 на момент удара

        # participation cap (чтобы не "быть рынком")
        part_window_s: float = 60.0,
        part_cap_frac: float = 0.12,          # до 12% minute-flow

        # дедлайн кампании
        campaign_deadline_s: Tuple[float, float] = (900.0, 5400.0),  # 15–90 минут
    ):
        self.agent_id = str(agent_id)
        self.capital = float(capital)

        self.flat_window_s = float(flat_window_s)
        self.flat_range_ticks = int(flat_range_ticks)
        self.min_mid_samples = int(min_mid_samples)

        self.flat_efficiency_max = float(flat_efficiency_max)
        self.flat_slope_ticks_per_min = float(flat_slope_ticks_per_min)
        self.flat_flip_rate_min = float(flat_flip_rate_min)
        self.flat_chop_max_range_ticks = int(flat_chop_max_range_ticks)

        self.base_arrival_lambda_per_min = float(base_arrival_lambda_per_min)
        self.flat_boost = float(flat_boost)
        self.cooldown_s = (float(cooldown_s[0]), float(cooldown_s[1]))

        self.parent_notional_frac = (float(parent_notional_frac[0]), float(parent_notional_frac[1]))
        self.parent_qty_min = float(parent_qty_min)
        self.parent_qty_max = float(parent_qty_max)

        self.slice_frac_of_l1 = (float(slice_frac_of_l1[0]), float(slice_frac_of_l1[1]))
        self.min_slice_qty = float(min_slice_qty)
        self.max_slice_qty = float(max_slice_qty)
        self.max_slice_notional_frac = float(max_slice_notional_frac)
        self.min_dt_s = float(min_dt_s)
        self.max_dt_s = float(max_dt_s)

        self.impact_pause_ticks = int(impact_pause_ticks)
        self.retrace_ticks = (int(retrace_ticks[0]), int(retrace_ticks[1]))
        self.max_wait_retrace_s = float(max_wait_retrace_s)
        self.refill_need_frac = float(refill_need_frac)

        self.part_window_s = float(part_window_s)
        self.part_cap_frac = float(part_cap_frac)

        self.campaign_deadline_s = (float(campaign_deadline_s[0]), float(campaign_deadline_s[1]))

        # ── состояние рынка ──
        self._mid_hist = deque(maxlen=12000)  # (ts, mid)
        self._last_now = None

        # ── состояние кампании ──
        self.active = False
        self.side: Optional[OrderSide] = None
        self.arrival_mid: Optional[float] = None
        self.target_qty: float = 0.0
        self.remaining_qty: float = 0.0

        self._deadline_ts: float = 0.0
        self._cooldown_until: float = 0.0

        self._next_action_ts: float = 0.0

        # импакт-контроль
        self._last_pulse_mid: Optional[float] = None
        self._last_pulse_ts: float = 0.0
        self._last_pulse_l1: float = 0.0

        self._waiting_retrace: bool = False
        self._retrace_target_mid: Optional[float] = None
        self._wait_start_ts: float = 0.0

        # participation
        self._exec_hist = deque()  # (ts, qty)

        # child order tracking
        self._oid_to_qty = {}

        # burst structure
        self._burst_left = 0
        self._burst_pause_until = 0.0

    def restore_capital(self):
        return

    def perceive_market(self, market_context):
        return "flow_campaign_executor"

    def on_order_filled(self, order_id, price, qty, side, slippage: float = 0.0):
        # server зовёт (order_id, price, volume, OrderSide) — как в твоих агентах
        try:
            q = float(qty)
            if q <= 0:
                return
            now = time.time()
            self._exec_hist.append((now, q))

            # снимаем remaining по факту
            if self.active:
                self.remaining_qty = max(0.0, self.remaining_qty - q)

            # чистим карту
            if order_id in self._oid_to_qty:
                self._oid_to_qty.pop(order_id, None)
        except Exception:
            pass

    # ───────────── helpers ─────────────

    def _is_flat(self, now: float) -> bool:
        if len(self._mid_hist) < self.min_mid_samples:
            return False

        cutoff = now - self.flat_window_s
        mids = []
        for ts, m in reversed(self._mid_hist):
            if ts < cutoff:
                break
            mids.append(float(m))

        if len(mids) < self.min_mid_samples:
            return False

        mids.reverse()

        lo, hi = min(mids), max(mids)
        range_ticks = (hi - lo) / TICK

        first, last = mids[0], mids[-1]
        net_ticks = abs(last - first) / TICK

        # path length + flip rate
        path_ticks = 0.0
        flips = 0
        prev_sign = 0
        for i in range(1, len(mids)):
            d = mids[i] - mids[i - 1]
            if d != 0.0:
                sign = 1 if d > 0 else -1
                if prev_sign != 0 and sign != prev_sign:
                    flips += 1
                prev_sign = sign
            path_ticks += abs(d) / TICK

        efficiency = net_ticks / max(path_ticks, 1e-9)  # 0..1
        flip_rate = flips / max(len(mids) - 2, 1)

        duration_min = max(self.flat_window_s / 60.0, 1e-9)
        slope_ticks_per_min = ((last - first) / TICK) / duration_min

        # 1) tight-range flat (старый смысл, но на нормальном окне)
        tight = range_ticks <= float(self.flat_range_ticks)

        # 2) choppy consolidation: большой распил, но без направленного дрейфа
        choppy = (
                range_ticks <= float(self.flat_chop_max_range_ticks)
                and abs(slope_ticks_per_min) <= float(self.flat_slope_ticks_per_min)
                and efficiency <= float(self.flat_efficiency_max)
                and flip_rate >= float(self.flat_flip_rate_min)
        )

        return tight or choppy

    def _prune_exec_hist(self, now: float):
        cutoff = now - self.part_window_s
        while self._exec_hist and self._exec_hist[0][0] < cutoff:
            self._exec_hist.popleft()

    def _executed_qty_window(self, now: float) -> float:
        self._prune_exec_hist(now)
        return sum(q for _, q in self._exec_hist)

    def _market_qty_window(self, order_book, now: float) -> float:
        # сумма трейдов за окно
        cutoff = now - self.part_window_s
        s = 0.0
        try:
            trades = getattr(order_book, "trade_history", None)
            if not trades:
                return 0.0
            for t in reversed(trades):
                ts = _trade_ts(t)
                if ts < cutoff:
                    break
                s += _trade_vol(t)
        except Exception:
            return 0.0
        return s

    def _maybe_spawn_parent(self, now: float, mid: float, flat: bool):
        if now < self._cooldown_until:
            return
        if self.active:
            return

        # интенсивность прихода заявки
        lam = self.base_arrival_lambda_per_min * (self.flat_boost if flat else 1.0)  # в минуту
        # дискретизация по dt
        if self._last_now is None:
            dt = 0.4
        else:
            dt = max(0.05, min(2.0, now - self._last_now))
        p = 1.0 - pow(2.718281828, -(lam / 60.0) * dt)  # Poisson -> Bernoulli
        if random.random() > p:
            return

        # формируем "клиентский ордер"
        side = OrderSide.BID if random.random() < 0.5 else OrderSide.ASK
        notional = self.capital * random.uniform(self.parent_notional_frac[0], self.parent_notional_frac[1])
        qty = max(1.0, notional / max(mid, 1e-9))
        qty = _clip(qty, self.parent_qty_min, self.parent_qty_max)

        self.active = True
        self.side = side
        self.arrival_mid = mid
        self.target_qty = float(qty)
        self.remaining_qty = float(qty)

        self._deadline_ts = now + random.uniform(self.campaign_deadline_s[0], self.campaign_deadline_s[1])
        self._next_action_ts = now + random.uniform(0.2, 1.2)

        self._last_pulse_mid = None
        self._waiting_retrace = False
        self._retrace_target_mid = None
        self._wait_start_ts = 0.0

        self._burst_left = random.randint(4, 11)
        self._burst_pause_until = 0.0

    def _should_pause_for_participation(self, order_book, now: float) -> bool:
        exec_q = self._executed_qty_window(now)
        mkt_q = self._market_qty_window(order_book, now)
        if mkt_q <= 1e-9:
            return False
        return (exec_q / mkt_q) >= self.part_cap_frac

    def _impact_adverse_ticks(self, mid_now: float) -> float:
        if self._last_pulse_mid is None:
            return 0.0
        if self.side == OrderSide.BID:
            return (mid_now - self._last_pulse_mid) / TICK  # buy -> рост цены ухудшает
        else:
            return (self._last_pulse_mid - mid_now) / TICK  # sell -> падение цены ухудшает

    def _retrace_ok(self, mid_now: float, bid_sz1: float, ask_sz1: float) -> bool:
        # 1) проверка целевого отката
        if self._retrace_target_mid is not None:
            if self.side == OrderSide.BID:
                if mid_now <= self._retrace_target_mid:
                    return True
            else:
                if mid_now >= self._retrace_target_mid:
                    return True

        # 2) или рефилл L1
        l1_now = ask_sz1 if self.side == OrderSide.BID else bid_sz1
        if self._last_pulse_l1 > 1e-9 and l1_now >= self._last_pulse_l1 * self.refill_need_frac:
            return True

        # 3) или истёк max_wait_retrace_s — тогда разрешаем продолжить
        if self._wait_start_ts > 0 and (time.time() - self._wait_start_ts) >= self.max_wait_retrace_s:
            return True

        return False

    # ───────────── main loop ─────────────

    def generate_orders(self, order_book, market_context) -> List[Order]:
        orders: List[Order] = []
        now = _ctx_now(market_context)

        # snapshot L1
        try:
            snap = order_book.get_order_book_snapshot(depth=1)
        except Exception:
            return orders

        best_bid, best_ask, mid, spread, bid_sz1, ask_sz1 = _best_from_snapshot(snap)
        if mid is None:
            self._last_now = now
            return orders

        self._mid_hist.append((now, mid))

        flat = self._is_flat(now)

        # если не активны — пробуем получить клиентский ордер (чаще в боковике)
        self._maybe_spawn_parent(now, mid, flat)

        # если кампании нет — всё
        if not self.active or self.side is None:
            self._last_now = now
            return orders

        # дедлайн: если время вышло — выключаемся
        if now >= self._deadline_ts or self.remaining_qty <= 1e-6:
            self.active = False
            self.side = None
            self.arrival_mid = None
            self.target_qty = 0.0
            self.remaining_qty = 0.0
            self._cooldown_until = now + random.uniform(self.cooldown_s[0], self.cooldown_s[1])
            self._last_now = now
            return orders

        # participation cap
        if self._should_pause_for_participation(order_book, now):
            self._next_action_ts = max(self._next_action_ts, now + random.uniform(0.25, 0.90))
            self._last_now = now
            return orders

        # burst-пауза
        if now < self._burst_pause_until:
            self._last_now = now
            return orders

        # частота действий
        if now < self._next_action_ts:
            self._last_now = now
            return orders

        # если ждём откат/рефилл — проверяем
        if self._waiting_retrace:
            if self._retrace_ok(mid, bid_sz1, ask_sz1):
                self._waiting_retrace = False
                self._retrace_target_mid = None
                self._wait_start_ts = 0.0
                # после паузы обычно не сразу снова бьём
                self._next_action_ts = now + random.uniform(0.15, 0.55)
            else:
                self._next_action_ts = now + random.uniform(0.10, 0.35)
                self._last_now = now
                return orders

        # контроль импакта: если после прошлого удара цена ушла слишком далеко в "плохую" сторону — пауза
        adverse = self._impact_adverse_ticks(mid)
        if adverse >= float(self.impact_pause_ticks):
            rt = random.randint(self.retrace_ticks[0], self.retrace_ticks[1])
            if self.side == OrderSide.BID:
                self._retrace_target_mid = mid - rt * TICK
            else:
                self._retrace_target_mid = mid + rt * TICK
            self._waiting_retrace = True
            self._wait_start_ts = time.time()
            self._next_action_ts = now + random.uniform(0.25, 0.90)
            self._last_now = now
            return orders

        # вычисляем размер слайса от L1 по стороне исполнения
        l1_take = ask_sz1 if self.side == OrderSide.BID else bid_sz1
        frac = random.uniform(self.slice_frac_of_l1[0], self.slice_frac_of_l1[1])
        qty = max(self.min_slice_qty, l1_take * frac)

        # cap по notional от капитала
        max_notional = self.capital * self.max_slice_notional_frac
        qty = min(qty, max_notional / max(mid, 1e-9))

        qty = _clip(qty, self.min_slice_qty, self.max_slice_qty)
        qty = min(qty, self.remaining_qty)

        # safety: если вообще нечего делать — пауза
        if qty <= 0.5:
            self._next_action_ts = now + random.uniform(0.30, 1.10)
            self._last_now = now
            return orders

        # создать MARKET ордер
        oid = str(uuid.uuid4())
        o = Order(
            order_id=oid,
            agent_id=self.agent_id,
            side=self.side,
            volume=float(qty),
            price=None,
            order_type=OrderType.MARKET,
            ttl=None,
            metadata={
                # latency обработает только schedule_add; add_order() игнорирует для MARKET — это ок
                "latency_ms": random.randint(5, 25),
                "latency_jitter_ms": random.randint(0, 20),
            }
        )
        orders.append(o)
        self._oid_to_qty[oid] = float(qty)

        # фиксируем "точку удара" для измерения импакта на следующих тиках
        self._last_pulse_mid = float(mid)
        self._last_pulse_ts = float(now)
        self._last_pulse_l1 = float(l1_take)

        # burst-структура: несколько ударов, затем пауза под рефилл
        self._burst_left -= 1
        if self._burst_left <= 0:
            self._burst_left = random.randint(4, 11)
            self._burst_pause_until = now + random.uniform(1.8, 6.5)
        else:
            self._burst_pause_until = 0.0

        # следующий тик действия
        self._next_action_ts = now + random.uniform(self.min_dt_s, self.max_dt_s)
        self._last_now = now
        return orders
