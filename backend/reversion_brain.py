import uuid
import time
import math
import random
from collections import deque
from typing import List, Dict, Any, Optional

from order import Order, OrderSide, OrderType, TICK


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _safe_log(x: float) -> float:
    return math.log(max(x, 1e-12))


class ReversionBrain:
    """
    LP-стильный контртрендовый фонд (V5, вариант C + активные контр-маркеты).

    Идея:
    - Крупный пассивно-активный LP, который:
        * выставляет плотные стенки против перетянутого тренда,
        * локально врезается маркетами против импульса, когда он ослабевает,
        * режет инвентарь при выносе,
        * частично фиксирует профит на откатах.
    - Интерфейс совместим с предыдущим ReversionBrain.
    """

    def __init__(self, agent_id: str, capital: float):
        self.agent_id = agent_id
        self.capital = float(capital)

        # позиция и PnL
        self.inventory: float = 0.0
        self.avg_entry: Optional[float] = None
        self.realized_pnl: float = 0.0

        # риск
        self.max_risk_frac: float = 0.45  # максимум notional vs capital
        self.daily_dd_limit_frac: float = 0.10
        self.risk_state: str = "normal"  # normal / reduced / flat
        self.pnl_ewma: float = 0.0
        self.dd_ewma: float = 0.0

        # лента
        self.trade_window: deque = deque(maxlen=4000)

        # режим рынка
        self.regime: str = "neutral"
        self.last_regime_change_ts: float = time.time()

        # LP-кампания (стенка)
        self.wall_active: bool = False
        self.wall_dir: Optional[int] = None  # +1 → sell-wall (контра ап-тренду), -1 → buy-wall
        self.wall_start_ts: float = 0.0
        self.wall_mid_ref: Optional[float] = None
        self.wall_strength: float = 0.0  # 0..1
        self.last_wall_refresh_ts: float = 0.0

        # лимит частоты действий
        self.last_action_ts: float = 0.0
        self.min_step_interval: float = 1.2

        # память mid
        self.last_mid: Optional[float] = None
        self.last_mid_ts: float = time.time()

        # мета ордеров
        self._order_meta: Dict[str, Dict[str, Any]] = {}

    # ----------------- PnL utils -----------------

    def _current_notional(self, mid: Optional[float]) -> float:
        if mid is None or self.inventory == 0:
            return 0.0
        return abs(self.inventory) * mid

    def _unrealized_pnl(self, mid: Optional[float]) -> float:
        if mid is None or self.avg_entry is None or self.inventory == 0:
            return 0.0
        if self.inventory > 0:
            return (mid - self.avg_entry) * self.inventory
        else:
            return (self.avg_entry - mid) * (-self.inventory)

    # ----------------- tape / features -----------------

    def _update_trades(self, order_book) -> None:
        # подмешиваем последние сделки
        for t in list(order_book.trade_history)[-256:]:
            self.trade_window.append(t)

    def _window_stats(self, now: float, horizon: float) -> Dict[str, float]:
        buy = sell = 0.0
        prices: List[float] = []
        for t in reversed(self.trade_window):
            ts = float(t.get("timestamp", now))
            if now - ts > horizon:
                break
            price = float(t["price"])
            vol = float(t["volume"])
            side = t.get("aggressor_side")
            notional = price * vol
            prices.append(price)
            if side == "buy":
                buy += notional
            elif side == "sell":
                sell += notional

        if not prices:
            return {
                "buy": 0.0,
                "sell": 0.0,
                "net": 0.0,
                "total": 0.0,
                "dom_frac": 0.0,
                "dir": 0,
                "price_change": 0.0,
                "price_range": 0.0,
            }

        net = buy - sell
        total = buy + sell
        if total <= 0:
            dom_frac = 0.0
            direction = 0
        else:
            dom_frac = net / total
            direction = 1 if dom_frac > 0.12 else -1 if dom_frac < -0.12 else 0

        first = prices[-1]
        last = prices[0]
        price_change = last - first
        price_range = max(prices) - min(prices)

        return {
            "buy": buy,
            "sell": sell,
            "net": net,
            "total": total,
            "dom_frac": dom_frac,
            "dir": direction,
            "price_change": price_change,
            "price_range": price_range,
        }

    def _extract_features(self, order_book) -> Dict[str, Any]:
        snapshot = order_book.get_order_book_snapshot(depth=5)
        bids = snapshot["bids"]
        asks = snapshot["asks"]

        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None

        if best_bid is not None and best_ask is not None:
            mid = 0.5 * (best_bid + best_ask)
            spread = best_ask - best_bid
        elif best_bid is not None:
            mid = best_bid
            spread = TICK
        elif best_ask is not None:
            mid = best_ask
            spread = TICK
        else:
            mid = None
            spread = 0.0

        now = time.time()

        # волатильность по mid
        if self.last_mid is not None and mid is not None:
            dt = max(now - self.last_mid_ts, 1e-3)
            ret = _safe_log(mid / self.last_mid)
            inst_vol = abs(ret) / math.sqrt(dt)
        else:
            inst_vol = 0.0
        if mid is not None:
            self.last_mid = mid
            self.last_mid_ts = now

        # окна потока
        stats_1s = self._window_stats(now, 1.0)
        stats_5s = self._window_stats(now, 5.0)
        stats_30s = self._window_stats(now, 30.0)
        stats_60s = self._window_stats(now, 60.0)

        def side_liq(levels):
            return sum(float(l["volume"]) for l in levels[:3])

        bid_liq = side_liq(bids)
        ask_liq = side_liq(asks)

        return {
            "mid": mid,
            "spread": spread,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "inst_vol": inst_vol,
            "stats_1s": stats_1s,
            "stats_5s": stats_5s,
            "stats_30s": stats_30s,
            "stats_60s": stats_60s,
            "bid_liq": bid_liq,
            "ask_liq": ask_liq,
        }

    # ----------------- regime / risk -----------------

    def _detect_regime(self, feat: Dict[str, Any]) -> str:
        mid = feat["mid"]
        if mid is None:
            return "neutral"

        vol = feat["inst_vol"]
        s60 = feat["stats_60s"]
        move60 = abs(s60["price_change"])
        trend_strength = abs(s60["dom_frac"])

        # CALIB: EUR sim vol/min ~0.57 price units = 57 TICK.
        # Old flat threshold 4 TICK and trend 10 TICK never fired.
        # New thresholds scaled to EUR: flat<20T, trend>25T, extended>45T.
        if vol < 0.00003 and move60 < 20 * TICK:    # CALIB: was 4 TICK
            regime = "flat"
        elif trend_strength < 0.2 and move60 < 25 * TICK:   # CALIB: was 10 TICK
            regime = "choppy"
        elif trend_strength >= 0.25 and move60 >= 25 * TICK:  # CALIB: was 10 TICK
            regime = "trend"
        elif move60 >= 45 * TICK:    # CALIB: was 18 TICK
            regime = "extended"
        else:
            regime = "neutral"

        now = time.time()
        if regime != self.regime and (now - self.last_regime_change_ts) < 10.0:
            regime = self.regime
        return regime

    def _update_risk_state(self, mid: Optional[float]) -> None:
        unreal = self._unrealized_pnl(mid)
        total_pnl = self.realized_pnl + unreal
        self.pnl_ewma = 0.98 * self.pnl_ewma + 0.02 * total_pnl

        dd = self.pnl_ewma - total_pnl
        self.dd_ewma = 0.98 * self.dd_ewma + 0.02 * dd

        cap = max(self.capital, 1e-9)
        dd_frac = self.dd_ewma / cap

        if dd_frac > self.daily_dd_limit_frac:
            self.risk_state = "flat"
        elif dd_frac > 0.5 * self.daily_dd_limit_frac:
            self.risk_state = "reduced"
        else:
            self.risk_state = "normal"

    def _max_notional_allowed(self) -> float:
        base = self.capital * self.max_risk_frac
        if self.risk_state == "normal":
            return base
        elif self.risk_state == "reduced":
            return base * 0.6
        else:
            return base * 0.2

    # ----------------- LP сигналы -----------------

    def _extended_trend_signal(self, feat: Dict[str, Any]) -> (Optional[int], float):
        """
        Определяем, когда рынок «перетянут» и нужен LP-противовес.

        Возвращает (dir, strength):
          dir = +1 → ставим SELL-стенку (контра ап-тренду),
          dir = -1 → BUY-стенка (контра даун-тренду).
        """
        mid = feat["mid"]
        if mid is None:
            return None, 0.0

        s30 = feat["stats_30s"]
        s60 = feat["stats_60s"]

        side60 = s60["dir"]
        dom60 = abs(s60["dom_frac"])
        move60 = s60["price_change"]
        move60_ticks = abs(move60) / max(TICK, 1e-9)

        side30 = s30["dir"]
        move30 = s30["price_change"]
        move30_ticks = abs(move30) / max(TICK, 1e-9)

        if side60 == 0 or side30 == 0 or side60 != side30:
            return None, 0.0

        if dom60 < 0.25:
            return None, 0.0

        # CALIB: Original thresholds were tuned for old sim where 1m range = 14 ticks.
        # Target 1m range = 57 ticks → lower thresholds so brain activates at proper moments.
        if move60_ticks < 5.0 or move30_ticks < 3.0:   # CALIB: was 10.0 / 6.0
            return None, 0.0

        raw_strength = 0.6 * _clip(dom60 / 0.6, 0.0, 1.5) + 0.4 * _clip(move60_ticks / 25.0, 0.0, 1.5)
        strength = _clip(raw_strength, 0.0, 1.0)

        direction = +1 if move60 > 0 else -1
        return direction, strength

    def _should_start_wall(self, now: float, feat: Dict[str, Any]) -> (Optional[int], float):
        if self.risk_state == "flat":
            return None, 0.0

        # cooldown между кампаниями LP
        if self.wall_active and (now - self.wall_start_ts) < 20.0:
            return None, 0.0

        # если недавно сильно выносило по PnL — не лезем
        unreal = self._unrealized_pnl(feat["mid"])
        if unreal < -0.03 * self.capital:
            return None, 0.0

        dir_ext, strength = self._extended_trend_signal(feat)
        if dir_ext is None or strength < 0.35:
            return None, 0.0

        return dir_ext, strength

    # ----------------- LP wall поведение -----------------

    def _spawn_wall_orders(self, now: float, feat: Dict[str, Any]) -> List[Order]:
        """
        Выставляем «лестницу» лимиток около рынка против тренда.
        Почти без маркетов.
        """
        orders: List[Order] = []
        mid = feat["mid"]
        best_bid = feat["best_bid"]
        best_ask = feat["best_ask"]
        if mid is None or self.wall_dir is None:
            return orders

        max_notional = self._max_notional_allowed()
        cur_notional = self._current_notional(mid)
        remaining = max_notional - cur_notional
        if remaining <= mid:  # меньше 1 контракта
            return orders

        # размер стенки 1 шагом
        base_frac = 0.02 + 0.06 * self.wall_strength  # 2–8% капитала за волну
        if self.risk_state == "reduced":
            base_frac *= 0.6
        wave_notional = min(self.capital * base_frac, remaining)
        qty_total = max(1.0, wave_notional / mid)

        levels = random.randint(3, 6)
        qty_per_level = qty_total / levels

        # SELL-стенка (контра ап-тренду)
        if self.wall_dir == +1:
            side = OrderSide.ASK
            ref = best_ask if best_ask is not None else mid
            for i in range(levels):
                step_ticks = 0 if i == 0 else random.randint(1, 3 + i)
                price = ref + step_ticks * TICK
                ttl = random.randint(10, 35)
                vol = float(qty_per_level * (1.1 if i == 0 else 0.8 + 0.4 * random.random()))
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=vol,
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=ttl,
                )
                orders.append(o)
                self._order_meta[o.order_id] = {"phase": "wall_sell", "lvl": i}

        # BUY-стенка (контра даун-тренду)
        else:
            side = OrderSide.BID
            ref = best_bid if best_bid is not None else mid
            for i in range(levels):
                step_ticks = 0 if i == 0 else random.randint(1, 3 + i)
                price = ref - step_ticks * TICK
                ttl = random.randint(10, 35)
                vol = float(qty_per_level * (1.1 if i == 0 else 0.8 + 0.4 * random.random()))
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=vol,
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=ttl,
                )
                orders.append(o)
                self._order_meta[o.order_id] = {"phase": "wall_buy", "lvl": i}

        self.last_wall_refresh_ts = now
        return orders

    # ----------------- активные контр-маркеты -----------------

    def _counter_mkt_pulse(self, now: float, feat: Dict[str, Any]) -> List[Order]:
        """
        Локальный удар маркетами ПРОТИВ тренда, когда импульс ослабевает.
        Размер маленький (0.2–1% капитала).
        """
        orders: List[Order] = []
        mid = feat["mid"]
        if mid is None or not self.wall_active or self.wall_dir is None:
            return orders

        # направление тренда для контры:
        # wall_dir = +1 → ап-тренд → шортим маркетом (ASK)
        # wall_dir = -1 → даун-тренд → лонгим маркетом (BID)
        dir_wall = self.wall_dir

        s1 = feat["stats_1s"]
        dom1 = abs(s1["dom_frac"])
        side1 = s1["dir"]

        # импульс ослаб: поток неуверенный / разваливается
        weakened = (side1 == 0) or (dom1 < 0.12)

        # цена отклонилась от точки старта кампании
        wall_ref = self.wall_mid_ref if self.wall_mid_ref is not None else mid
        price_deviation_ticks = abs(mid - wall_ref) / max(TICK, 1e-9)

        # нужна тренд/extended фаза, чтобы вообще думать о таком ударе
        if self.regime not in ("trend", "extended"):
            return orders

        # не чаще, чем раз в несколько секунд
        # (ограничивается общим min_step_interval + условиями)
        if not weakened or price_deviation_ticks <= 4.0:
            return orders

        max_notional = self._max_notional_allowed()
        cur_notional = self._current_notional(mid)
        if cur_notional >= 0.9 * max_notional:
            return orders

        frac = 0.002 + 0.008 * random.random()  # 0.2–1% капитала
        notional = self.capital * frac
        if notional < mid:
            return orders

        qty = max(1.0, notional / mid)
        side = OrderSide.ASK if dir_wall == +1 else OrderSide.BID

        o = Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            volume=float(qty),
            price=None,
            order_type=OrderType.MARKET,
            ttl=None,
        )
        orders.append(o)
        self._order_meta[o.order_id] = {"phase": "counter_mkt"}

        return orders

    # ----------------- inventory / exit -----------------

    def _manage_inventory_and_exit(self, now: float, feat: Dict[str, Any]) -> List[Order]:
        """
        LP-стильный выход:
        - частичный профит на откате,
        - стоп при выносе.
        """
        orders: List[Order] = []
        mid = feat["mid"]
        if mid is None or self.inventory == 0:
            return orders

        cur_notional = self._current_notional(mid)
        unreal = self._unrealized_pnl(mid)

        # стоп при сильном выносе
        max_notional = self._max_notional_allowed()
        hard_loss = -0.06 * self.capital  # 6% капа
        soft_loss = -0.03 * self.capital

        if unreal < hard_loss or (cur_notional > 0.8 * max_notional and unreal < soft_loss):
            cut_frac = 0.5 if unreal < hard_loss else 0.3
            qty = abs(self.inventory) * cut_frac
            side = OrderSide.BID if self.inventory < 0 else OrderSide.ASK
            o = Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=side,
                volume=float(qty),
                price=None,
                order_type=OrderType.MARKET,
                ttl=None,
            )
            orders.append(o)
            self._order_meta[o.order_id] = {"phase": "stop_cut"}
            return orders

        # профит-скейлинг на откате
        s5 = feat["stats_5s"]
        move5 = s5["price_change"]
        move5_ticks = move5 / max(TICK, 1e-9)

        inv_dir = 1 if self.inventory > 0 else -1
        if (inv_dir > 0 and move5_ticks < -2.0) or (inv_dir < 0 and move5_ticks > 2.0):
            step_frac = 0.15 + 0.10 * random.random()
            qty_total = abs(self.inventory) * step_frac
            if qty_total * mid < 0.01 * self.capital:
                return orders

            mkt_part = 0.15 + 0.15 * random.random()
            mkt_qty = qty_total * mkt_part
            lim_qty = qty_total - mkt_qty

            if mkt_qty > 0.5:
                side = OrderSide.BID if self.inventory < 0 else OrderSide.ASK
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=float(mkt_qty),
                    price=None,
                    order_type=OrderType.MARKET,
                    ttl=None,
                )
                orders.append(o)
                self._order_meta[o.order_id] = {"phase": "take_profit_mkt"}

            best_bid = feat["best_bid"]
            best_ask = feat["best_ask"]
            if lim_qty > 0.5:
                if self.inventory > 0:
                    ref = best_ask if best_ask is not None else mid
                    price = ref + random.randint(0, 2) * TICK
                    side = OrderSide.ASK
                else:
                    ref = best_bid if best_bid is not None else mid
                    price = ref - random.randint(0, 2) * TICK
                    side = OrderSide.BID
                ttl = random.randint(10, 40)
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=float(lim_qty),
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=ttl,
                )
                orders.append(o)
                self._order_meta[o.order_id] = {"phase": "take_profit_lim"}

        return orders

    def _maybe_flatten_all(self, feat: Dict[str, Any]) -> List[Order]:
        """
        Полная ликвидация, когда риск в состоянии flat.
        """
        orders: List[Order] = []
        mid = feat["mid"]
        if mid is None or self.inventory == 0:
            return orders
        if self.risk_state != "flat":
            return orders

        qty = abs(self.inventory)
        side = OrderSide.BID if self.inventory < 0 else OrderSide.ASK
        o = Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            volume=float(qty),
            price=None,
            order_type=OrderType.MARKET,
            ttl=None,
        )
        orders.append(o)
        self._order_meta[o.order_id] = {"phase": "risk_flatten"}
        self.wall_active = False
        self.wall_dir = None
        return orders

    # ----------------- основной generate_orders -----------------

    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        now = time.time()
        if now - self.last_action_ts < self.min_step_interval:
            return []

        snapshot = order_book.get_order_book_snapshot(depth=1)
        bids = snapshot["bids"]
        asks = snapshot["asks"]
        if bids and asks:
            mid = 0.5 * (float(bids[0]["price"]) + float(asks[0]["price"]))
        elif bids:
            mid = float(bids[0]["price"])
        elif asks:
            mid = float(asks[0]["price"])
        else:
            return []

        # обновляем ленту и фичи
        self._update_trades(order_book)
        feat = self._extract_features(order_book)

        # режим и риск
        new_regime = self._detect_regime(feat)
        if new_regime != self.regime:
            self.regime = new_regime
            self.last_regime_change_ts = now

        self._update_risk_state(feat["mid"])

        orders: List[Order] = []

        # глобальный риск-флэт
        orders.extend(self._maybe_flatten_all(feat))
        if orders:
            self.last_action_ts = now
            return orders

        # рынок совсем мёртвый – LP не шевелится
        if feat["inst_vol"] < 0.00002 and self.regime in ("flat", "choppy"):
            return []

        # если стенка не активна — решаем, запускать ли новую
        if not self.wall_active:
            dir_wall, strength = self._should_start_wall(now, feat)
            if dir_wall is not None:
                self.wall_active = True
                self.wall_dir = dir_wall
                self.wall_start_ts = now
                self.wall_mid_ref = feat["mid"]
                self.wall_strength = strength
                self.last_wall_refresh_ts = 0.0  # сразу построить уровни

        # если стенка активна – периодически обновляем уровни
        if self.wall_active and (now - self.last_wall_refresh_ts) > 5.0:
            orders.extend(self._spawn_wall_orders(now, feat))

        # активные контр-маркеты против тренда
        orders.extend(self._counter_mkt_pulse(now, feat))

        # управление инвентарём: профит и стопы
        orders.extend(self._manage_inventory_and_exit(now, feat))

        # таймаут кампании
        if self.wall_active and (now - self.wall_start_ts) > 300.0:
            self.wall_active = False
            self.wall_dir = None
            self.wall_mid_ref = None
            self.wall_strength = 0.0

        if orders:
            self.last_action_ts = now
        return orders

    # ----------------- учёт сделок -----------------

    def on_order_filled(self, order_id: str, price: float, qty: float, side: OrderSide, slippage: float = 0.0) -> None:
        if qty <= 0:
            return

        if self.avg_entry is None:
            self.inventory = qty if side == OrderSide.BID else -qty
            self.avg_entry = price
        else:
            pos = self.inventory
            if side == OrderSide.BID:
                # покупка
                if pos >= 0:
                    new_pos = pos + qty
                    self.avg_entry = (self.avg_entry * pos + price * qty) / max(new_pos, 1e-9)
                    self.inventory = new_pos
                else:
                    # частичное закрытие шорта
                    closing = min(qty, -pos)
                    self.realized_pnl += (self.avg_entry - price) * closing
                    pos += closing
                    if qty > closing:
                        new_qty = qty - closing
                        self.inventory = new_qty
                        self.avg_entry = price
                    else:
                        self.inventory = pos
                        if pos == 0:
                            self.avg_entry = None
            else:
                # продажа
                if pos <= 0:
                    new_pos = pos - qty
                    total_notional = abs(self.avg_entry * pos) + price * qty
                    self.inventory = new_pos
                    self.avg_entry = total_notional / max(abs(new_pos), 1e-9)
                else:
                    # частичное закрытие лонга
                    closing = min(qty, pos)
                    self.realized_pnl += (price - self.avg_entry) * closing
                    pos -= closing
                    if qty > closing:
                        new_qty = qty - closing
                        self.inventory = -new_qty
                        self.avg_entry = price
                    else:
                        self.inventory = pos
                        if pos == 0:
                            self.avg_entry = None

        self._order_meta.pop(order_id, None)

    # ----------------- совместимость -----------------

    def perceive_market(self, market_context) -> str:
        if market_context is None or not getattr(market_context, "is_ready", None):
            return "neutral"
        try:
            if not market_context.is_ready():
                return "neutral"
            name = str(market_context.phase.name).lower()
            if "trend" in name:
                return "trend"
            if "volatile" in name or "panic" in name:
                return "high_vol"
            if "flat" in name or "calm" in name:
                return "flat"
        except Exception:
            return "neutral"
        return "neutral"

    def restore_capital(self) -> None:
        # заглушка под внешний reset риска
        pass