# risk_parity_vol_control_fund.py
import time
import math
import random
from typing import List, Optional, Tuple

from backend.core.order import Order, OrderSide, OrderType


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


def _safe_float(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


class RiskParityVolControlFund:
    """
    Risk-Control / Vol-Target фонд (один инструмент).

    Механика:
      - оценивает реализованную волатильность по трейдам
      - масштабирует экспозицию inversely to vol (vol targeting)
      - направление задаёт простым трендовым фильтром (чтобы был "смысл" держать риск)
      - в стресс-режиме (vol spike) режет позицию агрессивно (market)
      - в обычном режиме ребалансит мягко (limit near touch)

    Совместимость:
      generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]
      on_order_filled(self, order_id, price, qty, side, slippage=0.0)
    """

    def __init__(
        self,
        agent_id: str = "risk_control_fund",
        capital: float = 25_000_000.0,

        # --- vol control params ---
        # CALIB: EUR sim realized sigma per trade ~0.0045 (price units, from 1m range 0.57 / sqrt(ticks_per_min)).
        # Old target_vol=0.0018 was calibrated to old sim where vol was 4x smaller.
        # New target = 0.0018 * 4 = 0.0072. Agent now correctly identifies normal vs stress regimes.
        target_vol: float = 0.0647,          # CALIB: was 0.0018

        vol_window_sec: float = 75.0,
        trend_window_sec: float = 240.0,

        min_leverage: float = 0.0,
        max_leverage: float = 6.0,

        # --- allocation / risk budget ---
        risk_budget_frac: float = 0.22,
        base_alloc: float = 1.0,
        max_pos_frac_of_book: float = 0.35,

        # --- execution / rebalance ---
        rebalance_interval_sec: float = 1.2,
        # CALIB: min_rebalance_qty scales with lot size.
        # EUR min meaningful rebalance ~0.007 lot = ~78 contracts.
        min_rebalance_qty: float = 80.0,     # CALIB: was 25 → ~0.007 lot at EUR scale

        rebalance_band_frac: float = 0.07,

        # стресс-режим
        shock_mult: float = 2.5,
        shock_cut_speed: float = 0.55,
        # CALIB: shock_slice_max = 0.7 lot = ~7868 contracts.
        # Old 2200 contracts = 0.2 lot — too small to register as meaningful EUR flow.
        shock_slice_max: float = 7800.0,     # CALIB: was 2200 → ~0.7 lot

        # обычный режим
        # CALIB: normal_slice_max = 0.44 lot = ~4950 contracts.
        # Old 1400 = 0.12 lot — fine for retail, too small for vol-control fund.
        normal_slice_max: float = 4900.0,    # CALIB: was 1400 → ~0.44 lot

        ttl_sec: float = 2.0,
        jitter: float = 0.12,
        tick_size: float = 0.01,
    ):
        self.agent_id = agent_id
        self.capital = float(capital)

        self.target_vol = float(target_vol)
        self.vol_window_sec = float(vol_window_sec)
        self.trend_window_sec = float(trend_window_sec)

        self.min_leverage = float(min_leverage)
        self.max_leverage = float(max_leverage)

        self.risk_budget_frac = float(risk_budget_frac)
        self.base_alloc = float(base_alloc)
        self.max_pos_frac_of_book = float(max_pos_frac_of_book)

        self.rebalance_interval_sec = float(rebalance_interval_sec)
        self.min_rebalance_qty = float(min_rebalance_qty)
        self.rebalance_band_frac = float(rebalance_band_frac)

        self.shock_mult = float(shock_mult)
        self.shock_cut_speed = float(shock_cut_speed)
        self.shock_slice_max = float(shock_slice_max)

        self.normal_slice_max = float(normal_slice_max)
        self.ttl_sec = float(ttl_sec)

        self.jitter = float(jitter)
        self.tick_size = float(tick_size)

        # state
        self.position_qty = 0.0
        self.avg_price = 0.0  # для контроля (не обязателен)
        self._seq = 0
        self._last_rebalance_ts = 0.0

    # -------------------- internals --------------------

    def _next_oid(self) -> str:
        self._seq += 1
        return f"{self.agent_id}_{self._seq}"

    def _mid_price(self, order_book) -> Optional[float]:
        bid = getattr(order_book, "_best_bid_price", lambda: None)()
        ask = getattr(order_book, "_best_ask_price", lambda: None)()
        if bid is not None and ask is not None:
            return (float(bid) + float(ask)) * 0.5
        last = getattr(order_book, "last_trade_price", None)
        if last is not None:
            return float(last)
        if bid is not None:
            return float(bid)
        if ask is not None:
            return float(ask)
        return None

    def _recent_prices(self, order_book, window_sec: float) -> List[Tuple[float, float]]:
        """
        Возвращает список (ts, price) из trade_history за окно.
        trade_history у тебя хранит dict с 'price' и 'timestamp'. :contentReference[oaicite:3]{index=3}
        """
        th = getattr(order_book, "trade_history", None)
        if not th:
            return []
        now = time.time()
        cut = now - window_sec
        out: List[Tuple[float, float]] = []
        # идём с конца — быстрее вырезаем окно
        for t in reversed(th):
            ts = _safe_float(t.get("timestamp"), 0.0)
            if ts < cut:
                break
            px = _safe_float(t.get("price"), 0.0)
            if px > 0 and ts > 0:
                out.append((ts, px))
        out.reverse()
        return out

    def _realized_sigma(self, series: List[Tuple[float, float]]) -> float:
        """
        sigma по лог-доходностям (без годовых коэффициентов — чисто "внутренняя" sigma на твоём масштабе времени).
        """
        if len(series) < 6:
            return 0.0
        rets: List[float] = []
        prev = series[0][1]
        for _, px in series[1:]:
            if px <= 0 or prev <= 0:
                prev = px
                continue
            r = math.log(px / prev)
            rets.append(r)
            prev = px
        if len(rets) < 5:
            return 0.0
        m = sum(rets) / len(rets)
        var = 0.0
        for r in rets:
            d = r - m
            var += d * d
        var /= max(1, (len(rets) - 1))
        return math.sqrt(max(var, 0.0))

    def _trend_signal(self, series: List[Tuple[float, float]]) -> float:
        """
        Простой тренд: знак изменения цены между началом и концом окна,
        усиленный отношением к "шуму" (sigma).
        """
        if len(series) < 8:
            return 0.0
        p0 = series[0][1]
        p1 = series[-1][1]
        if p0 <= 0 or p1 <= 0:
            return 0.0
        drift = math.log(p1 / p0)
        sigma = self._realized_sigma(series)
        # нормируем дрейф на sigma, но мягко
        if sigma <= 1e-12:
            return drift
        return drift / sigma

    def _target_position_qty(
        self,
        mid: float,
        sigma: float,
        trend_z: float,
        market_context=None
    ) -> float:
        """
        Целевая позиция:
          - size: (capital * risk_budget_frac * base_alloc) * leverage / mid
          - leverage: target_vol / max(sigma, floor)
          - direction: sign(trend_z) с гистерезисом; если тренда нет — держим 0..малую экспозицию.
        """
        if mid <= 0:
            return 0.0

        sigma_floor = max(self.target_vol * 0.35, 1e-8)
        eff_sigma = max(sigma, sigma_floor)

        raw_lev = self.target_vol / eff_sigma
        lev = _clamp(raw_lev, self.min_leverage, self.max_leverage)

        # Небольшая рандомизация вокруг левереджа, чтобы не было "ступеньками одинаково"
        lev *= (1.0 + random.uniform(-self.jitter, self.jitter) * 0.25)
        lev = _clamp(lev, self.min_leverage, self.max_leverage)

        # Direction:
        # - если тренд сильный: следуем
        # - если слабый: не обязаны держать риск (это убирает вечный "лонг ради лонга")
        dir_sign = 0.0
        if trend_z > 0.55:
            dir_sign = 1.0
        elif trend_z < -0.55:
            dir_sign = -1.0
        else:
            dir_sign = 0.0

        # Можно подключать market_context, если у тебя там есть режим/фаза.
        # Если в market_context есть что-то вроде market_context.phase / trend, можно аккуратно сместить.
        if market_context is not None:
            phase = getattr(market_context, "phase", None) or getattr(market_context, "regime", None)
            if isinstance(phase, str):
                ph = phase.lower()
                if "risk_off" in ph or "crisis" in ph:
                    dir_sign *= 0.5  # в risk-off снижаем направление (не обязательно меняем знак)
                if "trend_down" in ph and dir_sign > 0:
                    dir_sign *= 0.5
                if "trend_up" in ph and dir_sign < 0:
                    dir_sign *= 0.5

        notional_budget = self.capital * self.risk_budget_frac * self.base_alloc
        # Если направления нет — держим очень малую экспозицию (фонды часто "паркуются")
        if dir_sign == 0.0:
            notional_budget *= 0.12

        target_notional = notional_budget * lev * dir_sign
        target_qty = target_notional / mid

        return float(target_qty)

    def _rebalance_needed(self, target_qty: float) -> Tuple[bool, float]:
        delta = target_qty - self.position_qty
        abs_delta = abs(delta)
        band = max(self.min_rebalance_qty, abs(target_qty) * self.rebalance_band_frac)
        if abs_delta < band:
            return False, 0.0
        return True, delta

    def _make_limit_price(self, order_book, side: OrderSide) -> Optional[float]:
        bid = getattr(order_book, "_best_bid_price", lambda: None)()
        ask = getattr(order_book, "_best_ask_price", lambda: None)()
        if bid is None and ask is None:
            return None
        bid = float(bid) if bid is not None else None
        ask = float(ask) if ask is not None else None

        # near-touch, но чуть внутри, чтобы не всегда быть тейкером
        if side == OrderSide.BID:
            if bid is None:
                return None
            px = bid  # можно bid + tick, но тогда чаще кросс и станет агрессивным
            return round(px / self.tick_size) * self.tick_size
        else:
            if ask is None:
                return None
            px = ask
            return round(px / self.tick_size) * self.tick_size

    # -------------------- public API --------------------

    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        now = time.time()
        if now - self._last_rebalance_ts < self.rebalance_interval_sec:
            return []

        mid = self._mid_price(order_book)
        if mid is None or mid <= 0:
            return []

        vol_series = self._recent_prices(order_book, self.vol_window_sec)
        sigma = self._realized_sigma(vol_series)

        trend_series = self._recent_prices(order_book, self.trend_window_sec)
        trend_z = self._trend_signal(trend_series)

        target_qty = self._target_position_qty(mid=mid, sigma=sigma, trend_z=trend_z, market_context=market_context)

        need, delta = self._rebalance_needed(target_qty)
        if not need:
            self._last_rebalance_ts = now
            return []

        stress = sigma > (self.shock_mult * self.target_vol) and sigma > 0.0

        orders: List[Order] = []

        # сколько хотим сделать в этот тик (частичный ребаланс)
        if stress:
            # в стресс — режем быстро (это и даёт твои "пули" на минутках)
            take = _clamp(abs(delta) * self.shock_cut_speed, self.min_rebalance_qty, self.shock_slice_max)
            exec_qty = take if delta > 0 else -take
            order_type = OrderType.MARKET
            price = None
        else:
            # в норме — мягче
            take = _clamp(abs(delta) * (0.35 + random.uniform(-self.jitter, self.jitter) * 0.1),
                          self.min_rebalance_qty, self.normal_slice_max)
            exec_qty = take if delta > 0 else -take
            order_type = OrderType.LIMIT
            side_tmp = OrderSide.BID if exec_qty > 0 else OrderSide.ASK
            price = self._make_limit_price(order_book, side_tmp)
            if price is None:
                # если вдруг нет котировок — fallback в маркет, но без истерики
                order_type = OrderType.MARKET
                price = None

        side = OrderSide.BID if exec_qty > 0 else OrderSide.ASK
        qty = abs(exec_qty)

        # TTL чтобы не висели и не превращались в мусор
        ttl = self.ttl_sec if order_type == OrderType.LIMIT else None

        oid = self._next_oid()
        orders.append(Order(
            order_id=oid,
            agent_id=self.agent_id,
            side=side,
            volume=float(qty),
            price=price,
            order_type=order_type,
            ttl=ttl,
            metadata={
                "reason": "vol_control_rebalance",
                "sigma": float(sigma),
                "target_vol": float(self.target_vol),
                "trend_z": float(trend_z),
                "stress": bool(stress),
            }
        ))

        self._last_rebalance_ts = now
        return orders

    def on_order_filled(self, order_id, price, qty, side, slippage: float = 0.0):
        px = _safe_float(price, 0.0)
        q = abs(_safe_float(qty, 0.0))
        if px <= 0 or q <= 0:
            return

        # Обновляем позицию
        if side == OrderSide.BID:
            new_pos = self.position_qty + q
            # avg price update (простая)
            if self.position_qty >= 0:
                self.avg_price = (self.avg_price * abs(self.position_qty) + px * q) / max(abs(new_pos), 1e-12)
            else:
                # закрытие/переворот — avg можно сбросить аккуратно
                if new_pos > 0:
                    self.avg_price = px
            self.position_qty = new_pos

        else:  # ASK
            new_pos = self.position_qty - q
            if self.position_qty <= 0:
                self.avg_price = (self.avg_price * abs(self.position_qty) + px * q) / max(abs(new_pos), 1e-12)
            else:
                if new_pos < 0:
                    self.avg_price = px
            self.position_qty = new_pos