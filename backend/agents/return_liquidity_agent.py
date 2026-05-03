# return_liquidity_agent.py
#
# ReturnLiquidityAgent (event-driven):
# - НЕ котирует постоянно (не второй MM)
# - активируется только после "импульса" (по z-score mid-ретёрна)
# - делает краткосрочный рефилл (возврат ликвидности) + уходит в cooldown
# - вне активности: только чистит свои хвосты (CANCEL), не ставит новые лимиты
#
# Совместимо с твоим движком:
# - Order(order_id, agent_id, side, volume, price, order_type, ttl, metadata)
# - CANCEL реализован через OrderType.CANCEL и order_id = целевой oid (order_book.add_order планирует CANCEL событие)
# - ttl_ms поддерживается через metadata['ttl_ms'] (order_book.add_order планирует EXPIRE)  :contentReference[oaicite:0]{index=0}

import time
import random
import uuid
from collections import deque
from typing import Dict, Any, Optional, List, Tuple

from backend.core.order import Order, OrderSide, OrderType, quant


def _clip(x: float, a: float, b: float) -> float:
    return a if x < a else (b if x > b else x)


def _now(market_context=None) -> float:
    # если у market_context есть now() — используем, иначе time.time()
    try:
        if market_context is not None and hasattr(market_context, "now"):
            v = market_context.now()
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    except Exception:
        pass
    return time.time()


def _best_prices(order_book) -> Tuple[Optional[float], Optional[float]]:
    bb = getattr(order_book, "_best_bid_price", lambda: None)()
    ba = getattr(order_book, "_best_ask_price", lambda: None)()
    return bb, ba


def _mid(order_book) -> Optional[float]:
    bb, ba = _best_prices(order_book)
    if bb is None or ba is None:
        return None
    return 0.5 * (float(bb) + float(ba))


def _post_only_price(side: OrderSide, px: float, best_bid: float, best_ask: float, tick: float) -> float:
    # жёсткий post-only: не пересечь спред
    px = float(px)
    if side == OrderSide.BID:
        if px >= best_ask:
            px = best_ask - tick
    else:
        if px <= best_bid:
            px = best_bid + tick
    return float(quant(px))


class ReturnLiquidityAgent:
    """
    Назначение:
      - после импульса вернуть пассивную ликвидность (resilience), чтобы убрать перманентный импакт
      - НЕ держать цену в балансе и НЕ создавать "коридор" в спокойном рынке

    Ключевая логика:
      - event-driven gate:
          * impulse -> active window (коротко)
          * затем cooldown (молчит, только чистит хвосты)
          * в остальное время НЕ ставит новые лимиты

      - в активном окне ставит ОГРАНИЧЕННОЕ число лимитов:
          * near: 1 уровень на стороне fade (тормоз продолжения) + 1 уровень на стороне support (дать ретест)
          * far: 1 уровень в сторону anchor (куда рынок "должен" возвращаться), не в обе стороны
    """

    def __init__(
        self,
        agent_id: str,
        capital: float,
        tick_size: float = 0.01,
        lookback: int = 220,
    ):
        self.agent_id = str(agent_id)
        self.capital = float(capital)
        self.tick = float(tick_size)

        # Сервер уважает loop_interval, если есть  :contentReference[oaicite:1]{index=1}
        self.loop_interval = 0.45

        # ---- серии ----
        self._mids = deque(maxlen=int(lookback))
        self._rets = deque(maxlen=int(lookback))
        self._abs_rets = deque(maxlen=int(lookback))

        self.anchor_mid: Optional[float] = None
        self.last_mid: Optional[float] = None

        # ---- режим ----
        self._impulse_dir = 0
        self._impulse_active_until = 0.0

        # строгий gate
        self.active_until = 0.0
        self.cooldown_until = 0.0

        # ---- ордера агента ----
        # oid -> {side, price, ts}
        self.live_orders: Dict[str, Dict[str, Any]] = {}

        # ---- параметры детекта импульса ----
        self.impulse_z = 2.8
        self.impulse_hold_rng = (0.7, 1.7)      # сколько держать флаг импульса (для бустов)
        self.active_hold_rng = (0.6, 1.4)       # сколько агент реально ставит рефилл
        self.cooldown_rng = (2.0, 6.0)          # после активности — молчит

        # ---- параметры "баланса" (early-exit) ----
        # если mid близко к anchor и спред/вола низкие -> закрываемся и уходим в cooldown
        self.balance_band_spreads = 3.5
        self.min_spread_ticks_for_work = 2.2
        self.min_sigma_ticks_for_work = 1.1

        # ---- интенсивность котирования (в активном окне) ----
        # near: один уровень с каждой нужной стороны
        self.near_ticks = (2, 6)                # отступ от best
        # far: один уровень к anchor (структура возврата)
        self.far_ticks = (7, 20)

        # усиления только в импульсе (и умеренные)
        self.fade_boost = 1.18
        self.support_boost = 1.05

        # риск/объёмы
        self.max_notional_per_cycle_frac = 0.00045
        self.base_notional_frac = (0.00010, 0.00032)

        # TTL/latency (ttl_ms работает через metadata['ttl_ms'])  :contentReference[oaicite:2]{index=2}
        self.ttl_ms_near = (650, 1800)
        self.ttl_ms_far = (1200, 4200)
        self.latency_ms_rng = (10, 60)
        self.latency_jitter_ms_rng = (0, 25)

        # чистка/снятие мусора
        self.cleanup_dt = 0.65
        self._last_cleanup_ts = 0.0
        self.stale_ticks = 18

        # айсберги здесь ОТКЛЮЧЕНЫ намеренно: этот агент не должен "держать" цену
        self.use_icebergs = False

    # ---------------- stats ----------------

    def _update_series(self, mid: float):
        if self.last_mid is not None:
            r = float(mid) - float(self.last_mid)
            self._rets.append(r)
            self._abs_rets.append(abs(r))
        self._mids.append(float(mid))
        self.last_mid = float(mid)

        # anchor_mid: медленная оценка "куда возвращаться"
        if self.anchor_mid is None:
            self.anchor_mid = float(mid)
        else:
            self.anchor_mid = 0.9968 * float(self.anchor_mid) + 0.0032 * float(mid)

    def _sigma(self) -> float:
        # rob-ish: sigma ≈ 1.4826 * median(|ret|)
        if len(self._abs_rets) < 24:
            return 0.0
        xs = sorted(self._abs_rets)
        med = xs[len(xs) // 2]
        return float(1.4826 * med)

    def _impulse_check(self, now: float, best_bid: float, best_ask: float):
        if len(self._rets) < 24:
            return
        sig = self._sigma()
        if sig <= 1e-12:
            return

        r = float(self._rets[-1])
        z = abs(r) / max(sig, 1e-9)

        # импульс есть -> коротко держим флаг + направление
        if z >= self.impulse_z and best_bid is not None and best_ask is not None:
            self._impulse_dir = 1 if r > 0 else -1
            hold = random.uniform(*self.impulse_hold_rng)
            self._impulse_active_until = max(self._impulse_active_until, now + hold)

            # включаем активное окно рефилла (коротко)
            self.active_until = max(self.active_until, now + random.uniform(*self.active_hold_rng))
            self.cooldown_until = 0.0

    def _impulse_active(self, now: float) -> bool:
        return now < self._impulse_active_until

    def _is_balanced(self, mid: float, anchor: float, spread: float, sigma: float) -> bool:
        # dist в "спредах" + низкая вола + нормальный спред
        dist_spreads = abs(mid - anchor) / max(spread, self.tick)
        spread_ticks = spread / max(self.tick, 1e-9)
        sigma_ticks = sigma / max(self.tick, 1e-9)
        return (
            dist_spreads <= self.balance_band_spreads and
            spread_ticks <= self.min_spread_ticks_for_work and
            sigma_ticks <= self.min_sigma_ticks_for_work
        )

    # ---------------- order mgmt ----------------

    def _mk_cancel(self, oid: str) -> Order:
        # CANCEL: order_book.add_order() планирует EventKind.CANCEL по order_id  :contentReference[oaicite:3]{index=3}
        return Order(
            order_id=str(oid),
            agent_id=self.agent_id,
            side=OrderSide.BID,
            volume=0.0,
            price=None,
            order_type=OrderType.CANCEL,
            ttl=None,
            metadata={
                "latency_ms": random.randint(*self.latency_ms_rng),
                "latency_jitter_ms": random.randint(*self.latency_jitter_ms_rng),
            },
        )

    def _mk_limit(self, side: OrderSide, px: float, qty: float, ttl_ms: int, kind: str) -> Order:
        oid = str(uuid.uuid4())
        o = Order(
            order_id=oid,
            agent_id=self.agent_id,
            side=side,
            volume=float(qty),
            price=float(quant(px)),
            order_type=OrderType.LIMIT,
            ttl=None,
            metadata={
                "ttl_ms": int(ttl_ms),
                "latency_ms": random.randint(*self.latency_ms_rng),
                "latency_jitter_ms": random.randint(*self.latency_jitter_ms_rng),
                "kind": str(kind),
            },
        )
        self.live_orders[oid] = {"side": side, "price": float(o.price), "ts": time.time(), "kind": kind}
        return o

    def _should_cleanup(self, now: float) -> bool:
        return (now - self._last_cleanup_ts) >= self.cleanup_dt

    def _cleanup(self, now: float, best_bid: float, best_ask: float) -> List[Order]:
        self._last_cleanup_ts = now
        out: List[Order] = []

        # снимаем сильно устаревшие/далёкие
        to_cancel: List[str] = []
        for oid, info in list(self.live_orders.items()):
            px = float(info.get("price", 0.0))
            side = info.get("side", OrderSide.BID)
            age = now - float(info.get("ts", now))

            if side == OrderSide.BID:
                if (best_bid - px) > self.stale_ticks * self.tick:
                    to_cancel.append(oid)
            else:
                if (px - best_ask) > self.stale_ticks * self.tick:
                    to_cancel.append(oid)

            # старые ордера — иногда снимаем, чтобы не висели "пояса"
            if age > 6.0 and random.random() < 0.40:
                to_cancel.append(oid)

        # лимит cancel за цикл
        random.shuffle(to_cancel)
        for oid in to_cancel[:24]:
            out.append(self._mk_cancel(oid))
            self.live_orders.pop(oid, None)

        return out

    # ---------------- fills ----------------

    def on_order_filled(self, order_id: str, price: float, qty: float, side: OrderSide, slippage: float = 0.0):
        # server.notify_agent_fill вызывает on_order_filled по buy/sell агентам  :contentReference[oaicite:4]{index=4}
        self.live_orders.pop(str(order_id), None)

    def restore_capital(self):
        pass

    # ---------------- planning ----------------

    def _compute_qty(self, px: float, mult: float) -> float:
        px = max(float(px), self.tick)
        base_frac = random.uniform(*self.base_notional_frac)
        notional = self.capital * base_frac * mult
        q = notional / px
        return max(1.0, float(q))

    def _plan_orders(
        self,
        now: float,
        mid: float,
        anchor: float,
        best_bid: float,
        best_ask: float,
        sigma: float,
    ) -> List[Tuple[OrderSide, float, float, int, str]]:
        """
        Возвращает планы лимитов: (side, price, qty, ttl_ms, kind)
        """
        plans: List[Tuple[OrderSide, float, float, int, str]] = []

        spread = max(self.tick, float(best_ask - best_bid))
        impulse_on = self._impulse_active(now)

        # если импульс вверх: рынок "перелетел" вверх -> сверху ставим больше офферов (fade)
        # и чуть подкладываем бид снизу (support) для ретеста/отката.
        if self._impulse_dir > 0:
            fade_side = OrderSide.ASK
            sup_side = OrderSide.BID
        else:
            fade_side = OrderSide.BID
            sup_side = OrderSide.ASK

        # --- NEAR: ровно 2 заявки максимум (fade + support) ---
        t = random.randint(*self.near_ticks)

        # fade near
        if fade_side == OrderSide.ASK:
            px_fade = best_ask + t * self.tick
        else:
            px_fade = best_bid - t * self.tick

        px_fade = _post_only_price(fade_side, px_fade, best_bid, best_ask, self.tick)
        mult_fade = self.fade_boost if impulse_on else 1.0
        ttl_fade = random.randint(*self.ttl_ms_near)
        plans.append((fade_side, px_fade, self._compute_qty(px_fade, mult_fade), ttl_fade, "near_fade"))

        # support near (слабее)
        if sup_side == OrderSide.BID:
            px_sup = best_bid - max(1, t - 1) * self.tick
        else:
            px_sup = best_ask + max(1, t - 1) * self.tick

        px_sup = _post_only_price(sup_side, px_sup, best_bid, best_ask, self.tick)
        mult_sup = self.support_boost if impulse_on else 1.0
        ttl_sup = random.randint(*self.ttl_ms_near)
        plans.append((sup_side, px_sup, self._compute_qty(px_sup, mult_sup), ttl_sup, "near_support"))

        # --- FAR: ровно 1 уровень в сторону anchor (НЕ симметрия) ---
        tf = random.randint(*self.far_ticks)

        # если mid выше anchor -> "возврат" вниз: ставим far BID ближе к anchor
        # если mid ниже anchor -> "возврат" вверх: ставим far ASK ближе к anchor
        if mid > anchor:
            side_far = OrderSide.BID
            px_far = anchor - 0.45 * tf * self.tick - random.uniform(0.0, 2.0 * self.tick)
        else:
            side_far = OrderSide.ASK
            px_far = anchor + 0.45 * tf * self.tick + random.uniform(0.0, 2.0 * self.tick)

        px_far = _post_only_price(side_far, px_far, best_bid, best_ask, self.tick)
        ttl_far = random.randint(*self.ttl_ms_far)
        # far чуть меньше по мульту
        plans.append((side_far, px_far, self._compute_qty(px_far, 0.75), ttl_far, "far_anchor"))

        return plans

    # ---------------- main API ----------------

    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        now = _now(market_context)

        mid = _mid(order_book)
        if mid is None:
            return []

        best_bid, best_ask = _best_prices(order_book)
        if best_bid is None or best_ask is None:
            return []

        best_bid = float(best_bid)
        best_ask = float(best_ask)
        mid = float(mid)

        self._update_series(mid)
        sigma = self._sigma()
        self._impulse_check(now, best_bid, best_ask)

        spread = max(self.tick, best_ask - best_bid)
        anchor = float(self.anchor_mid if self.anchor_mid is not None else mid)

        out: List[Order] = []

        # периодическая чистка (всегда можно)
        if self._should_cleanup(now):
            out += self._cleanup(now, best_bid, best_ask)

        # ---- GATE / EXIT ----

        # cooldown: молчим, только чистим хвосты
        if now < self.cooldown_until:
            return out

        # если активное окно закончилось -> уходим
        if now >= self.active_until:
            # если рынок в балансе -> ставим cooldown и снимаем хвосты
            if self._is_balanced(mid, anchor, spread, sigma):
                self.cooldown_until = now + random.uniform(*self.cooldown_rng)
                out += self._cleanup(now, best_bid, best_ask)
                return out

            # если не balanced, но импульса нет -> НЕ котируем как MM
            # максимум: чистка хвостов (уже сделана выше)
            return out

        # ---- ACTIVE MODE: ставим ограниченный рефилл ----

        # риск-лимит на цикл
        max_notional = self.capital * self.max_notional_per_cycle_frac
        used = 0.0

        # не дублировать цены/стороны
        existing = set()
        for _, info in self.live_orders.items():
            try:
                existing.add((info["side"], float(info["price"])))
            except Exception:
                pass

        plans = self._plan_orders(now, mid, anchor, best_bid, best_ask, sigma)
        random.shuffle(plans)

        for side, px, qty, ttl_ms, kind in plans:
            px = float(px)
            qty = float(qty)

            key = (side, float(quant(px)))
            if key in existing:
                continue

            notional = px * qty
            if used + notional > max_notional and len(out) >= 1:
                break

            # финальный post-only контроль
            px = _post_only_price(side, px, best_bid, best_ask, self.tick)

            # страховка от пересечения
            if side == OrderSide.BID and px >= best_ask:
                continue
            if side == OrderSide.ASK and px <= best_bid:
                continue

            out.append(self._mk_limit(side, px, qty, ttl_ms, kind))
            used += notional
            existing.add((side, float(quant(px))))

        return out
