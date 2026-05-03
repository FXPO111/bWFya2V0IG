# is_pressure_executor.py  — PATCHED
#
# ═══════════════════════════════════════════════════════════════════
#  СВОДКА ИЗМЕНЕНИЙ (6 патчей):
#
#  [FIX-1] _signal: добавлен mean-reversion компонент
#          → устраняет залипание direction=-1 в тренде
#
#  [FIX-2] _signal: momentum переработан (fast + slow + vol-norm)
#          → убирает шумовой сигнал на основе 8 тиков
#
#  [FIX-3] fatigue + cooldown: усилены коэффициенты,
#          добавлен hard volume cap per window
#          → создаёт органичные паузы и ограничивает давление
#
#  [FIX-4] _risk_scale: EWMA alpha 0.03 → 0.15, откалиброваны пороги
#          → риск-контроль реально включается при накоплении позиции
#
#  [FIX-5] spread_gate_ticks: 10 → 3 (+ dynamic gate)
#          → агент не молотит во время стресса ликвидности
#
#  [FIX-6] restore_capital: реализован частичный сброс позиции
#          → убирает бесконечное накопление перекоса
# ═══════════════════════════════════════════════════════════════════

import random
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from backend.core.order import Order, OrderSide, OrderType

TICK = 0.001


def _best_prices(order_book) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    bb = order_book._best_bid_price()
    ba = order_book._best_ask_price()
    if bb is None or ba is None:
        return bb, ba, None, None
    mid = 0.5 * (bb + ba)
    spr = ba - bb
    return bb, ba, mid, spr


def _trade_sign(t: Dict[str, Any]) -> float:
    s = str(t.get("taker_side", "")).lower()
    if s in ("buy", "bid"):
        return +1.0
    if s in ("sell", "ask"):
        return -1.0
    return 0.0


class ISPressureExecutor:
    """
    Taker-only: генерит серии MARKET-ударов.
    Никаких лимиток.
    """

    def __init__(
        self,
        agent_id: str,
        capital: float,
        window_trades: int = 80,
        max_child_orders: int = 3,
        # [FIX-5] было 10 — слишком широко, агент торговал при любом спреде.
        # 3 тика = 0.03, что уже в ~1.5× от нормального спреда симa (0.02).
        spread_gate_ticks: int = 3,
        # [FIX-3] новый параметр: hard cap на suммарный notional за окно времени.
        # Предотвращает бесконечное давление независимо от fatigue.
        volume_window_sec: float = 60.0,
        volume_cap_frac: float = 0.05,        # CALIB: was 0.015 → 0.05 (3x, EUR flow is continuous)
        # [FIX-1] параметры mean-reversion сигнала
        mean_rev_lookback: int = 60,           # сколько mid_hist точек для расчёта "fair value"
        mean_rev_weight: float = 0.30,         # вес mean-rev в итоговом raw сигнале
        mean_rev_threshold_ticks: float = 3.0, # минимальное отклонение для активации (тиков)
    ):
        self.agent_id = str(agent_id)
        self.capital = float(capital)

        self.window_trades = int(window_trades)
        self.max_child_orders = int(max_child_orders)
        self.spread_gate_ticks = int(spread_gate_ticks)

        # [FIX-1] mean-reversion параметры
        self.mean_rev_lookback = int(mean_rev_lookback)
        self.mean_rev_weight = float(mean_rev_weight)
        self.mean_rev_threshold_ticks = float(mean_rev_threshold_ticks)

        # [FIX-3] volume cap: храним историю отправленного объёма с таймстемпами
        self.volume_window_sec = float(volume_window_sec)
        self.volume_cap_notional = float(capital) * float(volume_cap_frac)
        self._vol_hist: deque[Tuple[float, float]] = deque()  # (timestamp, notional)
        self._last_ts: float = 0.0  # последний известный timestamp

        self.trade_hist: deque[Dict[str, Any]] = deque(maxlen=2000)
        # [FIX-2] расширен буфер: нужно 40 точек для slow momentum
        self.mid_hist: deque[float] = deque(maxlen=400)

        # [FIX-5] rolling avg спреда для dynamic gate
        self.spr_hist: deque[float] = deque(maxlen=100)

        self.cooldown = 0
        self.fatigue = 0.0
        self.last_dir = 0

        self.signed_pos_qty = 0.0
        self.signed_pos_notional_ewma = 0.0

    def _ingest_trades(self, order_book):
        try:
            for t in list(order_book.trade_history)[-256:]:
                self.trade_hist.append(t)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    # [FIX-4] _risk_scale
    #
    # БЫЛО:  EWMA alpha = 0.03 → сходится за ~100 вызовов,
    #        реальная позиция отражалась с огромной задержкой.
    #        Пороги util 0.02/0.05/0.10 — при капитале ~766k
    #        нужна позиция >15k notional чтобы выйти из scale=1.0,
    #        а при alpha=0.03 EWMA просто не успевала туда доползти.
    #
    # СТАЛО: alpha = 0.15 → сходится за ~20 вызовов (в 5× быстрее).
    #        Пороги снижены: 0.01/0.03/0.07 — scale начинает работать
    #        при накоплении ~1% капитала в позиции.
    # ─────────────────────────────────────────────────────────────────
    def _risk_scale(self, mid: float) -> float:
        pos_notional = self.signed_pos_qty * mid
        # [FIX-4] alpha 0.03 → 0.15
        self.signed_pos_notional_ewma = 0.85 * self.signed_pos_notional_ewma + 0.15 * pos_notional
        util = abs(self.signed_pos_notional_ewma) / max(self.capital, 1.0)

        # [FIX-4] пороги опущены чтобы scale реально включался
        if util < 0.01:
            return 1.0
        if util < 0.03:
            return 0.65
        if util < 0.07:
            return 0.35
        return 0.12

    # ─────────────────────────────────────────────────────────────────
    # [FIX-1] + [FIX-2] _signal
    #
    # БЫЛО:  raw = 0.75*imb + 0.25*mom
    #        - imb берёт историю рынка где агент сам продаёт → залипает на -1
    #        - mom = mid[-1] - mid[-8] = ~десятки мс = шум
    #        - нет компонента возврата к среднему
    #
    # СТАЛО: raw = w_imb*imb + w_mom*mom_combined + w_rev*mean_rev
    #
    #   imb:          без изменений (рыночный дисбаланс)
    #   mom_combined: 0.5*fast_mom + 0.5*slow_mom, нормированные
    #                 на rolling std mid — не saturated при волатильности
    #   mean_rev:     когда цена отклонилась от rolling avg вниз >N тиков
    #                 → добавляет положительный (buy) bias.
    #                 Это главный источник органичных откатов.
    # ─────────────────────────────────────────────────────────────────
    def _signal(self, order_book) -> Tuple[int, float]:
        bb, ba, mid, spr = _best_prices(order_book)
        if mid is None or spr is None or spr <= 0:
            return 0, 0.0

        # [FIX-5] dynamic spread gate
        self.spr_hist.append(float(spr))
        avg_spr = sum(self.spr_hist) / len(self.spr_hist)
        spr_ticks = spr / TICK
        # статический gate: не торгуем при спреде > 3 тиков
        if spr_ticks > self.spread_gate_ticks:
            return 0, 0.0
        # dynamic gate: если текущий спред > 2.5× rolling avg — пауза
        if len(self.spr_hist) >= 20 and spr > 2.5 * avg_spr:
            return 0, 0.0

        self.mid_hist.append(float(mid))
        n_mid = len(self.mid_hist)

        # ── [FIX-2] Momentum: fast + slow, нормированный на волатильность ──
        mom_combined = 0.0
        if n_mid >= 8:
            # fast: последние 8 точек книги (~100–500 мс)
            fast_raw = self.mid_hist[-1] - self.mid_hist[-8]
            # slow: последние 40 точек (~1–3 сек, реальный тренд)
            slow_raw = (self.mid_hist[-1] - self.mid_hist[-40]) if n_mid >= 40 else fast_raw

            # rolling std для нормировки (не даём saturate при любой волатильности)
            window_for_std = list(self.mid_hist)[-20:]
            if len(window_for_std) >= 4:
                mean_w = sum(window_for_std) / len(window_for_std)
                std_w = (sum((x - mean_w) ** 2 for x in window_for_std) / len(window_for_std)) ** 0.5
                std_w = max(std_w, TICK)
            else:
                std_w = max(spr, TICK)

            fast_mom = max(-1.0, min(1.0, fast_raw / (4.0 * std_w)))
            slow_mom = max(-1.0, min(1.0, slow_raw / (8.0 * std_w)))
            mom_combined = 0.5 * fast_mom + 0.5 * slow_mom

        # ── [FIX-1] Mean-reversion компонент ──
        mean_rev = 0.0
        if n_mid >= self.mean_rev_lookback:
            # "fair value" = rolling average за lookback точек
            fair = sum(list(self.mid_hist)[-self.mean_rev_lookback:]) / self.mean_rev_lookback
            deviation_ticks = (fair - mid) / TICK  # >0 → цена ниже fair, ждём отскок вверх
            # активируем только при отклонении > threshold
            if abs(deviation_ticks) >= self.mean_rev_threshold_ticks:
                # нормируем: при отклонении в 3× threshold → mean_rev = ±1.0
                mean_rev = max(-1.0, min(1.0, deviation_ticks / (3.0 * self.mean_rev_threshold_ticks)))

        # ── Рыночный дисбаланс (imb) — без изменений ──
        w = list(self.trade_hist)[-self.window_trades:]
        imb = 0.0
        if w:
            signed = 0.0
            tot = 0.0
            for t in w:
                v = float(t.get("volume", 0.0) or 0.0)
                signed += _trade_sign(t) * v
                tot += v
            imb = (signed / tot) if tot > 0 else 0.0

        # ── Итоговый сигнал ──
        # [FIX-1] перераспределили веса: убрали 25% от mom, добавили mean_rev
        # imb: 0.75→0.50 (всё ещё основной, но не диктует всё)
        # mom: 0.25→0.20 (уменьшили т.к. теперь он качественнее)
        # mean_rev: 0→0.30 (новый, самый важный для pullback'ов)
        w_imb = 1.0 - self.mean_rev_weight - 0.20
        raw = w_imb * imb + 0.20 * mom_combined + self.mean_rev_weight * mean_rev

        strength = min(1.0, max(0.0, abs(raw)))
        if strength < 0.12:
            return 0, strength

        direction = +1 if raw > 0 else -1
        return direction, strength

    def _mk_market(self, side: OrderSide, qty: float) -> Order:
        return Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            volume=float(max(0.0, qty)),
            price=None,
            order_type=OrderType.MARKET,
            ttl=None,
        )

    # ─────────────────────────────────────────────────────────────────
    # [FIX-3] generate_orders: fatigue + cooldown + volume cap
    #
    # БЫЛО:  fatigue weight в p_fire = 0.55 → при max fatigue p=28%
    #        decay = 0.06/тик, cooldown 1-3 тика → обнуляется за секунды
    #        нет ограничения на суммарный объём за период
    #
    # СТАЛО:
    #   - fatigue weight 0.55 → 0.90: при max fatigue p=6% (почти стоп)
    #   - decay 0.06 → 0.015/тик (в 4× медленнее)
    #   - cooldown: 3-12 тиков вместо 1-3
    #   - hard volume cap: если сумма notional за последние
    #     volume_window_sec > volume_cap_notional → полная пауза
    # ─────────────────────────────────────────────────────────────────
    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        self._ingest_trades(order_book)

        bb, ba, mid, spr = _best_prices(order_book)
        if mid is None:
            return []

        # [FIX-3] обновляем timestamp (берём из контекста или используем счётчик)
        ts = float(getattr(market_context, "timestamp", None) or
                   getattr(order_book, "timestamp", None) or
                   self._last_ts + 0.1)
        self._last_ts = ts

        # [FIX-3] hard volume cap: суммируем notional за окно, удаляем старые
        while self._vol_hist and self._vol_hist[0][0] < ts - self.volume_window_sec:
            self._vol_hist.popleft()
        vol_in_window = sum(n for _, n in self._vol_hist)
        if vol_in_window >= self.volume_cap_notional:
            # объём исчерпан — принудительная пауза, fatigue растёт медленно
            self.fatigue = min(1.0, self.fatigue + 0.01)
            return []

        if self.cooldown > 0:
            self.cooldown -= 1
            # [FIX-3] decay 0.06 → 0.015: усталость уходит в 4× медленнее
            self.fatigue = max(0.0, self.fatigue - 0.015)
            return []

        direction, strength = self._signal(order_book)
        if direction == 0:
            # [FIX-3] decay 0.04 → 0.010 при отсутствии сигнала
            self.fatigue = max(0.0, self.fatigue - 0.010)
            return []

        # усталость нарастает при повторном направлении
        if direction == self.last_dir:
            self.fatigue = min(1.0, self.fatigue + 0.08 * strength)
        else:
            self.fatigue = self.fatigue * 0.65

        self.last_dir = direction

        # [FIX-3] weight 0.55 → 0.90: при fatigue=1.0 p_fire ≈ 6% (против 28% было)
        p_fire = (0.18 + 0.62 * strength) * (1.0 - 0.90 * self.fatigue)
        p_fire = min(0.85, max(0.02, p_fire))
        if random.random() > p_fire:
            return []

        risk = self._risk_scale(mid)

        # CALIB: original frac 0.00003-0.00025 → 0.00012-0.00100 (4x).
        # EUR institutional flow = 0.1-1 lot = 1124-11240 contracts per burst.
        # At capital=250M: 250M * 0.0009 = 225k notional = 2250 contracts = 0.2 lot ✓
        frac = (0.00012 + 0.00088 * strength) * risk
        frac = min(frac, 0.00110)         # CALIB: was 0.00028 → 0.00110

        notional = self.capital * frac
        qty = max(0.5, notional / max(mid, 1e-9))

        n_child = 1 + (1 if random.random() < 0.55 else 0) + (1 if random.random() < 0.25 else 0)
        n_child = max(1, min(self.max_child_orders, n_child))

        side = OrderSide.BID if direction > 0 else OrderSide.ASK
        out: List[Order] = []
        total_notional_sent = 0.0
        for _ in range(n_child):
            q = (qty / n_child) * random.uniform(0.7, 1.25)
            if q < 0.25:
                continue
            out.append(self._mk_market(side, q))
            total_notional_sent += q * mid

        # [FIX-3] записываем отправленный объём в историю cap'а
        if total_notional_sent > 0:
            self._vol_hist.append((ts, total_notional_sent))

        # CALIB: tighter cooldown was dampening flow. EUR IS flow is near-continuous.
        if strength > 0.55 and random.random() < 0.35:
            self.cooldown = random.randint(2, 6)   # was 4-12
        elif random.random() < 0.18:
            self.cooldown = random.randint(1, 3)   # was 2-5

        return out

    def on_order_filled(self, order_id: str, price: float, qty: float, side: OrderSide, slippage: float = 0.0):
        q = float(qty)
        signed = q if side == OrderSide.BID else -q
        self.signed_pos_qty += signed
        self.fatigue = min(1.0, self.fatigue + 0.04)

    # ─────────────────────────────────────────────────────────────────
    # [FIX-6] restore_capital
    #
    # БЫЛО:  pass — позиция никогда не сбрасывалась.
    #        Накопленный signed_pos_qty уходил в бесконечность,
    #        EWMA тоже, и risk_scale теоретически давил до 0.15 навсегда.
    #
    # СТАЛО: частичный decay позиции к нулю (50% за вызов).
    #        Не телепортирует позицию в 0 резко — плавный сброс.
    #        Вызывать из симуляции периодически (например, раз в N секунд
    #        или при смене торговой сессии).
    # ─────────────────────────────────────────────────────────────────
    def restore_capital(self, decay: float = 0.50):
        """
        Частичный сброс накопленной позиции.

        decay=0.50 означает: signed_pos_qty *= (1 - 0.50) = уменьшается вдвое.
        Вызывать периодически из внешнего планировщика симуляции.
        """
        self.signed_pos_qty *= (1.0 - float(decay))
        # EWMA тоже сбрасываем пропорционально — иначе risk_scale
        # будет ещё долго "помнить" старую большую позицию
        self.signed_pos_notional_ewma *= (1.0 - float(decay))

    def perceive_market(self, market_context) -> str:
        return "neutral"