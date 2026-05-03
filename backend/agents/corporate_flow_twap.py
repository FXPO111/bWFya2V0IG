# corporate_flow_twap.py
# Реалистичный блок корпоративного исполнения объёма.
# Несколько стилей исполнения (TWAP / VWAP-like / POV / Opportunistic / IS-like)
# в одном классе CorporateFlowTWAP, без тренд-фолловинга и смены стороны.

import uuid
import random
import time
import math
from collections import deque
from typing import Deque, Optional, Dict, Any

import numpy as np

from backend.core.order import Order, OrderSide, OrderType, TICK

def _ctx_now(ctx):
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()

def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _ewma(prev, new, alpha):
    if prev is None:
        return new
    return (1 - alpha) * prev + alpha * new


def _extract_features(order_book, trade_history: Deque, capital: float, now: float = None) -> Dict[str, Any]:
    """
    Локальные фичи рынка:
      - mid, spread
      - best bid/ask
      - top_depth (видимая ликвидность на верхних уровнях)
      - turnover_per_sec (приблизительный объём в деньгах в сек)
    """
    snap = order_book.get_order_book_snapshot(depth=5)
    bids = snap.get("bids", [])
    asks = snap.get("asks", [])

    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None

    if best_bid is not None and best_ask is not None:
        mid = 0.5 * (best_bid + best_ask)
        spread = best_ask - best_bid
    else:
        mid, spread = None, 0.0

    best_bid_sz = bids[0]["volume"] if bids else 0.0
    best_ask_sz = asks[0]["volume"] if asks else 0.0
    top_depth = 0.5 * (best_bid_sz + best_ask_sz) if (best_bid_sz and best_ask_sz) else max(best_bid_sz, best_ask_sz)

    if now is None:
        now = time.time()
    recent = [t for t in trade_history if now - float(t.get("timestamp", now)) <= 60.0]

    turnover = 0.0
    for t in recent:
        p = float(t.get("price", 0.0))
        v = float(t.get("volume") or t.get("qty") or 0.0)
        turnover += p * v
    turnover_per_sec = turnover / 60.0 if recent else 0.0

    return {
        "mid": mid,
        "spread": float(spread),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "top_depth": float(top_depth),
        "turnover_per_sec": float(turnover_per_sec),
    }


class CorporateFlowTWAP:
    """
    Корпоративный исполнитель объёма с несколькими стилями исполнения.

    Важные принципы:
      - side фиксирован (side_bias), никакого переворота направления;
      - цель — исполнить target_notional между session_start и session_end;
      - стратегии различаются по тому, КАК размазывать объём:
            * профиль 1: чистый TWAP
            * профиль 2: VWAP-like (больше объёма в середине)
            * профиль 3: POV (процент от текущего объёма)
            * профиль 4: opportunistic liquidity seeking
            * профиль 5: IS-like (ускорение при неблагоприятном движении)
            * профиль 6: mix TWAP/POV
            * профиль 7: mix VWAP/Opportunistic
      - никаких тренд-фолловинг решений по направлению.
    """

    def __init__(self, agent_id: str, capital: float):
        self.agent_id = str(agent_id)
        self.capital = float(capital)

        self.cash = float(capital)
        self.position = 0.0

        # история сделок — только для оценки VWAP/объёмов, НЕ для тренда
        self.trade_history: Deque = deque(maxlen=2000)

        # направление исполнения (BID/ASK) задаёт менеджер
        self.side_bias: Optional[OrderSide] = None

        # общая цель по нотации
        self.target_notional: Optional[float] = None
        self.executed_notional: float = 0.0
        self.completed: bool = False

        # сессия исполнения
        now = time.time()
        self.session_start: float = now
        self.session_end: float = now + 30.0 * 60.0   # дефолт 30 минут
        self.session_end_ts: float = self.session_end

        # arrival mid — для IS-like
        self.arrival_mid: Optional[float] = None

        # VWAP по собственному исполнению
        self.vwap_price: Optional[float] = None
        self.vwap_notional: float = 0.0

        # Глобальный timing
        self.next_ts: float = now + random.uniform(3.0, 10.0)
        self.base_intensity: float = random.uniform(0.4, 0.9)

        # Профиль (стиль исполнения)
        self.profile_id: int = (abs(hash(self.agent_id)) % 7) + 1
        self.style_cfg: Dict[str, Any] = self._init_style_config(self.profile_id)
        self.style: str = self.style_cfg["style"]

        # EWMA по объёму/воле по желанию
        self.turnover_ewma: Optional[float] = None

        self._own_order_remaining = {}

    # ============================================================
    # Профиль стиля исполнения
    # ============================================================
    def _init_style_config(self, pid: int) -> Dict[str, Any]:
        """
        Возвращает словарь конфигурации стиля.
        style: "twap", "vwap", "pov", "opp", "is", "mix_twap_pov", "mix_vwap_opp"
        """

        # базовая дефолт-конфигурация
        cfg = {
            "style": "twap",
            "dt_range": (8.0, 16.0),       # базовый интервал между клипами
            "mkt_base": 0.12,              # базовая доля MARKET
            "max_clip_cap_frac": 0.015,    # макс. клип в доле от капитала
            "max_clip_liq_frac": 0.20,     # макс. клип в доле от видимой ликвидности
            "pov_range": (0.06, 0.16),     # для POV: доля от объёма
            "front_load": 0.0,             # для IS: насколько фронт-лоудится
        }

        if pid == 1:
            # Чистый TWAP — равномерно по времени, низкая агрессия
            cfg["style"] = "twap"
            cfg["dt_range"] = (9.0, 18.0)
            cfg["mkt_base"] = 0.10
        elif pid == 2:
            # VWAP-like — больше объёма в середине
            cfg["style"] = "vwap"
            cfg["dt_range"] = (7.0, 14.0)
            cfg["mkt_base"] = 0.10
        elif pid == 3:
            # POV — участие в текущем объёме
            cfg["style"] = "pov"
            cfg["dt_range"] = (6.0, 12.0)
            cfg["mkt_base"] = 0.16
            cfg["pov_range"] = (0.05, 0.18)
        elif pid == 4:
            # Opportunistic — ждёт хорошей ликвидности, бьёт пакетами
            cfg["style"] = "opp"
            cfg["dt_range"] = (5.0, 11.0)
            cfg["mkt_base"] = 0.08
            cfg["max_clip_liq_frac"] = 0.30
        elif pid == 5:
            # IS-like — ускоряется при неблагоприятном движении
            cfg["style"] = "is"
            cfg["dt_range"] = (6.0, 13.0)
            cfg["mkt_base"] = 0.18
            cfg["front_load"] = 0.55
        elif pid == 6:
            # Mix TWAP/POV
            cfg["style"] = "mix_twap_pov"
            cfg["dt_range"] = (7.0, 15.0)
            cfg["mkt_base"] = 0.14
            cfg["pov_range"] = (0.04, 0.14)
        else:
            # Mix VWAP/Opportunistic
            cfg["style"] = "mix_vwap_opp"
            cfg["dt_range"] = (6.0, 14.0)
            cfg["mkt_base"] = 0.12
            cfg["max_clip_liq_frac"] = 0.25

        return cfg

    # ============================================================
    # Конфиг от менеджера
    # ============================================================
    def configure(self, side_bias=None, target_notional=None, session_minutes=None, now=None):
        if now is None:
            now = time.time()

        if side_bias is not None:
            self.side_bias = side_bias

        if target_notional is not None and target_notional > 0:
            self.target_notional = float(target_notional)

        if session_minutes is not None and session_minutes > 1:
            self.session_start = now
            self.session_end = now + float(session_minutes) * 60.0
            self.session_end_ts = self.session_end

        # стартовый next_ts с небольшим джиттером, чтобы всё не совпадало
        base_dt_min, base_dt_max = self.style_cfg["dt_range"]
        jitter = random.uniform(0.2, 0.8)
        self.next_ts = now + random.uniform(base_dt_min * 0.3, base_dt_max * 0.8) * jitter

    # ============================================================
    def _update_trade_history(self, order_book):
        """
        Заполняем trade_history свежими тиками.
        Никакого тренда отсюда не считаем — только объём/turnover.
        """
        try:
            recent = list(order_book.trade_history)[-200:]
        except Exception:
            recent = []
        for t in recent:
            self.trade_history.append(t)

    # ============================================================
    def _ensure_target_notional(self, mid: float):
        if self.target_notional is not None:
            return
        # Если менеджер не задал — берём 1–3% от капитала
        frac = random.uniform(0.01, 0.03)
        self.target_notional = self.capital * frac

    # ============================================================
    def _remaining_notional(self) -> Optional[float]:
        if self.target_notional is None:
            return None
        return max(self.target_notional - self.executed_notional, 0.0)

    # ============================================================
    def _session_progress(self, now: float) -> float:
        horizon = max(self.session_end - self.session_start, 1.0)
        return _clip((now - self.session_start) / horizon, 0.0, 1.0)

    # ============================================================
    def _estimate_turnover_per_sec(self) -> float:
        now = getattr(self, "_now_ts", time.time())
        window = 60.0
        trades = [t for t in self.trade_history if now - float(t.get("timestamp", now)) <= window]
        if not trades:
            return 0.0
        turnover = 0.0
        for t in trades:
            p = float(t.get("price", 0.0))
            v = float(t.get("volume") or t.get("qty") or 0.0)
            turnover += p * v
        return turnover / window

    # ============================================================
    def _compute_clip_twap(self, now: float, mid: float, remaining: float, features: Dict[str, Any]):
        """
        Чистый TWAP: равномерно по времени, лёгкий шум.
        """
        horizon_left = max(self.session_end - now, 5.0)
        dt_min, dt_max = self.style_cfg["dt_range"]
        base_dt = random.uniform(dt_min, dt_max)

        slices_left = max(1, int(horizon_left / base_dt))
        base_slice = remaining / slices_left

        top_depth = max(features.get("top_depth", 1.0), 1.0)
        liq_notional = top_depth * mid

        max_clip_liq = liq_notional * self.style_cfg["max_clip_liq_frac"]
        max_clip_cap = self.capital * self.style_cfg["max_clip_cap_frac"]

        clip_notional = min(base_slice, max_clip_liq, max_clip_cap, remaining)
        clip_notional = max(clip_notional, mid * 0.2)

        qty = clip_notional / mid
        qty = max(1.0, qty)

        dt = base_dt
        return qty, clip_notional, dt

    # ============================================================
    def _compute_clip_vwap(self, now: float, mid: float, remaining: float, features: Dict[str, Any]):
        """
        VWAP-like: больше объёма в середине сессии.
        """
        progress = self._session_progress(now)
        # "колокольчик" плотности around 0.5
        x = progress - 0.5
        bump = math.exp(- (x * x) / 0.04)  # широкий бугор
        density = 0.6 + 0.9 * bump         # от ~0.6 до ~1.5

        horizon_left = max(self.session_end - now, 5.0)
        dt_min, dt_max = self.style_cfg["dt_range"]
        base_dt = random.uniform(dt_min, dt_max)

        slices_left = max(1, int(horizon_left / base_dt))
        base_slice = remaining / slices_left

        desired_slice = base_slice * density

        top_depth = max(features.get("top_depth", 1.0), 1.0)
        liq_notional = top_depth * mid

        max_clip_liq = liq_notional * self.style_cfg["max_clip_liq_frac"]
        max_clip_cap = self.capital * self.style_cfg["max_clip_cap_frac"]

        clip_notional = min(desired_slice, max_clip_liq, max_clip_cap, remaining)
        clip_notional = max(clip_notional, mid * 0.2)

        qty = clip_notional / mid
        qty = max(1.0, qty)

        # в середине сессии — чаще клипы (уменьшаем dt)
        dt = base_dt / density
        dt = _clip(dt, dt_min * 0.4, dt_max * 1.4)

        return qty, clip_notional, dt

    # ============================================================
    def _compute_clip_pov(self, now: float, mid: float, remaining: float, features: Dict[str, Any]):
        """
        POV: participation of volume. Если объёма мало — fallback к маленькому TWAP.
        """
        dt_min, dt_max = self.style_cfg["dt_range"]
        base_dt = random.uniform(dt_min, dt_max)

        turnover_ps = self._estimate_turnover_per_sec()
        self.turnover_ewma = _ewma(self.turnover_ewma, turnover_ps, 0.3)

        pov_lo, pov_hi = self.style_cfg["pov_range"]
        pov = random.uniform(pov_lo, pov_hi)

        # ожидаемый оборот за следующий интервал
        effective_turnover = (self.turnover_ewma or turnover_ps)
        expected_turnover = effective_turnover * base_dt

        if expected_turnover <= mid * 3.0:
            # рынка почти нет — fallback к маленькому TWAP
            top_depth = max(features.get("top_depth", 1.0), 1.0)
            liq_notional = top_depth * mid
            base_slice = min(remaining / 10.0, liq_notional * 0.1, self.capital * 0.005)
            clip_notional = max(base_slice, mid * 0.2)
        else:
            clip_notional = pov * expected_turnover
            clip_notional = min(clip_notional, remaining)

        top_depth = max(features.get("top_depth", 1.0), 1.0)
        liq_notional = top_depth * mid

        max_clip_liq = liq_notional * self.style_cfg["max_clip_liq_frac"]
        max_clip_cap = self.capital * self.style_cfg["max_clip_cap_frac"]
        clip_notional = min(clip_notional, max_clip_liq, max_clip_cap, remaining)
        clip_notional = max(clip_notional, mid * 0.2)

        qty = clip_notional / mid
        qty = max(1.0, qty)

        dt = base_dt
        return qty, clip_notional, dt

    # ============================================================
    def _compute_clip_opp(self, now: float, mid: float, remaining: float, features: Dict[str, Any]):
        """
        Opportunistic: ждёт повышенной ликвидности, тогда даёт более жирный клип.
        """
        dt_min, dt_max = self.style_cfg["dt_range"]
        top_depth = max(features.get("top_depth", 0.0), 0.0)
        liq_notional = top_depth * mid

        # порог "хорошей" ликвидности
        threshold = self.capital * 0.003

        if liq_notional <= threshold:
            # ликвидности мало — ждём
            dt = random.uniform(dt_min * 0.7, dt_max * 1.2)
            return 0.0, 0.0, dt

        # когда ликвидность высокая — бьём более жирно
        base_slice = min(
            liq_notional * random.uniform(0.12, self.style_cfg["max_clip_liq_frac"]),
            self.capital * self.style_cfg["max_clip_cap_frac"],
            remaining,
        )
        clip_notional = max(base_slice, mid * 0.5)

        qty = clip_notional / mid
        qty = max(1.0, qty)

        dt = random.uniform(dt_min * 0.4, dt_max * 0.9)
        return qty, clip_notional, dt

    # ============================================================
    def _compute_clip_is(self, now: float, mid: float, remaining: float, features: Dict[str, Any]):
        """
        IS-like: если цена идёт против клиента — ускоряемся (больше клипы и/или чаще),
                 если в его сторону — замедляемся.
        """
        dt_min, dt_max = self.style_cfg["dt_range"]
        base_dt = random.uniform(dt_min, dt_max)

        # базовый TWAP
        horizon_left = max(self.session_end - now, 5.0)
        slices_left = max(1, int(horizon_left / base_dt))
        base_slice = remaining / slices_left

        # arrival mid
        if self.arrival_mid is None:
            self.arrival_mid = mid

        # для BUY: неблагоприятно, если цена выше arrival
        # для SELL: неблагоприятно, если цена ниже arrival
        adverse = 0.0
        if self.side_bias == OrderSide.BID:
            adverse = (mid - self.arrival_mid) / max(TICK, 1e-9)
        elif self.side_bias == OrderSide.ASK:
            adverse = (self.arrival_mid - mid) / max(TICK, 1e-9)

        adverse_ticks = adverse
        adverse_clipped = _clip(adverse_ticks / 10.0, -1.0, 1.0)

        # front_load фактор
        front = self.style_cfg.get("front_load", 0.5)
        # adverse >0 → ускоряемся
        accel = 1.0 + front * max(0.0, adverse_clipped)   # 1..(1+front)
        # если пока в нашу сторону — можем чуть замедлиться
        slow = 1.0 - 0.5 * max(0.0, -adverse_clipped)     # 0.5..1

        factor = accel * slow

        desired_slice = base_slice * factor

        top_depth = max(features.get("top_depth", 1.0), 1.0)
        liq_notional = top_depth * mid

        max_clip_liq = liq_notional * self.style_cfg["max_clip_liq_frac"]
        max_clip_cap = self.capital * self.style_cfg["max_clip_cap_frac"]
        clip_notional = min(desired_slice, max_clip_liq, max_clip_cap, remaining)
        clip_notional = max(clip_notional, mid * 0.2)

        qty = clip_notional / mid
        qty = max(1.0, qty)

        dt = base_dt / factor
        dt = _clip(dt, dt_min * 0.4, dt_max * 1.5)

        return qty, clip_notional, dt

    # ============================================================
    def _compute_clip_mix_twap_pov(self, now: float, mid: float, remaining: float, features: Dict[str, Any]):
        """
        Смешанный режим: часть шагов чистый TWAP, часть POV.
        """
        if random.random() < 0.5:
            return self._compute_clip_twap(now, mid, remaining, features)
        else:
            return self._compute_clip_pov(now, mid, remaining, features)

    # ============================================================
    def _compute_clip_mix_vwap_opp(self, now: float, mid: float, remaining: float, features: Dict[str, Any]):
        """
        Смешанный режим: VWAP-like + opportunistic bursts.
        """
        # Если ликвидность сильно выше среднего — используем opportunistic
        top_depth = max(features.get("top_depth", 0.0), 0.0)
        liq_notional = top_depth * mid
        threshold = self.capital * 0.003

        if liq_notional > threshold and random.random() < 0.6:
            return self._compute_clip_opp(now, mid, remaining, features)
        else:
            return self._compute_clip_vwap(now, mid, remaining, features)

    # ============================================================
    def _compute_clip(self, now: float, mid: float, remaining: float, features: Dict[str, Any]):
        """
        Роутер по стилям.
        """
        style = self.style
        if style == "twap":
            return self._compute_clip_twap(now, mid, remaining, features)
        elif style == "vwap":
            return self._compute_clip_vwap(now, mid, remaining, features)
        elif style == "pov":
            return self._compute_clip_pov(now, mid, remaining, features)
        elif style == "opp":
            return self._compute_clip_opp(now, mid, remaining, features)
        elif style == "is":
            return self._compute_clip_is(now, mid, remaining, features)
        elif style == "mix_twap_pov":
            return self._compute_clip_mix_twap_pov(now, mid, remaining, features)
        else:  # "mix_vwap_opp"
            return self._compute_clip_mix_vwap_opp(now, mid, remaining, features)

    # ============================================================
    def _pick_order_type(self, features: Dict[str, Any]) -> bool:
        """
        Выбор MARKET vs LIMIT.
        Возвращает True, если MARKET, False если LIMIT.
        Зависит от спреда и стиля, но не от тренда.
        """
        spread = float(features.get("spread", 0.0))
        mkt_base = float(self.style_cfg.get("mkt_base", 0.12))

        if spread <= 2 * TICK:
            mkt_p = mkt_base + 0.05
        elif spread > 4 * TICK:
            mkt_p = mkt_base * 0.5
        else:
            mkt_p = mkt_base

        # Opportunistic стиль — ещё менее маркетовый
        if self.style in ("opp", "mix_vwap_opp"):
            mkt_p *= 0.7

        # POV и IS могут быть чуть агрессивнее
        if self.style in ("pov", "is", "mix_twap_pov"):
            mkt_p *= 1.2

        mkt_p = _clip(mkt_p, 0.03, 0.4)
        return random.random() < mkt_p

    # ============================================================
    # Основная функция генерации ордеров
    # ============================================================
    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = (float(getattr(market_context, "now_ts", 0.0)) if market_context is not None and getattr(market_context,"now_ts",None) is not None else time.time())
        self._now_ts = now

        if self.completed:
            return []

        if now >= self.session_end:
            self.completed = True
            return []

        if self.side_bias is None or self.target_notional is None:
            return []

        if now < self.next_ts:
            return []

        # Обновляем trade history для объёмов и VWAP
        self._update_trade_history(order_book)

        # Фичи рынка
        features = _extract_features(order_book, self.trade_history, self.capital, now=now)
        mid = features["mid"]
        if mid is None:
            # рынок "мертвый", переносим попытку
            self.next_ts = now + random.uniform(5.0, 12.0)
            return []

        if self.arrival_mid is None:
            self.arrival_mid = mid

        self._ensure_target_notional(mid)
        remaining = self._remaining_notional()
        if remaining is None or remaining <= mid * 0.5:
            self.completed = True
            return []

        qty, clip_notional, dt = self._compute_clip(now, mid, remaining, features)

        # стиль мог решить "ждать" (opp) → clip_notional = 0
        if clip_notional <= 0.0 or qty <= 0.0:
            dt_min, dt_max = self.style_cfg["dt_range"]
            self.next_ts = now + max(2.0, random.uniform(dt_min * 0.6, dt_max * 1.2))
            return []

        # MARKET vs LIMIT
        use_market = self._pick_order_type(features)

        orders = []
        if use_market:
            order = Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=self.side_bias,
                volume=float(qty),
                price=None,
                order_type=OrderType.MARKET,
            )
            orders.append(order)
        else:
            best_bid = features.get("best_bid")
            best_ask = features.get("best_ask")

            if self.side_bias == OrderSide.BID:
                ref = best_bid if best_bid is not None else (mid - TICK)
                offset_ticks = random.choice([0, 0, 1])
                price = ref - offset_ticks * TICK
            else:
                ref = best_ask if best_ask is not None else (mid + TICK)
                offset_ticks = random.choice([0, 0, 1])
                price = ref + offset_ticks * TICK

            ttl = random.randint(10, 35)
            order = Order(
                order_id=str(uuid.uuid4()),
                agent_id=self.agent_id,
                side=self.side_bias,
                volume=float(qty),
                price=float(price),
                order_type=OrderType.LIMIT,
                ttl=ttl,
            )
            orders.append(order)

        # Тайминг следующего клипа
        dt_min, dt_max = self.style_cfg["dt_range"]
        jitter = random.uniform(-0.3 * dt, 0.4 * dt)
        dt_final = _clip(dt + jitter, dt_min * 0.3, dt_max * 1.8)
        self.next_ts = now + max(2.0, dt_final)

        self._own_order_remaining[order.order_id] = float(order.volume)

        return orders

    # ============================================================
    def on_order_filled(self, order_id, price, qty, side, slippage=0.0):
        rem = self._own_order_remaining.get(order_id)
        if rem is None:
            return  # это не наш ордер, менеджер раздал филл всем подряд

        rem = float(rem) - float(qty)
        if rem <= 1e-12:
            self._own_order_remaining.pop(order_id, None)
        else:
            self._own_order_remaining[order_id] = rem

        notional = price * qty

        if side == OrderSide.BID:
            self.position += qty
            self.cash -= notional
        else:
            self.position -= qty
            self.cash += notional

        abs_notional = abs(notional)
        self.executed_notional += abs_notional

        # обновление собственного VWAP
        if abs_notional > 0:
            if self.vwap_price is None:
                self.vwap_price = price
                self.vwap_notional = abs_notional
            else:
                total = self.vwap_notional + abs_notional
                self.vwap_price = (
                    self.vwap_price * self.vwap_notional + price * abs_notional
                ) / total
                self.vwap_notional = total

        if self.target_notional and self.executed_notional >= self.target_notional * 0.995:
            self.completed = True

    # ============================================================
    def restore_capital(self):
        # Корпораты капитально не реинвестируют, тут заглушка
        pass

    def perceive_market(self, market_context):
        # Для логов/диагностики — показываем стиль и профиль
        return f"corp_exec_{self.style}_p{self.profile_id}"
