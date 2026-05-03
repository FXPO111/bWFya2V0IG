# strategist_agent.py
# InstitutionalExecutor v5 — институт с "дыханием" ликвидности и устойчивым к grid-эффекту.
# Цели:
#   - неровное движение, без runaway-трендов
#   - постоянные мелкие откаты, но без "зажатия"
#   - отсутствие деградации в микрогрид на длинных отрезках
#   - самодостаточный рынок: тренд + ликвидность + контртренд + шум

import uuid
import random
import time
import math
from collections import deque
from typing import List, Dict, Any, Optional

import numpy as np

from order import Order, OrderSide, OrderType, TICK

def _ctx_now(ctx):
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()

def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x

def _post_only_price(side: OrderSide, price: float, best_bid: float | None, best_ask: float | None) -> float:
    if best_bid is None or best_ask is None:
        return price
    if side == OrderSide.BID:
        return min(price, best_ask - TICK)
    else:
        return max(price, best_bid + TICK)

# ============================================================
# ВСПОМОГАТЕЛЬНОЕ: фичи из ордербука
# ============================================================

def _extract_features(order_book, trade_history, capital, now=None):
    snapshot = order_book.get_order_book_snapshot(depth=3)
    bids = snapshot["bids"]
    asks = snapshot["asks"]

    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None

    if best_bid is not None and best_ask is not None:
        mid = 0.5 * (best_bid + best_ask)
        spread = best_ask - best_bid
    else:
        mid = None
        spread = 0.0

    if now is None:
        now = time.time()
    trades = [t for t in trade_history if now - float(t.get("timestamp", now)) <= 120.0]

    prices = [float(t["price"]) for t in trades] if trades else []
    vols = [float(t["volume"]) for t in trades] if trades else []

    if len(prices) >= 5:
        arr = np.maximum(1e-9, np.array(prices))
        rets = np.diff(np.log(arr))
        sigma = float(np.std(rets))
    else:
        sigma = 0.0

    if len(prices) >= 30:
        trend = math.log(prices[-1] / max(1e-9, prices[0]))
    else:
        trend = 0.0

    if len(prices) >= 2:
        last_ret = math.log(prices[-1] / max(1e-9, prices[-2]))
        if last_ret > 0:
            last_dir = 1
        elif last_ret < 0:
            last_dir = -1
        else:
            last_dir = 0
    else:
        last_ret = 0.0
        last_dir = 0

    turnover = float(sum(v * p for v, p in zip(vols, prices))) if trades else 0.0
    dt = 120.0
    turnover_per_sec = turnover / dt if dt > 0 else 0.0

    near_bid_notional = 0.0
    near_ask_notional = 0.0
    for b in bids[:3]:
        p = float(b["price"])
        v = float(b["volume"])
        near_bid_notional += p * v
    for a in asks[:3]:
        p = float(a["price"])
        v = float(a["volume"])
        near_ask_notional += p * v

    near_total = near_bid_notional + near_ask_notional
    cap = max(capital, 1e-9)
    near_liq_ratio = near_total / cap
    near_liq_ratio_bid = near_bid_notional / cap
    near_liq_ratio_ask = near_ask_notional / cap

    return {
        "mid": mid,
        "spread": spread,
        "sigma": sigma,
        "trend": trend,
        "last_ret": last_ret,
        "last_dir": last_dir,
        "turnover_per_sec": turnover_per_sec,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "near_liq_ratio": near_liq_ratio,
        "near_liq_ratio_bid": near_liq_ratio_bid,
        "near_liq_ratio_ask": near_liq_ratio_ask,
    }


# ============================================================
# ДЕСКИ ВНУТРИ ИНСТИТУТА
# ============================================================

class DirectionalDesk:
    """
    Слабый направленный поток + микроволны + фазовый TP.

    Добавлено:
      - трекинг позиции и средней цены
      - unrealized PnL
      - TP-фаза (10–40 сек): плавный scale-out против текущей позиции
        без кувалды и без "3 маркетов подряд".
    """

    def __init__(self, capital_share: float):
        self.capital = float(capital_share)
        self.side = OrderSide.BID if random.random() < 0.5 else OrderSide.ASK

        self.intent_flip_prob = 0.02  # шанс сменить сторону в тик
        self.wave_active = False
        self.wave_side: Optional[OrderSide] = None
        self.wave_steps_left = 0
        self.wave_intensity = 1.0
        self.next_wave_earliest_ts = time.time() + random.uniform(10.0, 40.0)

        # ---- позиция / PnL ----
        self.inventory: float = 0.0
        self.avg_entry_price: Optional[float] = None
        self.realized_pnl: float = 0.0

        # ---- TP-фаза (scale-out) ----
        self.tp_active: bool = False
        self.tp_end_ts: float = 0.0
        self.tp_side: Optional[OrderSide] = None
        self.tp_intensity: float = 0.0  # насколько агрессивно разгружаем (доля позы)
        self.tp_last_action_ts: float = 0.0
        self.next_profit_ts: float = time.time() + random.uniform(60.0, 180.0)

    # ======================== POSITION / PNL ==============================

    def _on_fill_position(self, price: float, qty: float, side: OrderSide):
        if qty <= 0:
            return

        if self.avg_entry_price is None:
            # первая сделка
            self.inventory = qty if side == OrderSide.BID else -qty
            self.avg_entry_price = price
            return

        pos = self.inventory

        # BUY
        if side == OrderSide.BID:
            if pos >= 0:
                new_pos = pos + qty
                if new_pos > 0:
                    self.avg_entry_price = (self.avg_entry_price * pos + price * qty) / new_pos
                else:
                    self.avg_entry_price = price
                self.inventory = new_pos
            else:
                # закрываем часть/весь шорт
                closing = min(qty, -pos)
                self.realized_pnl += (self.avg_entry_price - price) * closing
                pos += closing
                if qty > closing:
                    # переворот в лонг
                    new_qty = qty - closing
                    self.inventory = new_qty
                    self.avg_entry_price = price
                else:
                    self.inventory = pos
                    if pos == 0:
                        self.avg_entry_price = None

        # SELL
        else:
            if pos <= 0:
                new_pos = pos - qty
                total_notional = abs(self.avg_entry_price * pos) + price * qty
                if new_pos != 0:
                    self.avg_entry_price = total_notional / abs(new_pos)
                else:
                    self.avg_entry_price = None
                self.inventory = new_pos
            else:
                # закрываем часть/весь лонг
                closing = min(qty, pos)
                self.realized_pnl += (price - self.avg_entry_price) * closing
                pos -= closing
                if qty > closing:
                    # переворот в шорт
                    new_qty = qty - closing
                    self.inventory = -new_qty
                    self.avg_entry_price = price
                else:
                    self.inventory = pos
                    if pos == 0:
                        self.avg_entry_price = None

    def _unrealized_pnl(self, mid: float) -> float:
        if self.avg_entry_price is None or self.inventory == 0:
            return 0.0
        pos = self.inventory
        if pos > 0:
            return (mid - self.avg_entry_price) * pos
        else:
            return (self.avg_entry_price - mid) * (-pos)

    # ======================== TP START / STEP =============================

    def _maybe_start_tp(
        self,
        now: float,
        feat: Dict[str, Any],
        flow_activity: float,
        overextended: bool,
    ):
        """
        Решаем, включать ли TP-фазу.
        Условия:
          - есть заметная позиция
          - есть заметный профит
          - рынок перегрет (overextended / сильный тренд / сигма)
          - выдержан min-gap по времени (next_profit_ts)
        """
        if self.tp_active:
            return

        mid = feat["mid"]
        if mid is None:
            return
        if self.avg_entry_price is None or self.inventory == 0:
            return
        if now < self.next_profit_ts:
            return

        pos = self.inventory
        pos_notional = abs(pos) * mid
        if pos_notional < self.capital * 0.003:
            # слишком маленькая поза — нет смысла
            return

        unreal = self._unrealized_pnl(mid)
        pnl_frac = unreal / max(self.capital, 1.0)

        # пороги профита
        soft_thr = 0.0007   # 0.07% от капитала деска
        hard_thr = 0.0020   # 0.2%

        if pnl_frac < soft_thr:
            return

        trend_mag = abs(feat["trend"])
        sigma = feat["sigma"]

        # степень "перекрута"
        stretch = 0.0
        if overextended:
            stretch += 0.7
        stretch += 0.3 * _clip(trend_mag / 0.0015, 0.0, 1.0)
        stretch += 0.2 * _clip(sigma / 0.0007, 0.0, 1.0)
        stretch = _clip(stretch, 0.0, 1.5)

        if stretch < 0.35:
            # перекрута мало, ещё рано
            return

        # стартуем TP
        self.tp_active = True
        self.tp_side = OrderSide.ASK if pos > 0 else OrderSide.BID

        # доля позиции, которую будем выгружать за фазу
        if pnl_frac > hard_thr:
            tp_int = random.uniform(0.35, 0.65)
        else:
            tp_int = random.uniform(0.20, 0.45)
        # усиливаем при большем stretch
        tp_int *= (0.7 + 0.8 * stretch)
        self.tp_intensity = _clip(tp_int, 0.15, 0.9)

        self.tp_end_ts = now + random.uniform(10.0, 40.0)
        self.tp_last_action_ts = now - 1.0  # чтобы иметь право сходу что-то сделать
        # следующая потенциальная TP-фаза не раньше чем через 40–120 сек после старта
        self.next_profit_ts = now + random.uniform(40.0, 120.0)

    def _tp_step(
        self,
        now: float,
        agent_id: str,
        feat: Dict[str, Any],
    ) -> List[Order]:
        """
        Один тик TP-фазы:
          - уменьшенная обычная активность
          - мелкие MARKET против позиции
          - лимитные "стенки" в сторону TP
        """
        orders: List[Order] = []
        mid = feat["mid"]
        best_bid = feat["best_bid"]
        best_ask = feat["best_ask"]
        if mid is None or not self.tp_active or self.tp_side is None:
            return orders

        # Фаза завершилась по времени или по размеру позы
        if now >= self.tp_end_ts or self.inventory == 0 or self.avg_entry_price is None:
            self.tp_active = False
            self.tp_side = None
            self.tp_intensity = 0.0
            return orders

        # не чаще, чем раз в 0.3–0.8 сек
        min_dt = 0.3
        max_dt = 0.8
        dt_needed = random.uniform(min_dt, max_dt)
        if now - self.tp_last_action_ts < dt_needed:
            return orders

        pos = self.inventory
        pos_notional = abs(pos) * mid
        if pos_notional < self.capital * 0.0015:
            # почти вышли — можно выключать
            self.tp_active = False
            self.tp_side = None
            self.tp_intensity = 0.0
            return orders

        # размер одной разгрузки: доля текущей позы, ограниченная по notional
        base_frac = random.uniform(0.01, 0.05) * self.tp_intensity
        base_frac = _clip(base_frac, 0.01, 0.10)

        close_notional = pos_notional * base_frac
        close_notional = _clip(
            close_notional,
            self.capital * 0.0003,
            self.capital * 0.0045
        )
        qty = max(1.0, close_notional / mid)
        if qty > abs(pos):
            qty = abs(pos)

        # 1) маленький MARKET против позиции (с вероятностью)
        if random.random() < 0.35 and qty > 0.5:
            o_mkt = Order(
                order_id=str(uuid.uuid4()),
                agent_id=agent_id,
                side=self.tp_side,
                volume=float(qty),
                price=None,
                order_type=OrderType.MARKET,
                ttl=None,
            )
            orders.append(o_mkt)

        # 2) лимитные стены (часто) — на несколько тиков от лучшей
        if random.random() < 0.70:
            if self.tp_side == OrderSide.ASK and best_ask is not None:
                ticks = random.randint(1, 3)
                price = best_ask + ticks * TICK
            elif self.tp_side == OrderSide.BID and best_bid is not None:
                ticks = random.randint(1, 3)
                price = best_bid - ticks * TICK
            else:
                price = mid
            wall_notional = pos_notional * random.uniform(0.01, 0.04) * self.tp_intensity
            wall_notional = _clip(
                wall_notional,
                self.capital * 0.0003,
                self.capital * 0.0035,
            )
            wall_qty = max(1.0, wall_notional / price)
            wall_qty = min(wall_qty, abs(pos))  # не больше текущей позы

            if wall_qty > 0.5:
                o_wall = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    side=self.tp_side,
                    volume=float(wall_qty),
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=random.randint(10, 60),
                )
                orders.append(o_wall)

        self.tp_last_action_ts = now
        return orders

    # ======================== WAVES / BASE FLOW ===========================

    def _maybe_start_wave(self, now: float, feat: Dict[str, Any], flow_activity: float):
        if self.wave_active:
            return
        if now < self.next_wave_earliest_ts:
            return

        sigma = feat["sigma"]
        trend = feat["trend"]
        act = _clip(flow_activity, 0.0, 1.0)

        base_p = 0.02 + 0.06 * act + 1.0 * sigma + 0.25 * abs(trend)
        if random.random() > base_p:
            return

        # выбираем сторону волны: чаще по тренду, иногда против
        if trend > 0 and random.random() < 0.7:
            side = OrderSide.BID
        elif trend < 0 and random.random() < 0.7:
            side = OrderSide.ASK
        else:
            side = OrderSide.BID if random.random() < 0.5 else OrderSide.ASK

        self.wave_active = True
        self.wave_side = side
        self.wave_steps_left = random.randint(3, 5)
        self.wave_intensity = random.uniform(1.1, 1.6)
        self.next_wave_earliest_ts = now + random.uniform(30.0, 60.0)

    def generate_orders(
        self,
        now: float,
        agent_id: str,
        feat: Dict[str, Any],
        flow_activity: float,
        overextended: bool,
    ) -> List[Order]:
        orders: List[Order] = []
        mid = feat["mid"]
        best_bid = feat["best_bid"]
        best_ask = feat["best_ask"]
        if mid is None:
            return orders

        # потенциальный старт TP-фазы
        self._maybe_start_tp(now, feat, flow_activity, overextended)

        # волны
        self._maybe_start_wave(now, feat, flow_activity)
        in_wave = self.wave_active and self.wave_steps_left > 0

        # выбор базовой стороны
        if in_wave:
            side = self.wave_side or self.side
        else:
            side = self.side
            if random.random() < self.intent_flip_prob:
                self.side = OrderSide.BID if self.side == OrderSide.ASK else OrderSide.ASK
                side = self.side

        # базовая вероятность активности
        p_act = 0.12 + 0.30 * _clip(flow_activity, 0.0, 1.0)
        if overextended:
            p_act *= 0.3
        if in_wave:
            p_act *= 2.2  # волна — чаще активность

        # если TP активен и мы собираемся лезть в сторону текущей позиции — душим активность
        if self.tp_active and self.inventory != 0:
            if (self.inventory > 0 and side == OrderSide.BID) or (self.inventory < 0 and side == OrderSide.ASK):
                p_act *= 0.4  # меньше новых входов в ту же сторону

        dir_orders: List[Order] = []

        if random.random() <= p_act:
            # размер: очень небольшой, от 0.01% до 0.05% капитала деска
            base_frac = random.uniform(0.00008, 0.00035)
            base_frac *= (0.5 + 1.0 * _clip(flow_activity, 0.0, 1.0))
            if in_wave:
                base_frac *= self.wave_intensity

            # если рынок уже overextended и мы бьём в сторону тренда — душим объём
            trend = feat["trend"]
            if overextended:
                if (trend > 0 and side == OrderSide.BID) or (trend < 0 and side == OrderSide.ASK):
                    base_frac *= 0.5
                else:
                    # контртренд/заход против перекрута — наоборот, чуть усилить
                    base_frac *= 1.2

            notional = self.capital * base_frac
            notional = min(notional, self.capital * 0.001)  # максимум 0.1% капитала деска
            qty = max(1.0, notional / mid)

            # MARKET крайне редко, в основном лимиты
            base_mkt_p = 0.05 + 0.10 * _clip(flow_activity, 0.0, 1.0)
            if overextended:
                base_mkt_p *= 0.3
            if in_wave:
                base_mkt_p *= 1.8  # во время волны немного больше маркетов
            base_mkt_p = _clip(base_mkt_p, 0.02, 0.35)

            use_market = random.random() < base_mkt_p

            if use_market:
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    side=side,
                    volume=float(qty),
                    price=None,
                    order_type=OrderType.MARKET,
                    ttl=None,
                )
                dir_orders.append(o)
            else:
                if side == OrderSide.BID and best_bid is not None:
                    ticks = random.randint(-2, 1)
                    price = best_bid + ticks * TICK
                elif side == OrderSide.ASK and best_ask is not None:
                    ticks = random.randint(-1, 2)
                    price = best_ask - ticks * TICK
                else:
                    price = mid
                price = _post_only_price(side, price, best_bid, best_ask)
                ttl = random.randint(4, 15) if not in_wave else random.randint(2, 8)
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    side=side,
                    volume=float(qty),
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=ttl,
                )
                dir_orders.append(o)

        # обновляем wave-состояние
        if in_wave:
            self.wave_steps_left -= 1
            if self.wave_steps_left <= 0:
                self.wave_active = False

        # TP-шаг (может работать параллельно с обычным потоком)
        tp_orders = []
        if self.tp_active:
            tp_orders = self._tp_step(now, agent_id, feat)

        # склеиваем
        orders.extend(tp_orders)
        orders.extend(dir_orders)
        return orders

    def on_fill(self, price: float, qty: float, side: OrderSide):
        self._on_fill_position(price, qty, side)


class LiquidityDesk:
    """
    Ликвидностный деск института (как внутренний ММ) с "дыханием".
    Каждые 20–45 секунд меняет:
      - профиль размеров
      - смещение по тикам
      - допустимую дельту инвентаря
    Это ломает сетки и не даёт стакану стать решёткой.
    """

    def __init__(self, capital_share: float):
        self.capital = float(capital_share)
        self.inventory = 0.0

        # базовый таргет по инвентарю
        self.base_target_inv_frac = 0.05
        self.target_inv_frac = self.base_target_inv_frac

        self.last_requote_ts = time.time()

        # breathing
        self.breath_state_id = 0
        self.next_breath_ts = time.time() + random.uniform(20.0, 45.0)
        self.size_mult = 1.0
        self.bid_offset_ticks = 1
        self.ask_offset_ticks = 1

    def _breathe(self, now: float, sigma: float):
        if now < self.next_breath_ts:
            return
        self.breath_state_id += 1

        # новый режим: случайные параметры
        self.size_mult = random.uniform(0.7, 1.6)
        base_ticks = 1 if sigma < 0.0004 else 2
        extra = 0 if sigma < 0.0007 else 1
        self.bid_offset_ticks = base_ticks + random.randint(0, extra + 1)
        self.ask_offset_ticks = base_ticks + random.randint(0, extra + 1)

        self.target_inv_frac = self.base_target_inv_frac * random.uniform(0.7, 1.4)

        # следующее дыхание
        self.next_breath_ts = now + random.uniform(20.0, 45.0)

    def generate_orders(
        self,
        now: float,
        agent_id: str,
        feat: Dict[str, Any],
        flow_activity: float,
    ) -> List[Order]:
        orders: List[Order] = []
        mid = feat["mid"]
        best_bid = feat["best_bid"]
        best_ask = feat["best_ask"]
        if mid is None:
            return orders

        self._breathe(now, feat["sigma"])

        # вероятность, что этот тик будем что-то перекотировать
        p_act = 0.22 + 0.35 * _clip(flow_activity, 0.0, 1.0)
        if random.random() > p_act:
            return orders

        # желаемый максимум инвентаря
        max_inv_notional = self.capital * self.target_inv_frac
        max_inv_qty = max_inv_notional / mid if mid > 0 else 0.0

        # базовый размер лимиток
        base_notional = self.capital * random.uniform(0.0002, 0.0010) * self.size_mult
        qty = max(1.0, base_notional / mid)

        # если инвентарь сильно перекошен, смещаем сторону
        bias_bid = 1.0
        bias_ask = 1.0
        if self.inventory > 0 and abs(self.inventory) > 0.3 * max_inv_qty:
            # много лонга → меньше BID, больше ASK
            bias_bid = 0.4
            bias_ask = 1.7
        elif self.inventory < 0 and abs(self.inventory) > 0.3 * max_inv_qty:
            # много шорта
            bias_bid = 1.7
            bias_ask = 0.4

        # BID
        if random.random() < 0.6 * bias_bid:
            if best_bid is not None:
                ticks = random.randint(-self.bid_offset_ticks, self.bid_offset_ticks)
                price = best_bid + ticks * TICK
            else:
                price = mid - self.bid_offset_ticks * TICK
            price = _post_only_price(OrderSide.BID, price, best_bid, best_ask)
            ttl = random.randint(5, 20)
            q = min(qty, max_inv_qty - max(0.0, self.inventory))
            if q > 0.5:
                orders.append(
                    Order(
                        order_id=str(uuid.uuid4()),
                        agent_id=agent_id,
                        side=OrderSide.BID,
                        volume=float(q),
                        price=float(round(price, 5)),
                        order_type=OrderType.LIMIT,
                        ttl=ttl,
                    )
                )

        # ASK
        if random.random() < 0.6 * bias_ask:
            if best_ask is not None:
                ticks = random.randint(-self.ask_offset_ticks, self.ask_offset_ticks)
                price = best_ask - ticks * TICK
            else:
                price = mid + self.ask_offset_ticks * TICK
            price = _post_only_price(OrderSide.ASK, price, best_bid, best_ask)
            ttl = random.randint(5, 20)
            q = min(qty, max_inv_qty + min(0.0, self.inventory))
            if q > 0.5:
                orders.append(
                    Order(
                        order_id=str(uuid.uuid4()),
                        agent_id=agent_id,
                        side=OrderSide.ASK,
                        volume=float(q),
                        price=float(round(price, 5)),
                        order_type=OrderType.LIMIT,
                        ttl=ttl,
                    )
                )

        return orders

    def on_fill(self, price: float, qty: float, side: OrderSide):
        if side == OrderSide.BID:
            self.inventory += qty
        elif side == OrderSide.ASK:
            self.inventory -= qty


class MeanReversionDesk:
    """
    Контртрендовый деск с fatigue:
      - при частом успехе начинает "уставать" и работать реже
      - иногда ошибается и усиливает движение, как в реале
    """

    def __init__(self, capital_share: float):
        self.capital = float(capital_share)
        self.last_dirs = deque(maxlen=5)
        self.fatigue = 0.0
        self.last_update_ts = time.time()

    def _update_fatigue(self, now: float):
        dt = now - self.last_update_ts
        if dt <= 0:
            return
        # экспоненциальное восстановление
        self.fatigue *= math.exp(-dt / 40.0)  # таймконстанта ~60 сек
        self.last_update_ts = now

    def generate_orders(
        self,
        now: float,
        agent_id: str,
        feat: Dict[str, Any],
        flow_activity: float,
        overextended: bool,
    ) -> List[Order]:
        orders: List[Order] = []
        mid = feat["mid"]
        best_bid = feat["best_bid"]
        best_ask = feat["best_ask"]
        if mid is None:
            return orders

        self._update_fatigue(now)

        last_dir = feat["last_dir"]
        self.last_dirs.append(last_dir)

        # считаем "дисбаланс" последних шагов
        up = sum(1 for d in self.last_dirs if d > 0)
        dn = sum(1 for d in self.last_dirs if d < 0)
        imbalance = up - dn  # >0 = перекос вверх, <0 = вниз

        # если рынок спокойный → нет смысла что-то делать
        if abs(imbalance) <= 1 and abs(feat["last_ret"]) < 1e-4:
            return orders

        # вероятность активности
        base_p = 0.12 + 0.28 * _clip(flow_activity, 0.0, 1.0)
        if not overextended:
            base_p *= 0.8

        # усталость режет активность
        base_p *= (1.0 - 0.5 * _clip(self.fatigue, 0.0, 1.0))

        if random.random() > base_p:
            return orders

        # сторона: в норме ПРОТИВ перекоса, но при сильной усталости иногда ошибается
        anti_trend = True
        if self.fatigue > 0.6 and random.random() < 0.35:
            anti_trend = False

        if anti_trend:
            if imbalance > 0:
                side = OrderSide.ASK
            elif imbalance < 0:
                side = OrderSide.BID
            else:
                if last_dir > 0:
                    side = OrderSide.ASK
                elif last_dir < 0:
                    side = OrderSide.BID
                else:
                    return orders
        else:
            # ошибаемся: идём по движению
            if imbalance > 0:
                side = OrderSide.BID
            elif imbalance < 0:
                side = OrderSide.ASK
            else:
                if last_dir > 0:
                    side = OrderSide.BID
                elif last_dir < 0:
                    side = OrderSide.ASK
                else:
                    return orders

        # размер: 0.01–0.05% капитала деска
        base_frac = random.uniform(0.00015, 0.0006)
        base_frac *= (0.7 + 1.3 * _clip(flow_activity, 0.0, 1.0))
        notional = self.capital * base_frac
        notional = min(notional, self.capital * 0.0012)
        qty = max(1.0, notional / mid)

        # чаще MARKET (микро-откаты)
        mkt_p = 0.55
        if self.fatigue > 0.6:
            mkt_p *= 0.7  # при усталости меньше агрессии
        use_market = random.random() < mkt_p

        if use_market:
            o = Order(
                order_id=str(uuid.uuid4()),
                agent_id=agent_id,
                side=side,
                volume=float(qty),
                price=None,
                order_type=OrderType.MARKET,
                ttl=None,
            )
            orders.append(o)
        else:
            if side == OrderSide.BID and best_bid is not None:
                ticks = random.randint(-1, 1)
                price = best_bid + ticks * TICK
            elif side == OrderSide.ASK and best_ask is not None:
                ticks = random.randint(-1, 1)
                price = best_ask - ticks * TICK
            else:
                price = mid
            price = _post_only_price(side, price, best_bid, best_ask)
            ttl = random.randint(3, 10)
            o = Order(
                order_id=str(uuid.uuid4()),
                agent_id=agent_id,
                side=side,
                volume=float(qty),
                price=float(round(price, 5)),
                order_type=OrderType.LIMIT,
                ttl=ttl,
            )
            orders.append(o)

        # усталость растёт, если MR вообще сработал
        self.fatigue = _clip(self.fatigue + 0.12, 0.0, 1.0)

        return orders

    def on_fill(self, price: float, qty: float, side: OrderSide):
        # можно вести статистику, если понадобится
        pass


class NoiseDesk:
    """
    Крошечный шумовой поток.
    Маленькие лимитки/маркет-ордера в случайные моменты и стороны,
    ломают решётку и создают реальный микро-шум.
    """

    def __init__(self, capital_share: float):
        self.capital = float(capital_share)

    def generate_orders(
        self,
        agent_id: str,
        feat: Dict[str, Any],
        flow_activity: float,
    ) -> List[Order]:
        orders: List[Order] = []
        mid = feat["mid"]
        best_bid = feat["best_bid"]
        best_ask = feat["best_ask"]
        if mid is None:
            return orders

        # низкая вероятность активности
        base_p = 0.05 + 0.15 * _clip(flow_activity, 0.0, 1.0)
        if random.random() > base_p:
            return orders

        side = OrderSide.BID if random.random() < 0.5 else OrderSide.ASK

        # размер: 0.005–0.02% капитала noise-деска
        frac = random.uniform(0.00005, 0.0002)
        notional = self.capital * frac
        notional = min(notional, self.capital * 0.0004)
        qty = max(1.0, notional / mid)

        # выбор типа ордера
        mkt_p = 0.35
        use_market = random.random() < mkt_p

        if use_market:
            orders.append(
                Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    side=side,
                    volume=float(qty),
                    price=None,
                    order_type=OrderType.MARKET,
                    ttl=None,
                )
            )
        else:
            # ставим не только возле лучшей, но и слегка глубже
            if side == OrderSide.BID and best_bid is not None:
                ticks = random.randint(-4, 2)
                price = best_bid + ticks * TICK
            elif side == OrderSide.ASK and best_ask is not None:
                ticks = random.randint(-2, 4)
                price = best_ask - ticks * TICK
            else:
                price = mid
            price = _post_only_price(side, price, best_bid, best_ask)
            ttl = random.randint(3, 25)
            orders.append(
                Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    side=side,
                    volume=float(qty),
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=ttl,
                )
            )

        return orders


# ============================================================
# ГЛАВНЫЙ КЛАСС: ИНСТИТУТ
# ============================================================

class InstitutionalExecutor:
    """
    Институт, внутри которого:
      - DirectionalDesk (тренд + микроволны + TP-фаза)
      - LiquidityDesk v2 (дышащая ликвидность)
      - MeanReversionDesk (контртренд с усталостью/ошибками)
      - NoiseDesk (шум)

    Внешний интерфейс тот же:
      - generate_orders(order_book, market_context=None, **kwargs)
      - on_order_filled(order_id, price, qty, side, slippage=0.0)
      - restore_capital()
      - perceive_market(market_context)
    """

    def __init__(self, agent_id: str, capital: float, num_subagents: int = 8):
        self.agent_id = agent_id
        self.capital = float(capital)

        # делим капитал между десками
        dir_cap = self.capital * 0.33
        liq_cap = self.capital * 0.37
        mr_cap = self.capital * 0.23
        noise_cap = self.capital * 0.07

        self.directional_desk = DirectionalDesk(dir_cap)
        self.liquidity_desk = LiquidityDesk(liq_cap)
        self.mr_desk = MeanReversionDesk(mr_cap)
        self.noise_desk = NoiseDesk(noise_cap)

        # история трейдов для фичей
        self.trade_history: deque[Dict[str, Any]] = deque(maxlen=5000)

        # маппинг ордер → деск
        self._order_to_desk: Dict[str, str] = {}

        # стохастика активности (AR(1) на [0;1])
        self.flow_activity = 0.4
        self.flow_mu = 0.35
        self.flow_phi = 0.985
        self.flow_sigma = 0.10
        self.last_flow_update_ts = 0.0

        # якорь цены (EWMA mid), чтобы понимать, как далеко ушли
        self.anchor_mid: Optional[float] = None

        # трекинг направленного notional (для контроля дрейфа)
        self.signed_notional_ewma = 0.0

    # --- служебка ---

    def _update_trade_history(self, order_book):
        for t in list(order_book.trade_history)[-256:]:
            self.trade_history.append(t)

    def _update_flow_activity(self, feat: Dict[str, Any], now: float):
        if self.last_flow_update_ts <= 0.0:
            self.last_flow_update_ts = now
            return
        dt = now - self.last_flow_update_ts
        if dt <= 0:
            dt = 1e-3
        dt = min(dt, 5.0)

        z = random.gauss(0.0, 1.0)
        act = self.flow_activity
        act = self.flow_mu + self.flow_phi * (act - self.flow_mu) + self.flow_sigma * math.sqrt(dt) * z

        sigma = feat["sigma"]
        trend = abs(feat["trend"])

        # менее агрессивная реакция на тренд/волу
        act += 1.5 * sigma + 0.25 * trend

        # если уже сильно ушли от anchor_mid — режем активность, чтобы не разгонять дальше
        mid = feat["mid"]
        if mid is not None and self.anchor_mid is not None:
            spread = feat["spread"] or TICK
            ticks = (mid - self.anchor_mid) / max(TICK, spread)
            over = abs(ticks)
            if over > 40.0:
                act *= 0.65
            elif over > 25.0:
                act *= 0.8

        self.flow_activity = _clip(act, 0.0, 1.0)
        self.last_flow_update_ts = now

    def _update_anchor_mid(self, mid: Optional[float]):
        if mid is None:
            return
        if self.anchor_mid is None:
            self.anchor_mid = float(mid)
        else:
            # очень медленный EWMA → "фундаментальный" уровень
            self.anchor_mid = 0.995 * self.anchor_mid + 0.005 * float(mid)

    def _is_overextended(self, feat: Dict[str, Any]) -> bool:
        mid = feat["mid"]
        if mid is None or self.anchor_mid is None:
            return False
        spread = feat["spread"] or TICK
        ticks = (mid - self.anchor_mid) / max(TICK, spread)
        # если ушли > 30 "спредов" вверх/вниз → считаем перекосом
        return abs(ticks) > 30.0

    # --- основной API ---

    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        now = _ctx_now(market_context)
        self._now_ts = now
        feat = _extract_features(order_book, self.trade_history, self.capital, now=now)
        self._update_trade_history(order_book)
        feat = _extract_features(order_book, self.trade_history, self.capital)
        self._update_anchor_mid(feat["mid"])
        self._update_flow_activity(feat, now)

        overextended = self._is_overextended(feat)
        flow = self.flow_activity

        # глобальная вероятность "пустого тика" (нет сделок)
        if flow < 0.20 and random.random() < 0.80:
            return []
        if flow < 0.35 and random.random() < 0.50:
            return []
        # при очень высокой активности наоборот режем, чтобы не было runaway
        if flow > 0.85 and random.random() < 0.70:
            return []

        # собираем ордера с четырёх десков
        orders: List[Order] = []

        liq_orders = self.liquidity_desk.generate_orders(
            now=now,
            agent_id=self.agent_id,
            feat=feat,
            flow_activity=flow,
        )
        for o in liq_orders:
            orders.append(o)
            self._order_to_desk[o.order_id] = "liq"

        dir_orders = self.directional_desk.generate_orders(
            now=now,
            agent_id=self.agent_id,
            feat=feat,
            flow_activity=flow,
            overextended=overextended,
        )
        for o in dir_orders:
            orders.append(o)
            self._order_to_desk[o.order_id] = "dir"

        mr_orders = self.mr_desk.generate_orders(
            now=now,
            agent_id=self.agent_id,
            feat=feat,
            flow_activity=flow,
            overextended=overextended,
        )
        for o in mr_orders:
            orders.append(o)
            self._order_to_desk[o.order_id] = "mr"

        noise_orders = self.noise_desk.generate_orders(
            agent_id=self.agent_id,
            feat=feat,
            flow_activity=flow,
        )
        for o in noise_orders:
            orders.append(o)
            self._order_to_desk[o.order_id] = "noise"

        if not orders:
            return []

        # жёсткий контроль по notional, чтобы институт один не мог устроить вынос

        mid = feat["mid"] or 0.0
        best_bid = feat["best_bid"]
        best_ask = feat["best_ask"]

        total_notional = 0.0
        signed_notional = 0.0
        for o in orders:
            if o.price is not None:
                price = o.price
            elif best_bid is not None and best_ask is not None:
                price = 0.5 * (best_bid + best_ask)
            else:
                price = mid if mid > 0 else 100.0
            notional = price * o.volume
            total_notional += notional
            if o.side == OrderSide.BID:
                signed_notional += notional
            elif o.side == OrderSide.ASK:
                signed_notional -= notional

        # максимум 0.2% капитала за тик по общему объёму
        max_total = self.capital * 0.002
        if total_notional > max_total and total_notional > 0:
            scale = max_total / total_notional
            new_orders: List[Order] = []
            for o in orders:
                new_vol = o.volume * scale
                if new_vol >= 0.5:
                    o.volume = float(new_vol)
                    new_orders.append(o)
                else:
                    self._order_to_desk.pop(o.order_id, None)
            orders = new_orders

        # пересчитываем signed_notional после скейла
        signed_notional = 0.0
        for o in orders:
            if o.price is not None:
                price = o.price
            elif best_bid is not None and best_ask is not None:
                price = 0.5 * (best_bid + best_ask)
            else:
                price = mid if mid > 0 else 100.0
            notional = price * o.volume
            if o.side == OrderSide.BID:
                signed_notional += notional
            elif o.side == OrderSide.ASK:
                signed_notional -= notional

        # максимум 0.1% капитала по чистому направленному notional за тик
        max_signed = self.capital * 0.001
        if abs(signed_notional) > max_signed and abs(signed_notional) > 0:
            dir_like_ids = {
                oid for oid, desk in self._order_to_desk.items()
                if desk in ("dir", "mr", "noise")
            }
            dir_signed = 0.0
            for o in orders:
                if o.order_id not in dir_like_ids:
                    continue
                if o.price is not None:
                    price = o.price
                elif best_bid is not None and best_ask is not None:
                    price = 0.5 * (best_bid + best_ask)
                else:
                    price = mid if mid > 0 else 100.0
                n = price * o.volume
                if o.side == OrderSide.BID:
                    dir_signed += n
                elif o.side == OrderSide.ASK:
                    dir_signed -= n

            if abs(dir_signed) > max_signed and abs(dir_signed) > 0:
                scale = max_signed / abs(dir_signed)
                new_orders = []
                for o in orders:
                    if o.order_id in dir_like_ids:
                        new_vol = o.volume * scale
                        if new_vol >= 0.5:
                            o.volume = float(new_vol)
                            new_orders.append(o)
                        else:
                            self._order_to_desk.pop(o.order_id, None)
                    else:
                        new_orders.append(o)
                orders = new_orders

        self.signed_notional_ewma = 0.98 * self.signed_notional_ewma + 0.02 * signed_notional

        return orders

    # --- обработка исполнений ---

    def on_order_filled(self, order_id: str, price: float, qty: float, side: OrderSide, slippage: float = 0.0):
        desk_name = self._order_to_desk.pop(order_id, None)
        if desk_name is None:
            return

        if desk_name == "liq":
            self.liquidity_desk.on_fill(price, qty, side)
        elif desk_name == "dir":
            self.directional_desk.on_fill(price, qty, side)
        elif desk_name == "mr":
            self.mr_desk.on_fill(price, qty, side)
        elif desk_name == "noise":
            # шум инвентаря не ведём
            pass

    # --- совместимый API ---

    def restore_capital(self):
        # институт здесь не перезапускаем, риск на десках и лимитируется в generate_orders
        pass

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
