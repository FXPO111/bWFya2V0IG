# customer_aggregator_flow.py
# CustomerAggregatorFlow — агрегатор клиентского потока (retail + corp-smalls + algo-crumbs),
# который даёт ТОЛЬКО market-агрессию, но не одной кувалдой, а пачками слайсов.
#
# Важно:
# - Сервер вызывает generate_orders(order_book, order_book.market_context) раз в ~0.5s. :contentReference[oaicite:1]{index=1}
# - Филы приходят в on_order_filled(order_id, price, volume, side). :contentReference[oaicite:2]{index=2}
# - order_book.add_order для MARKET сразу матчит; latency из metadata сейчас не используется сервером.

import time
import uuid
import random
import os
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

from order import Order, OrderSide, OrderType

# ──────────────────────────────────────────────────────────────
# Внутренний "тикет" агрегатора (клиентская программа/пакет)
# ──────────────────────────────────────────────────────────────

@dataclass
class _Ticket:
    side: OrderSide
    total_notional: float
    done_notional: float
    start_ts: float
    deadline_ts: float
    urgency: float          # 0..1
    style: str              # 'retail' | 'corp' | 'algo'
    max_slice_notional: float
    min_slice_notional: float
    last_emit_ts: float


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _safe_mid(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if best_bid is None or best_ask is None:
        return None
    if best_ask <= 0:
        return None
    return 0.5 * (best_bid + best_ask)


def _snapshot_top(order_book, depth: int = 5) -> Dict[str, Any]:
    snap = order_book.get_order_book_snapshot(depth=depth)
    bids = snap.get("bids", []) or []
    asks = snap.get("asks", []) or []
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    return {"bids": bids, "asks": asks, "best_bid": best_bid, "best_ask": best_ask}


def _near_liq_notional(side: OrderSide, levels: List[Dict[str, Any]], k: int = 3) -> float:
    # грубая оценка "сколько стоит ликвидность" на первых k уровнях
    tot = 0.0
    for lv in (levels[:k] if levels else []):
        p = float(lv.get("price", 0.0))
        v = float(lv.get("volume", 0.0))
        tot += p * v
    return tot


# ──────────────────────────────────────────────────────────────
# АГЕНТ
# ──────────────────────────────────────────────────────────────

class CustomerAggregatorFlow:
    """
    Даёт market-агрессию как агрегированный клиентский поток.

    Паттерн работы (циклично, не one-shot):
      1) Поддерживает пул тикетов (tickets) — каждый тикет = клиентская программа
         (корп-платёж, розничный всплеск, микропоток от алго-клиентов).
      2) На каждом вызове generate_orders:
         - смотрит top-of-book
         - доздаёт тикеты с вероятностями, зависящими от spread/ликвидности
         - исполняет часть активных тикетов пачкой MARKET-слайсов
           (2..N штук за вызов), с естественными паузами/джиттером.
      3) Имеет inventory/risk-складку: если накопил перекос, начинает чаще
         добавлять тикеты противоположной стороны (как внутренний неттинг).
    """

    supports_conn = False

    def __init__(
        self,
        agent_id: str = "caf",
        capital: float = 300_000_000.0,
        *,
        seed: Optional[int] = None,

        # базовые интенсивности (скорости появления тикетов)
        retail_rate: float = 0.55,   # среднее число retail-тикетов в секунду (до фильтров)
        corp_rate: float = 0.12,     # corp-smalls
        algo_rate: float = 0.35,     # мелкие "крошки"

        # риск/неттинг
        max_inventory_notional_frac: float = 0.06,  # после этого начинаем принудительно балансировать
        rebalance_strength: float = 0.45,           # насколько агрессивно давим перекос
    ):
        self.agent_id = str(agent_id)
        self.capital = float(capital)

        if seed is not None:
            self._rng = random.Random(seed)
            self._seed_used = seed
        else:
            self._seed_used = int.from_bytes(os.urandom(4), 'big')
            self._rng = random.Random(self._seed_used)

        self._retail_rate = float(retail_rate)
        self._corp_rate = float(corp_rate)
        self._algo_rate = float(algo_rate)

        self._max_inv_frac = float(max_inventory_notional_frac)
        self._rebalance_strength = float(rebalance_strength)

        self._tickets: List[_Ticket] = []
        self._last_ts = time.time()

        # inventory в "ноционале": + значит купили (длинный риск), - значит продали
        self._inv_notional = 0.0

        # ограничение, чтобы не заспамить стакан
        self._max_orders_per_call = 18
        self._max_active_tickets = 24

        # естественные "режимы дня": меняются каждые 20–60 сек
        self._next_regime_ts = time.time() + self._rng.uniform(20.0, 60.0)
        self._regime_mult = 1.0

    # ──────────────────────────────────────────────────────────
    # Regime: меняет интенсивность/характер потока, чтобы не было ровной пилы
    # ──────────────────────────────────────────────────────────

    def _roll_regime(self, now: float):
        if now < self._next_regime_ts:
            return
        # режим: от "тихо" до "активно"
        self._regime_mult = self._rng.uniform(0.55, 1.75)
        self._next_regime_ts = now + self._rng.uniform(20.0, 60.0)

    # ──────────────────────────────────────────────────────────
    # Ticket generation
    # ──────────────────────────────────────────────────────────

    def _maybe_spawn_ticket(
        self,
        now: float,
        best_bid: Optional[float],
        best_ask: Optional[float],
        bids: List[Dict[str, Any]],
        asks: List[Dict[str, Any]],
    ):
        if len(self._tickets) >= self._max_active_tickets:
            return

        mid = _safe_mid(best_bid, best_ask)
        if mid is None:
            return

        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else 0.0
        near_bid = _near_liq_notional(OrderSide.BID, bids, k=3)
        near_ask = _near_liq_notional(OrderSide.ASK, asks, k=3)
        near_liq = max(1e-9, near_bid + near_ask)

        # чем шире спред и чем меньше ликвидность — тем меньше новых тикетов
        liquidity_factor = _clamp((near_liq / max(self.capital, 1.0)) * 10.0, 0.15, 1.25)
        spread_factor = 1.0
        if spread > 0:
            # грубо: при большом спреде клиенты реже "жмут"
            spread_factor = _clamp(0.9 / (1.0 + 40.0 * spread), 0.25, 1.0)

        # перекос inventory → повышаем шанс тикетов противоположной стороны (неттинг)
        inv_cap = max(1.0, self.capital)
        inv_frac = _clamp(abs(self._inv_notional) / inv_cap, 0.0, 10.0)

        # базовая вероятность направления (без трендовиков/стопосбривов)
        # это "клиентский шум" + эффект неттинга
        p_buy = 0.5
        if inv_frac > self._max_inv_frac:
            # если перекос сильный — давим в противоположную сторону
            if self._inv_notional > 0:
                p_buy -= self._rebalance_strength * _clamp((inv_frac - self._max_inv_frac) / self._max_inv_frac, 0.0, 1.0)
            else:
                p_buy += self._rebalance_strength * _clamp((inv_frac - self._max_inv_frac) / self._max_inv_frac, 0.0, 1.0)
        p_buy = _clamp(p_buy, 0.15, 0.85)

        # интенсивности появления тикетов (похоже на пуассон-процесс, но с фильтрами)
        dt = max(1e-6, now - self._last_ts)
        base = liquidity_factor * spread_factor * self._regime_mult

        # отдельные "источники" потока
        lam_retail = self._retail_rate * base
        lam_corp   = self._corp_rate   * base
        lam_algo   = self._algo_rate   * base

        # вероятность "события" за dt
        def hit(lam: float) -> bool:
            # 1 - exp(-lam*dt) (приблизим для малых dt)
            p = 1.0 - pow(2.718281828, -lam * dt)
            return self._rng.random() < _clamp(p, 0.0, 0.95)

        # розница: короткие, часто мелкие, иногда пачка
        if hit(lam_retail):
            side = OrderSide.BID if (self._rng.random() < p_buy) else OrderSide.ASK
            # размер в долях капитала (очень малый), но пачками
            notional = self.capital * self._rng.uniform(0.00008, 0.00055)
            deadline = now + self._rng.uniform(2.0, 10.0)
            urgency = self._rng.uniform(0.45, 0.95)
            max_slice = notional * self._rng.uniform(0.15, 0.40)
            min_slice = notional * self._rng.uniform(0.03, 0.10)
            self._tickets.append(_Ticket(side, notional, 0.0, now, deadline, urgency, "retail", max_slice, min_slice, now))

        # corp-smalls: чуть крупнее, дольше, чаще "догоняет дедлайн"
        if hit(lam_corp):
            side = OrderSide.BID if (self._rng.random() < p_buy) else OrderSide.ASK
            notional = self.capital * self._rng.uniform(0.00035, 0.00210)
            deadline = now + self._rng.uniform(8.0, 45.0)
            urgency = self._rng.uniform(0.25, 0.80)
            max_slice = notional * self._rng.uniform(0.08, 0.22)
            min_slice = notional * self._rng.uniform(0.02, 0.06)
            self._tickets.append(_Ticket(side, notional, 0.0, now, deadline, urgency, "corp", max_slice, min_slice, now))

        # algo crumbs: много мелких "тычков", короткие паузы, почти всегда маленькие слайсы
        if hit(lam_algo):
            side = OrderSide.BID if (self._rng.random() < p_buy) else OrderSide.ASK
            notional = self.capital * self._rng.uniform(0.00003, 0.00025)
            deadline = now + self._rng.uniform(1.0, 6.0)
            urgency = self._rng.uniform(0.55, 1.00)
            max_slice = notional * self._rng.uniform(0.25, 0.60)
            min_slice = notional * self._rng.uniform(0.07, 0.20)
            self._tickets.append(_Ticket(side, notional, 0.0, now, deadline, urgency, "algo", max_slice, min_slice, now))

    # ──────────────────────────────────────────────────────────
    # Execution: market-only slicing per ticket
    # ──────────────────────────────────────────────────────────

    def _ticket_remaining(self, t: _Ticket) -> float:
        return max(0.0, t.total_notional - t.done_notional)

    def _compute_ticket_slice_notional(
        self,
        t: _Ticket,
        now: float,
        spread: float,
        near_liq_ratio: float,
    ) -> float:
        """
        Делает "разумный" размер следующего слайса:
        - ближе к дедлайну → больше
        - выше urgency → больше
        - шире спред/хуже ликва → меньше
        """
        rem = self._ticket_remaining(t)
        if rem <= 0.0:
            return 0.0

        time_left = max(1e-6, t.deadline_ts - now)
        total_time = max(1e-6, t.deadline_ts - t.start_ts)
        pressure = 1.0 - _clamp(time_left / total_time, 0.0, 1.0)  # 0..1 чем ближе дедлайн, тем больше

        # спред/ликва фильтры: при плохих условиях режем
        spread_pen = 1.0 / (1.0 + 35.0 * max(0.0, spread))
        liq_bonus = _clamp(0.75 + 1.25 * near_liq_ratio, 0.35, 1.6)

        # базовый целевой кусок: доля остатка
        frac = 0.06 + 0.20 * t.urgency + 0.30 * pressure
        frac *= spread_pen
        frac *= liq_bonus
        frac = _clamp(frac, 0.02, 0.55)

        target = rem * frac

        # clamp по профилю тикета
        target = _clamp(target, t.min_slice_notional, t.max_slice_notional)
        target = min(target, rem)

        # джиттер, чтобы не было механистики
        target *= self._rng.uniform(0.75, 1.25)
        target = min(target, rem)
        return max(0.0, target)

    def _emit_market_slices(
        self,
        now: float,
        mid: float,
        spread: float,
        near_liq_ratio: float,
        max_orders: int,
    ) -> List[Order]:
        out: List[Order] = []
        if not self._tickets:
            return out

        # сортируем тикеты по "срочности" (дедлайн/urgency), но с шумом
        def score(t: _Ticket) -> float:
            rem = self._ticket_remaining(t)
            if rem <= 0:
                return -1e9
            time_left = max(1e-6, t.deadline_ts - now)
            base = (t.urgency + 0.65) / (time_left ** 0.35)
            return base * self._rng.uniform(0.92, 1.08)

        self._tickets.sort(key=score, reverse=True)

        # исполняем пачкой: 2..N ордеров за вызов, но с "естественными паузами" внутри тикета
        for t in list(self._tickets):
            if len(out) >= max_orders:
                break

            rem = self._ticket_remaining(t)
            if rem <= 0.0:
                continue

            # естественный "ритм" по стилю: не каждый тикет стреляет каждую итерацию
            min_gap = 0.0
            if t.style == "retail":
                min_gap = self._rng.uniform(0.05, 0.22)
            elif t.style == "corp":
                min_gap = self._rng.uniform(0.12, 0.45)
            else:  # algo
                min_gap = self._rng.uniform(0.02, 0.12)

            if now - t.last_emit_ts < min_gap:
                continue

            # сколько слайсов подряд дать из этого тикета сейчас
            if t.style == "corp":
                burst = self._rng.randint(1, 3)
            elif t.style == "retail":
                burst = self._rng.randint(1, 4)
            else:
                burst = self._rng.randint(1, 6)

            for _ in range(burst):
                if len(out) >= max_orders:
                    break

                slice_notional = self._compute_ticket_slice_notional(t, now, spread, near_liq_ratio)
                if slice_notional <= 0.0:
                    break

                qty = max(1.0, slice_notional / max(1e-9, mid))

                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=t.side,
                    volume=float(qty),
                    price=None,
                    order_type=OrderType.MARKET,
                    ttl=None,
                    metadata={
                        # сейчас сервер не использует latency для agent orders (add_order напрямую),
                        # но оставляем, если ты потом переведёшь агентов на schedule_add. :contentReference[oaicite:4]{index=4}
                        "latency_ms": int(self._rng.uniform(8, 40) if t.style != "corp" else self._rng.uniform(15, 75)),
                        "latency_jitter_ms": int(self._rng.uniform(3, 15)),
                        "flow": "customer_agg",
                        "style": t.style,
                    },
                )
                out.append(o)

                # считаем "заявленное" исполнение как done_notional,
                # а реальный риск/инвентарь подправится через on_order_filled
                t.done_notional += slice_notional
                t.last_emit_ts = now

                if self._ticket_remaining(t) <= 0.0:
                    break

        # чистка закрытых/просроченных
        alive: List[_Ticket] = []
        for t in self._tickets:
            if self._ticket_remaining(t) <= 0.0:
                continue
            if now > t.deadline_ts + 0.75:
                # если дедлайн прошёл — остаток выкидываем (клиент отменил/перенёс)
                continue
            alive.append(t)
        self._tickets = alive

        return out

    # ──────────────────────────────────────────────────────────
    # Public API expected by server
    # ──────────────────────────────────────────────────────────

    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        now = time.time()
        self._roll_regime(now)

        top = _snapshot_top(order_book, depth=6)
        best_bid = top["best_bid"]
        best_ask = top["best_ask"]
        bids = top["bids"]
        asks = top["asks"]

        mid = _safe_mid(best_bid, best_ask)
        if mid is None:
            self._last_ts = now
            return []

        spread = float(best_ask - best_bid) if (best_bid is not None and best_ask is not None) else 0.0

        # near-liq ratio: сколько нотионала рядом относительно капитала
        near_bid = _near_liq_notional(OrderSide.BID, bids, k=3)
        near_ask = _near_liq_notional(OrderSide.ASK, asks, k=3)
        near_ratio = _clamp((near_bid + near_ask) / max(1.0, self.capital), 0.0, 0.25)

        # 1) доздать тикеты
        self._maybe_spawn_ticket(now, best_bid, best_ask, bids, asks)

        # 2) исполнить часть тикетов пачкой market-слайсов
        orders = self._emit_market_slices(
            now=now,
            mid=mid,
            spread=spread,
            near_liq_ratio=near_ratio,
            max_orders=self._max_orders_per_call,
        )

        self._last_ts = now
        return orders

    def on_order_filled(self, order_id: str, price: float, volume: float, side: OrderSide):
        # инвентарь в нотионале (условно)
        px = float(price)
        qty = float(volume)
        notional = px * qty
        if side == OrderSide.BID:
            self._inv_notional += notional
        else:
            self._inv_notional -= notional

    # Optional (не обязаны, но пусть будут, чтобы не ломать общий стиль агентов)
    def restore_capital(self):
        pass

    def perceive_market(self, order_book, market_context=None):
        pass
