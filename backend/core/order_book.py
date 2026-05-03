import heapq
import time
import random
import threading
from bisect import bisect_left, insort
from collections import deque
from typing import Dict, Optional, List, Any

from backend.core.order import Order, OrderSide, OrderType
from backend.core.order import quant


# ──────────────────────────────────────────────────────────────
# Событийная модель
# ──────────────────────────────────────────────────────────────

class EventKind:
    ACTIVATE  = 'activate'    # ордер становится активным после latency
    MATCH     = 'match'       # запуск матчинга
    CANCEL    = 'cancel'      # отмена
    EXPIRE    = 'expire'      # истечение TTL
    REPLENISH = 'replenish'   # пополнение айсберга


class Event:
    __slots__ = ("ts_ms", "kind", "oid", "payload")

    def __init__(self, ts_ms: int, kind: str, oid: Optional[str] = None, payload: Optional[dict] = None):
        self.ts_ms = int(ts_ms)
        self.kind = kind
        self.oid = oid
        self.payload = payload or {}

    def __lt__(self, other: "Event"):
        return self.ts_ms < other.ts_ms


class EventQueue:
    def __init__(self):
        self._h: List[Event] = []

    def push(self, ev: Event):
        heapq.heappush(self._h, ev)

    def pop_ready(self, now_ms: int, budget: int) -> List[Event]:
        out: List[Event] = []
        while self._h and len(out) < budget and self._h[0].ts_ms <= now_ms:
            out.append(heapq.heappop(self._h))
        return out


# ──────────────────────────────────────────────────────────────
# Уровень цены
# ──────────────────────────────────────────────────────────────

class PriceLevel:
    def __init__(self, price: float):
        self.price = price
        self.orders: deque[Order] = deque()

    def add_order(self, order: Order):
        self.orders.append(order)

    def remove_filled_or_inactive_orders(self):
        # Чистим голову; FIFO сохраняется.
        # Внутренние "мёртвые" ордера не трогаем, чтобы не ломать price-time порядок.
        while self.orders and not self.orders[0].is_active():
            self.orders.popleft()

    def total_volume_visible(self) -> float:
        s = 0.0
        for o in self.orders:
            if not o.is_active():
                continue
            s += _visible_remaining(o)
        return s

    def __bool__(self):
        if not self.orders:
            return False

        for o in self.orders:
            if o.is_active() and _visible_remaining(o) > 0.0:
                return True

        return False

    def __repr__(self):
        return f"PriceLevel(price={self.price}, count={len(self.orders)})"


# ──────────────────────────────────────────────────────────────
# Айсберги
# metadata: {'display_qty': X, 'reserve_qty': Y, 'chunk': Z, 'replenish_ms': M}
# ──────────────────────────────────────────────────────────────

def _visible_remaining(o: Order) -> float:
    md = getattr(o, "metadata", {}) or {}
    disp = md.get("display_qty", None)

    if disp is None:
        return max(0.0, float(o.remaining_volume()))

    if not hasattr(o, "_visible_left"):
        total = float(o.remaining_volume())
        init_visible = min(float(disp), total)
        o._visible_left = init_visible

        explicit_reserve = md.get("reserve_qty", None)
        if explicit_reserve is None:
            o._reserve_left = max(0.0, total - init_visible)
        else:
            o._reserve_left = float(explicit_reserve)

    rem = float(o.remaining_volume())
    cur_vis = float(getattr(o, "_visible_left", 0.0))
    cur_res = float(getattr(o, "_reserve_left", 0.0))

    if cur_vis + cur_res > rem + 1e-12:
        delta = (cur_vis + cur_res) - rem

        if cur_vis >= delta:
            cur_vis -= delta
        else:
            delta -= cur_vis
            cur_vis = 0.0
            cur_res = max(0.0, cur_res - delta)

        o._visible_left = cur_vis
        o._reserve_left = cur_res

    return max(0.0, float(getattr(o, "_visible_left", 0.0)))


def _consume_visible(o: Order, qty: float):
    md = getattr(o, "metadata", {}) or {}

    if md.get("display_qty", None) is None:
        return

    if not hasattr(o, "_visible_left"):
        _visible_remaining(o)

    o._visible_left = max(0.0, float(o._visible_left) - float(qty))


def _schedule_replenish_if_needed(book: "OrderBook", o: Order, now_ms: int):
    md = getattr(o, "metadata", {}) or {}

    if md.get("display_qty", None) is None:
        return

    if not hasattr(o, "_visible_left"):
        _visible_remaining(o)

    if o._visible_left <= 1e-12 and getattr(o, "_reserve_left", 0.0) > 1e-12 and o.is_active():
        chunk = float(md.get("chunk", md.get("display_qty", 0.0)))
        add = min(chunk, float(o._reserve_left))
        o._pending_replenish = add

        delay = int(md.get("replenish_ms", 200))
        book.eq.push(Event(
            ts_ms=now_ms + max(1, delay),
            kind=EventKind.REPLENISH,
            oid=o.order_id
        ))


# ──────────────────────────────────────────────────────────────
# Книга ордеров
# ──────────────────────────────────────────────────────────────

class OrderBook:
    def __init__(self):
        # ВАЖНО:
        # Все публичные чтения/мутации стакана проходят через этот lock.
        # RLock нужен, потому что tick -> step -> handlers -> cancel/match могут
        # вызываться вложенно.
        self._lock = threading.RLock()

        self.bids: Dict[float, PriceLevel] = {}
        self.asks: Dict[float, PriceLevel] = {}

        # Старые heaps оставлены для совместимости best_bid/best_ask.
        self.bid_prices: List[float] = []   # max-heap через отрицание
        self.ask_prices: List[float] = []   # min-heap

        # Быстрый индекс цен для snapshot.
        # Оба списка ascending.
        # BID snapshot идёт с конца, ASK snapshot идёт с начала.
        self._bid_price_index: List[float] = []
        self._ask_price_index: List[float] = []

        self.orders: Dict[str, Order] = {}
        self.last_trade_price: Optional[float] = None
        self.trade_history: deque[Dict[str, Any]] = deque(maxlen=1000)
        self._trade_seq = 0

        self.eq = EventQueue()
        self.market_context = None

        # --- perf / housekeeping ---
        self._ttl_orders: set[str] = set()
        self._last_housekeep_ms: int = 0
        self._last_heap_compact_ms: int = 0

        # legacy ttl cadence: старый серверный tick был примерно 0.3s
        self._last_legacy_ttl_ms: int = 0
        self._legacy_ttl_interval_ms: int = 300

        self._housekeep_bid_cursor: int = 0
        self._housekeep_ask_cursor: int = 0
        self._housekeep_budget_per_side: int = 750

    # ── price-index helpers ───────────────────────────────────

    def _index_for_side(self, side: OrderSide) -> List[float]:
        return self._bid_price_index if side == OrderSide.BID else self._ask_price_index

    def _insert_price_into_index(self, side: OrderSide, price: float) -> None:
        price = quant(price)
        arr = self._index_for_side(side)

        i = bisect_left(arr, price)
        if i >= len(arr) or arr[i] != price:
            insort(arr, price)

    def _remove_price_from_index(self, side: OrderSide, price: float) -> None:
        price = quant(price)
        arr = self._index_for_side(side)

        i = bisect_left(arr, price)
        if i < len(arr) and arr[i] == price:
            arr.pop(i)

    def _rebuild_price_indexes(self) -> None:
        self._bid_price_index = sorted(self.bids.keys())
        self._ask_price_index = sorted(self.asks.keys())

    # ── levels / heaps ────────────────────────────────────────

    def _add_price_level(self, side: OrderSide, price: float):
        price = quant(price)

        if side == OrderSide.BID:
            if price not in self.bids:
                self.bids[price] = PriceLevel(price)
                heapq.heappush(self.bid_prices, -price)
                self._insert_price_into_index(OrderSide.BID, price)
        else:
            if price not in self.asks:
                self.asks[price] = PriceLevel(price)
                heapq.heappush(self.ask_prices, price)
                self._insert_price_into_index(OrderSide.ASK, price)

    def _remove_price_level_if_empty(self, side: OrderSide, price: float):
        price = quant(price)

        if side == OrderSide.BID:
            pl = self.bids.get(price)
            if pl is None or not pl:
                self.bids.pop(price, None)
                self._remove_price_from_index(OrderSide.BID, price)
        else:
            pl = self.asks.get(price)
            if pl is None or not pl:
                self.asks.pop(price, None)
                self._remove_price_from_index(OrderSide.ASK, price)

    def _best_bid_price_unlocked(self) -> Optional[float]:
        while self.bid_prices:
            p = quant(-self.bid_prices[0])
            if p in self.bids and bool(self.bids[p]):
                return p
            heapq.heappop(self.bid_prices)
        return None

    def _best_ask_price_unlocked(self) -> Optional[float]:
        while self.ask_prices:
            p = quant(self.ask_prices[0])
            if p in self.asks and bool(self.asks[p]):
                return p
            heapq.heappop(self.ask_prices)
        return None

    def _best_bid_price(self) -> Optional[float]:
        with self._lock:
            return self._best_bid_price_unlocked()

    def _best_ask_price(self) -> Optional[float]:
        with self._lock:
            return self._best_ask_price_unlocked()

    def _track_ttl_order(self, order: Optional[Order]) -> None:
        if not order:
            return

        oid = order.order_id
        ttl = getattr(order, "ttl", None)

        if ttl is not None and ttl > 0 and order.is_active():
            self._ttl_orders.add(oid)
        else:
            self._ttl_orders.discard(oid)

    def _unindex_if_inactive(self, order: Optional[Order]) -> None:
        if not order:
            return

        if not order.is_active():
            oid = order.order_id
            self.orders.pop(oid, None)
            self._ttl_orders.discard(oid)

    def _housekeep_side(self, side: OrderSide, budget: int) -> None:
        arr = self._bid_price_index if side == OrderSide.BID else self._ask_price_index
        book = self.bids if side == OrderSide.BID else self.asks

        if not arr:
            if side == OrderSide.BID:
                self._housekeep_bid_cursor = 0
            else:
                self._housekeep_ask_cursor = 0
            return

        cursor = self._housekeep_bid_cursor if side == OrderSide.BID else self._housekeep_ask_cursor
        cursor = min(max(cursor, 0), len(arr) - 1)

        checked = 0
        while arr and checked < budget:
            if cursor >= len(arr):
                cursor = 0

            price = arr[cursor]
            level = book.get(price)

            if level is None:
                arr.pop(cursor)
                checked += 1
                continue

            level.remove_filled_or_inactive_orders()

            if not level:
                book.pop(price, None)
                arr.pop(cursor)
                checked += 1
                continue

            cursor += 1
            checked += 1

            if cursor >= len(arr):
                cursor = 0
                break

        if side == OrderSide.BID:
            self._housekeep_bid_cursor = cursor
        else:
            self._housekeep_ask_cursor = cursor

    def _maybe_housekeep(self, now_ms: int) -> None:
        # 1) Компактим heaps цен, если в них накопился мусор.
        if now_ms - self._last_heap_compact_ms >= 2000:
            if len(self.bid_prices) > 4 * len(self.bids) + 1024:
                self.bid_prices = [-p for p in self.bids.keys()]
                heapq.heapify(self.bid_prices)

            if len(self.ask_prices) > 4 * len(self.asks) + 1024:
                self.ask_prices = [p for p in self.asks.keys()]
                heapq.heapify(self.ask_prices)

            if len(self._bid_price_index) > len(self.bids) + 1024 or len(self._ask_price_index) > len(self.asks) + 1024:
                self._rebuild_price_indexes()

            self._last_heap_compact_ms = now_ms

        # 2) Чистим пустые уровни не полным проходом, а порциями.
        if now_ms - self._last_housekeep_ms >= 100:
            self._housekeep_side(OrderSide.BID, self._housekeep_budget_per_side)
            self._housekeep_side(OrderSide.ASK, self._housekeep_budget_per_side)
            self._last_housekeep_ms = now_ms

    # ── public API ────────────────────────────────────────────

    def add_order(self, order: Order) -> List[Dict[str, Any]]:
        with self._lock:
            return self._add_order_unlocked(order)

    def _add_order_unlocked(self, order: Order) -> List[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)

        # CANCEL:
        # Если задержка явно не задана — отменяем сразу.
        # Иначе cancel-события могут копиться и проходить пачкой.
        if order.order_type == OrderType.CANCEL:
            md = getattr(order, "metadata", {}) or {}
            lat = int(md.get("latency_ms", 0) or 0)
            jitter = int(md.get("latency_jitter_ms", 0) or 0)

            if lat <= 0 and jitter <= 0:
                self._cancel_order_unlocked(order.order_id)
                return []

            when = now_ms + max(0, lat) + random.randint(0, max(0, jitter))
            self.eq.push(Event(ts_ms=when, kind=EventKind.CANCEL, oid=order.order_id))
            return []

        # Некорректную лимитку не индексируем.
        if order.order_type == OrderType.LIMIT and order.price is None:
            return []

        # Индексируем только не-CANCEL.
        self.orders[order.order_id] = order

        ttl_ms = int(getattr(order, "metadata", {}).get("ttl_ms", 0) or 0)
        if ttl_ms > 0:
            self.eq.push(Event(now_ms + ttl_ms, EventKind.EXPIRE, oid=order.order_id))

        if order.order_type == OrderType.LIMIT:
            order.price = quant(order.price)

            # Важно:
            # маркетабельная лимитка НЕ кладётся сначала в книгу.
            # Сначала она исполняет противоположную сторону до своей limit price.
            # Только остаток становится resting liquidity.
            if order.side == OrderSide.BID:
                best_ask = self._best_ask_price_unlocked()
                if best_ask is not None and best_ask <= order.price:
                    return self._match_limit_order_unlocked(order)
            else:
                best_bid = self._best_bid_price_unlocked()
                if best_bid is not None and best_bid >= order.price:
                    return self._match_limit_order_unlocked(order)

            self._rest_limit_order_unlocked(order)
            return []

        return self._match_market_order_unlocked(order)

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            return self._cancel_order_unlocked(order_id)

    def _cancel_order_unlocked(self, order_id: str) -> bool:
        order = self.orders.get(order_id)

        if order and order.is_active():
            order.cancel()

            if hasattr(order, "_visible_left"):
                order._visible_left = 0.0
                order._reserve_left = 0.0

            self.orders.pop(order_id, None)
            self._ttl_orders.discard(order_id)
            return True

        return False

    # ── event API ─────────────────────────────────────────────

    def schedule_add(self, order: Order, now_ms: int):
        with self._lock:
            self._schedule_add_unlocked(order, now_ms)

    def _schedule_add_unlocked(self, order: Order, now_ms: int):
        md = getattr(order, "metadata", {}) or {}
        lat = int(md.get("latency_ms", 0) or 0)
        jitter = int(md.get("latency_jitter_ms", 5) or 5)
        act = now_ms + max(0, lat) + random.randint(0, max(0, jitter))

        if order.order_type == OrderType.CANCEL:
            if lat <= 0 and jitter <= 0:
                self._cancel_order_unlocked(order.order_id)
                return

            self.eq.push(Event(ts_ms=act, kind=EventKind.CANCEL, oid=order.order_id))
            return

        if order.order_type == OrderType.LIMIT and order.price is None:
            return

        self.orders[order.order_id] = order

        ttl_ms = int(md.get("ttl_ms", 0) or 0)
        if ttl_ms > 0:
            self.eq.push(Event(ts_ms=act + ttl_ms, kind=EventKind.EXPIRE, oid=order.order_id))

        self.eq.push(Event(
            ts_ms=act,
            kind=EventKind.ACTIVATE,
            oid=order.order_id,
            payload={"order": order}
        ))

    def schedule_cancel(self, order_id: str, now_ms: int, latency_ms: int = 0):
        with self._lock:
            if latency_ms <= 0:
                self._cancel_order_unlocked(order_id)
                return

            self.eq.push(Event(
                ts_ms=now_ms + max(0, latency_ms),
                kind=EventKind.CANCEL,
                oid=order_id
            ))

    # ── engine step ───────────────────────────────────────────

    def step(self, now_ms: int, max_events_per_tick: int = 10000) -> List[Dict[str, Any]]:
        with self._lock:
            return self._step_unlocked(now_ms, max_events_per_tick)

    def _step_unlocked(self, now_ms: int, max_events_per_tick: int = 10000) -> List[Dict[str, Any]]:
        trades_accum: List[Dict[str, Any]] = []
        budget = int(max_events_per_tick)

        while budget > 0:
            ready = self.eq.pop_ready(now_ms, budget)
            if not ready:
                break

            budget -= len(ready)

            for ev in ready:
                k = ev.kind

                if k == EventKind.ACTIVATE:
                    trades_accum += self._on_activate_unlocked(ev.payload["order"], now_ms)

                elif k == EventKind.MATCH:
                    trades_accum += self._on_match_unlocked(now_ms, ev.payload.get("order"))

                elif k == EventKind.CANCEL:
                    self._on_cancel_unlocked(ev.oid)

                elif k == EventKind.EXPIRE:
                    self._on_expire_unlocked(ev.oid)

                elif k == EventKind.REPLENISH:
                    trades_accum += self._on_replenish_unlocked(ev.oid, now_ms)

        # Аварийная страховка.
        # В норме после _match_limit_order_unlocked стакан не должен быть crossed.
        if self._is_crossed_unlocked():
            trades_accum += self._repair_crossed_book_unlocked()

        self._maybe_housekeep(now_ms)
        return trades_accum

    # ── event handlers ───────────────────────────────────────

    def _on_activate_unlocked(self, order: Order, now_ms: int) -> List[Dict[str, Any]]:
        if (not order) or (order.order_id not in self.orders) or (not order.is_active()):
            return []

        try:
            # Для price-time приоритета важен фактический приход в книгу.
            order.timestamp = time.time()
        except Exception:
            pass

        if order.order_type == OrderType.LIMIT:
            if order.price is None:
                return []

            order.price = quant(order.price)

            # Delayed/activated лимитка тоже не должна сначала становиться resting,
            # если она уже маркетабельна.
            if order.side == OrderSide.BID:
                best_ask = self._best_ask_price_unlocked()
                if best_ask is not None and best_ask <= order.price:
                    return self._match_limit_order_unlocked(order)
            else:
                best_bid = self._best_bid_price_unlocked()
                if best_bid is not None and best_bid >= order.price:
                    return self._match_limit_order_unlocked(order)

            self._rest_limit_order_unlocked(order)
            return []

        return self._match_market_order_unlocked(order)

    def _on_match_unlocked(self, now_ms: int, maybe_order: Optional[Order]) -> List[Dict[str, Any]]:
        if maybe_order is not None:
            return self._match_market_order_unlocked(maybe_order)

        return self._match_unlocked()

    def _on_cancel_unlocked(self, oid: str):
        self._cancel_order_unlocked(oid)

    def _on_expire_unlocked(self, oid: str):
        o = self.orders.get(oid)

        if o and o.is_active():
            o.cancel()

        self.orders.pop(oid, None)
        self._ttl_orders.discard(oid)

    def _on_replenish_unlocked(self, oid: str, now_ms: int) -> List[Dict[str, Any]]:
        o = self.orders.get(oid)

        if not o or not o.is_active():
            return []

        md = getattr(o, "metadata", {}) or {}

        if md.get("display_qty", None) is None:
            return []

        if not hasattr(o, "_visible_left"):
            _visible_remaining(o)

        add = float(getattr(o, "_pending_replenish", 0.0) or 0.0)
        if add <= 0.0:
            return []

        o._visible_left = float(getattr(o, "_visible_left", 0.0)) + add
        o._reserve_left = max(0.0, float(getattr(o, "_reserve_left", 0.0)) - add)
        o._pending_replenish = 0.0

        if self._is_crossed_unlocked():
            return self._repair_crossed_book_unlocked()

        return []

    # ── resting / matching ────────────────────────────────────

    def _rest_limit_order_unlocked(self, order: Order) -> None:
        """
        Ставит НЕ-маркетабельный остаток лимитки в книгу.
        """
        if order.price is None or not order.is_active():
            return

        order.price = quant(order.price)

        book = self.bids if order.side == OrderSide.BID else self.asks
        self._add_price_level(order.side, order.price)
        book[order.price].add_order(order)

        self._track_ttl_order(order)

    def _match_limit_order_unlocked(self, limit_order: Order) -> List[Dict[str, Any]]:
        """
        Маркетабельная лимитка:
        - не кладётся в книгу до матчинга;
        - бьёт противоположную сторону только до своей limit price;
        - остаток становится resting limit.
        """
        trades: List[Dict[str, Any]] = []

        if limit_order.price is None:
            return []

        limit_order.price = quant(limit_order.price)

        if limit_order.side == OrderSide.BID:
            opposite_book = self.asks
            price_getter = self._best_ask_price_unlocked
            resting_side = OrderSide.ASK
            taker_side = 'buy'

            def can_cross(px: Optional[float]) -> bool:
                return px is not None and px <= limit_order.price

        else:
            opposite_book = self.bids
            price_getter = self._best_bid_price_unlocked
            resting_side = OrderSide.BID
            taker_side = 'sell'

            def can_cross(px: Optional[float]) -> bool:
                return px is not None and px >= limit_order.price

        while limit_order.is_active():
            best_price = price_getter()

            if best_price is None or not can_cross(best_price):
                break

            level = opposite_book.get(best_price)
            if level is None:
                self._remove_price_level_if_empty(resting_side, best_price)
                continue

            level.remove_filled_or_inactive_orders()

            if not level:
                self._remove_price_level_if_empty(resting_side, best_price)
                continue

            resting_order: Optional[Order] = None
            for o in level.orders:
                if o.is_active() and _visible_remaining(o) > 0.0:
                    resting_order = o
                    break

            if resting_order is None:
                self._remove_price_level_if_empty(resting_side, best_price)
                continue

            vis = _visible_remaining(resting_order)
            trade_qty = min(limit_order.remaining_volume(), vis)

            if trade_qty <= 0.0:
                self._remove_price_level_if_empty(resting_side, best_price)
                continue

            limit_order.fill(trade_qty)
            resting_order.fill(trade_qty)
            _consume_visible(resting_order, trade_qty)

            ts = time.time()

            trade = {
                'price': best_price,
                'volume': trade_qty,
                'buy_order_id': limit_order.order_id if taker_side == 'buy' else resting_order.order_id,
                'sell_order_id': resting_order.order_id if taker_side == 'buy' else limit_order.order_id,
                'buy_agent': limit_order.agent_id if taker_side == 'buy' else resting_order.agent_id,
                'sell_agent': resting_order.agent_id if taker_side == 'buy' else limit_order.agent_id,
                'timestamp': ts,
                'taker_side': taker_side,
            }

            self._trade_seq += 1
            trade['trade_id'] = self._trade_seq
            trade['taker'] = limit_order.agent_id
            trade['maker'] = resting_order.agent_id
            trade['taker_order_id'] = limit_order.order_id
            trade['maker_order_id'] = resting_order.order_id

            trades.append(trade)
            self.trade_history.append(trade)
            self.last_trade_price = best_price

            _schedule_replenish_if_needed(self, resting_order, now_ms=int(ts * 1000))

            self._unindex_if_inactive(resting_order)

            level.remove_filled_or_inactive_orders()
            if not level:
                self._remove_price_level_if_empty(resting_side, best_price)

        if limit_order.is_active() and limit_order.remaining_volume() > 1e-12:
            self._rest_limit_order_unlocked(limit_order)
        else:
            self.orders.pop(limit_order.order_id, None)
            self._ttl_orders.discard(limit_order.order_id)

        return trades

    def _match_market_order_unlocked(self, market_order: Order) -> List[Dict[str, Any]]:
        trades: List[Dict[str, Any]] = []

        if market_order.side == OrderSide.BID:
            opposite_book = self.asks
            price_getter = self._best_ask_price_unlocked
            taker_side = 'buy'
            resting_side = OrderSide.ASK
        else:
            opposite_book = self.bids
            price_getter = self._best_bid_price_unlocked
            taker_side = 'sell'
            resting_side = OrderSide.BID

        qty_accum = 0.0
        val_accum = 0.0

        while market_order.is_active():
            best_price = price_getter()
            if best_price is None:
                break

            level = opposite_book.get(best_price)
            if level is None:
                self._remove_price_level_if_empty(resting_side, best_price)
                continue

            level.remove_filled_or_inactive_orders()

            if not level:
                self._remove_price_level_if_empty(resting_side, best_price)
                continue

            resting_order: Optional[Order] = None
            for o in level.orders:
                if o.is_active() and _visible_remaining(o) > 0.0:
                    resting_order = o
                    break

            if resting_order is None:
                self._remove_price_level_if_empty(resting_side, best_price)
                continue

            vis = _visible_remaining(resting_order)
            trade_qty = min(market_order.remaining_volume(), vis)

            if trade_qty <= 0.0:
                self._remove_price_level_if_empty(resting_side, best_price)
                continue

            market_order.fill(trade_qty)
            resting_order.fill(trade_qty)
            _consume_visible(resting_order, trade_qty)

            ts = time.time()

            trade = {
                'price': best_price,
                'volume': trade_qty,
                'buy_order_id': market_order.order_id if taker_side == 'buy' else resting_order.order_id,
                'sell_order_id': resting_order.order_id if taker_side == 'buy' else market_order.order_id,
                'buy_agent': market_order.agent_id if taker_side == 'buy' else resting_order.agent_id,
                'sell_agent': resting_order.agent_id if taker_side == 'buy' else market_order.agent_id,
                'timestamp': ts,
                'taker_side': taker_side,
            }

            self._trade_seq += 1
            trade['trade_id'] = self._trade_seq
            trade['taker'] = market_order.agent_id
            trade['maker'] = resting_order.agent_id
            trade['taker_order_id'] = market_order.order_id
            trade['maker_order_id'] = resting_order.order_id

            trades.append(trade)
            self.trade_history.append(trade)

            qty_accum += trade_qty
            val_accum += trade_qty * best_price
            self.last_trade_price = val_accum / max(qty_accum, 1e-12)

            _schedule_replenish_if_needed(self, resting_order, now_ms=int(ts * 1000))

            self._unindex_if_inactive(resting_order)

            level.remove_filled_or_inactive_orders()
            if not level:
                self._remove_price_level_if_empty(resting_side, best_price)

        if market_order.remaining_volume() > 1e-12:
            market_order.cancel()

        self.orders.pop(market_order.order_id, None)
        self._ttl_orders.discard(market_order.order_id)

        return trades

    def match(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._match_unlocked()

    def _match_unlocked(self) -> List[Dict[str, Any]]:
        """
        Аварийное кросс-лимит сведение, если книга всё-таки crossed.
        Нормальная маркетабельная лимитка должна идти через _match_limit_order_unlocked().
        """
        trades: List[Dict[str, Any]] = []

        while True:
            best_bid = self._best_bid_price_unlocked()
            best_ask = self._best_ask_price_unlocked()

            if best_bid is None or best_ask is None or best_bid < best_ask:
                break

            bid_level = self.bids.get(best_bid)
            ask_level = self.asks.get(best_ask)

            if bid_level is None:
                self._remove_price_level_if_empty(OrderSide.BID, best_bid)
                continue

            if ask_level is None:
                self._remove_price_level_if_empty(OrderSide.ASK, best_ask)
                continue

            bid_level.remove_filled_or_inactive_orders()
            ask_level.remove_filled_or_inactive_orders()

            if not bid_level or not ask_level:
                if not bid_level:
                    self._remove_price_level_if_empty(OrderSide.BID, best_bid)
                if not ask_level:
                    self._remove_price_level_if_empty(OrderSide.ASK, best_ask)
                continue

            bid_order: Optional[Order] = None
            for o in bid_level.orders:
                if o.is_active() and _visible_remaining(o) > 0.0:
                    bid_order = o
                    break

            ask_order: Optional[Order] = None
            for o in ask_level.orders:
                if o.is_active() and _visible_remaining(o) > 0.0:
                    ask_order = o
                    break

            if bid_order is None or ask_order is None:
                if bid_order is None:
                    self._remove_price_level_if_empty(OrderSide.BID, best_bid)
                if ask_order is None:
                    self._remove_price_level_if_empty(OrderSide.ASK, best_ask)
                continue

            trade_qty = min(_visible_remaining(bid_order), _visible_remaining(ask_order))

            if trade_qty <= 0.0:
                if _visible_remaining(bid_order) <= 0.0:
                    self._remove_price_level_if_empty(OrderSide.BID, best_bid)
                if _visible_remaining(ask_order) <= 0.0:
                    self._remove_price_level_if_empty(OrderSide.ASK, best_ask)
                continue

            ts_bid = float(getattr(bid_order, 'timestamp', 0.0) or 0.0)
            ts_ask = float(getattr(ask_order, 'timestamp', 0.0) or 0.0)

            trade_price = ask_order.price if ts_ask <= ts_bid else bid_order.price

            bid_order.fill(trade_qty)
            ask_order.fill(trade_qty)

            _consume_visible(bid_order, trade_qty)
            _consume_visible(ask_order, trade_qty)

            ts = time.time()

            trade = {
                'price': trade_price,
                'volume': trade_qty,
                'buy_order_id': bid_order.order_id,
                'sell_order_id': ask_order.order_id,
                'buy_agent': bid_order.agent_id,
                'sell_agent': ask_order.agent_id,
                'timestamp': ts,
                'taker_side': 'buy' if ts_bid > ts_ask else 'sell',
            }

            self._trade_seq += 1
            trade['trade_id'] = self._trade_seq

            if ts_bid > ts_ask:
                trade['taker'] = bid_order.agent_id
                trade['maker'] = ask_order.agent_id
                trade['taker_order_id'] = bid_order.order_id
                trade['maker_order_id'] = ask_order.order_id
            else:
                trade['taker'] = ask_order.agent_id
                trade['maker'] = bid_order.agent_id
                trade['taker_order_id'] = ask_order.order_id
                trade['maker_order_id'] = bid_order.order_id

            trades.append(trade)
            self.trade_history.append(trade)
            self.last_trade_price = trade_price

            _schedule_replenish_if_needed(self, bid_order, int(ts * 1000))
            _schedule_replenish_if_needed(self, ask_order, int(ts * 1000))

            self._unindex_if_inactive(bid_order)
            self._unindex_if_inactive(ask_order)

            bid_level.remove_filled_or_inactive_orders()
            ask_level.remove_filled_or_inactive_orders()

            if not bid_level:
                self._remove_price_level_if_empty(OrderSide.BID, best_bid)
            if not ask_level:
                self._remove_price_level_if_empty(OrderSide.ASK, best_ask)

        return trades

    # ── invariant protection ──────────────────────────────────

    def is_crossed(self) -> bool:
        with self._lock:
            return self._is_crossed_unlocked()

    def _is_crossed_unlocked(self) -> bool:
        bb = self._best_bid_price_unlocked()
        ba = self._best_ask_price_unlocked()
        return bb is not None and ba is not None and bb >= ba

    def repair_crossed_book(self, max_rounds: int = 1000) -> List[Dict[str, Any]]:
        with self._lock:
            return self._repair_crossed_book_unlocked(max_rounds=max_rounds)

    def _repair_crossed_book_unlocked(self, max_rounds: int = 1000) -> List[Dict[str, Any]]:
        all_trades: List[Dict[str, Any]] = []

        rounds = 0
        while rounds < max_rounds and self._is_crossed_unlocked():
            trades = self._match_unlocked()

            if not trades:
                bb = self._best_bid_price_unlocked()
                ba = self._best_ask_price_unlocked()

                if bb is not None:
                    self._remove_price_level_if_empty(OrderSide.BID, bb)
                if ba is not None:
                    self._remove_price_level_if_empty(OrderSide.ASK, ba)

                if self._is_crossed_unlocked():
                    break

                continue

            all_trades.extend(trades)
            rounds += 1

        return all_trades

    def tick(self):
        with self._lock:
            return self._tick_unlocked()

    def _tick_unlocked(self):
        now_ms = int(time.time() * 1000)

        trades = self._step_unlocked(now_ms, max_events_per_tick=10000)

        if self._last_legacy_ttl_ms <= 0:
            self._last_legacy_ttl_ms = now_ms

        elapsed = now_ms - self._last_legacy_ttl_ms
        ttl_steps = 0

        if elapsed >= self._legacy_ttl_interval_ms:
            ttl_steps = max(1, elapsed // self._legacy_ttl_interval_ms)
            ttl_steps = min(int(ttl_steps), 10)
            self._last_legacy_ttl_ms += ttl_steps * self._legacy_ttl_interval_ms

        if ttl_steps > 0:
            to_cancel: List[str] = []

            for oid in list(self._ttl_orders):
                order = self.orders.get(oid)

                if not order or (not order.is_active()) or getattr(order, "ttl", None) is None:
                    self._ttl_orders.discard(oid)
                    continue

                order.ttl -= ttl_steps

                if order.ttl <= 0:
                    to_cancel.append(oid)
                    self._ttl_orders.discard(oid)
                    continue

            for oid in to_cancel:
                self._cancel_order_unlocked(oid)

        if self._is_crossed_unlocked():
            trades += self._repair_crossed_book_unlocked()

        self._maybe_housekeep(now_ms)

        return trades

    # ── snapshots / telemetry ─────────────────────────────────

    def get_order_book_snapshot(self, depth: int = 10) -> Dict[str, List[Dict[str, Any]]]:
        with self._lock:
            return self._get_order_book_snapshot_unlocked(depth)

    def _get_order_book_snapshot_unlocked(self, depth: int = 10) -> Dict[str, List[Dict[str, Any]]]:
        depth = max(1, int(depth))

        bids_snapshot: List[Dict[str, Any]] = []
        asks_snapshot: List[Dict[str, Any]] = []

        i = len(self._bid_price_index) - 1
        while i >= 0 and len(bids_snapshot) < depth:
            p = self._bid_price_index[i]
            level = self.bids.get(p)

            if level is None:
                self._bid_price_index.pop(i)
                i -= 1
                continue

            level.remove_filled_or_inactive_orders()

            if not level:
                self.bids.pop(p, None)
                self._bid_price_index.pop(i)
                i -= 1
                continue

            vol = level.total_volume_visible()

            if vol > 0.0:
                bids_snapshot.append({
                    'price': p,
                    'volume': vol,
                    'source': getattr(level.orders[0], 'agent_id', None) if level.orders else None,
                })

            i -= 1

        i = 0
        while i < len(self._ask_price_index) and len(asks_snapshot) < depth:
            p = self._ask_price_index[i]
            level = self.asks.get(p)

            if level is None:
                self._ask_price_index.pop(i)
                continue

            level.remove_filled_or_inactive_orders()

            if not level:
                self.asks.pop(p, None)
                self._ask_price_index.pop(i)
                continue

            vol = level.total_volume_visible()

            if vol > 0.0:
                asks_snapshot.append({
                    'price': p,
                    'volume': vol,
                    'source': getattr(level.orders[0], 'agent_id', None) if level.orders else None,
                })

            i += 1

        return {'bids': bids_snapshot, 'asks': asks_snapshot}

    def __repr__(self):
        with self._lock:
            return (
                f"OrderBook(bids={len(self.bids)}, asks={len(self.asks)}, "
                f"orders={len(self.orders)}, last_trade_price={self.last_trade_price})"
            )

    def _best_bid_size(self) -> float:
        with self._lock:
            p = self._best_bid_price_unlocked()

            if p is None:
                return 0.0

            level = self.bids.get(p)

            if level is None:
                return 0.0

            return float(level.total_volume_visible())

    def _best_ask_size(self) -> float:
        with self._lock:
            p = self._best_ask_price_unlocked()

            if p is None:
                return 0.0

            level = self.asks.get(p)

            if level is None:
                return 0.0

            return float(level.total_volume_visible())