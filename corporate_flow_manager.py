# corporate_flow_manager.py
# Realistic Corporate Flow Manager — 8–25 потоков, сбалансированный net flow,
# без тренда, без фазы рынка, без "random opposite", чистый корпоративный стиль.

import time
import random
import math
from typing import List, Tuple

from corporate_flow_twap import CorporateFlowTWAP
from order import OrderSide

def _ctx_now(ctx):
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()

class CorporateFlowManager:
    """
    Реалистичный корпоративный менеджер:
      - 8–25 одновременно живущих исполнителей
      - направление не зависит от тренда/фазы
      - контролируется net-flow (buy vs sell)
      - генерация долгоживущих крупных задач (как EUR/USD)
      - исполнители внутри (TWAP/VWAP/POV/Opportunistic/IS)
    """

    def __init__(self, agent_id: str, capital_pool: float):
        self.agent_id = agent_id
        self.capital_pool = float(capital_pool)

        # активные корпоративные исполнения
        self.active_agents: List[CorporateFlowTWAP] = []

        # жесткий лимит
        self.max_flows = 25

        # контроль момента обновления
        self.next_check_ts = time.time() + random.uniform(4.0, 8.0)

        # DEBUG/telemetry (теперь это "живые остатки", а не накопление с начала)
        self.net_buy_notional = 0.0
        self.net_sell_notional = 0.0

    # -------------------------------------------------------------
    def _get_session(self, market_context, now: float):
        hour = getattr(market_context, "utc_hour", None) if market_context is not None else None
        if hour is None:
            hour = time.gmtime(now).tm_hour  # UTC fallback
        if 0 <= hour < 6:
            return "asia"
        if 6 <= hour < 13:
            return "london"
        if 13 <= hour < 21:
            return "us"
        return "late_us"

    # -------------------------------------------------------------
    # Живой remaining-notional по активным потокам
    # -------------------------------------------------------------
    def _agent_remaining(self, agent: CorporateFlowTWAP) -> float:
        try:
            tgt = float(agent.target_notional or 0.0)
        except Exception:
            tgt = 0.0
        try:
            exe = float(getattr(agent, "executed_notional", 0.0) or 0.0)
        except Exception:
            exe = 0.0
        rem = tgt - exe
        return rem if rem > 0.0 else 0.0

    def _recompute_live_net(self) -> Tuple[float, float]:
        buy_rem = 0.0
        sell_rem = 0.0

        for a in self.active_agents:
            # вычищаем только логикой cleanup, здесь просто считаем
            rem = self._agent_remaining(a)
            if rem <= 0.0:
                continue

            if getattr(a, "side_bias", None) == OrderSide.BID:
                buy_rem += rem
            elif getattr(a, "side_bias", None) == OrderSide.ASK:
                sell_rem += rem

        # сохраняем для мониторинга
        self.net_buy_notional = buy_rem
        self.net_sell_notional = sell_rem
        return buy_rem, sell_rem

    # -------------------------------------------------------------
    # Контроль направления: только по живому дисбалансу (remaining)
    # -------------------------------------------------------------
    def _pick_direction(self) -> OrderSide:
        """
        Сторона определяется ТОЛЬКО балансом активных корпоративных остатков.
        Никакого влияния рынка.
        """
        buy_rem, sell_rem = self._recompute_live_net()

        tot = buy_rem + sell_rem
        if tot <= 1e-9:
            # если сейчас нет активного корпоративного остатка — 50/50
            return OrderSide.BID if random.random() < 0.5 else OrderSide.ASK

        imb = (buy_rem - sell_rem) / max(tot, 1e-9)  # + => перекос в BUY-остаток

        # если перекос экстремальный — принудительно контр-сторона
        if imb > 0.35:
            return OrderSide.ASK
        if imb < -0.35:
            return OrderSide.BID

        # мягкий стохастический контрбаланс (без "скрипта" 50/50)
        # +imb => повышаем вероятность SELL, -imb => повышаем вероятность BUY
        p_buy = 0.5 - 0.35 * math.tanh(imb * 3.0)

        # легкий шум, чтобы не получались механические серии
        p_buy += random.uniform(-0.03, 0.03)

        # ограничение, чтобы не зажимало в 0/1 надолго
        p_buy = max(0.05, min(0.95, p_buy))

        return OrderSide.BID if random.random() < p_buy else OrderSide.ASK

    # -------------------------------------------------------------
    def _desired_flow_count(self, market_context, now: float):
        session = self._get_session(market_context, now)

        if session == "asia":
            return random.randint(4, 10)
        if session == "london":
            return random.randint(14, 25)
        if session == "us":
            return random.randint(12, 22)
        if session == "late_us":
            return random.randint(6, 14)

        return random.randint(8, 20)

    # -------------------------------------------------------------
    def _spawn_flow(self, market_context, now: float):
        session = self._get_session(market_context, now)

        # распределяем капитал на поток
        if session == "asia":
            cap_frac = random.uniform(0.01, 0.05)
        elif session == "london":
            cap_frac = random.uniform(0.02, 0.10)
        elif session == "us":
            cap_frac = random.uniform(0.02, 0.08)
        else:  # late_us
            cap_frac = random.uniform(0.01, 0.06)

        twap_capital = self.capital_pool * cap_frac

        # направление только по live net (remaining)
        side = self._pick_direction()

        agent = CorporateFlowTWAP(
            agent_id=f"{self.agent_id}_flow_{random.randint(1000, 9999)}",
            capital=twap_capital,
        )

        # целевой объём (по FX-реалиям: 1–6% от выделенного капитала)
        target_frac = random.uniform(0.01, 0.06)
        target_notional = twap_capital * target_frac

        # длительность (реалистично)
        if session == "london":
            dur = random.uniform(18, 50)
        elif session == "us":
            dur = random.uniform(16, 45)
        else:
            dur = random.uniform(12, 35)

        agent.configure(
            side_bias=side,
            target_notional=target_notional,
            session_minutes=dur,
            now=getattr(self, "_now_ts", None),
        )

        self.active_agents.append(agent)

        # обновим телеметрию
        self._recompute_live_net()

    # -------------------------------------------------------------
    def _cleanup(self):
        now = getattr(self, "_now_ts", time.time())
        cleaned = []
        for agent in self.active_agents:
            try:
                if agent.completed:
                    continue
                if now > agent.session_end + 2.0:
                    continue
                rem = self._agent_remaining(agent)
                if rem <= 0.0:
                    continue
            except Exception:
                continue
            cleaned.append(agent)
        self.active_agents = cleaned

        # после чистки пересчитываем live net
        self._recompute_live_net()

    # -------------------------------------------------------------
    def generate_orders(self, order_book, market_context=None, **kwargs):
        now = _ctx_now(market_context)
        self._now_ts = now

        if now >= self.next_check_ts:
            self.next_check_ts = now + random.uniform(4.0, 7.0)

            self._cleanup()

            want = self._desired_flow_count(market_context, now)

            # спавним только если не хватает
            while len(self.active_agents) < want and len(self.active_agents) < self.max_flows:
                self._spawn_flow(market_context, now)

            # ВАЖНО: не "pop старейших".
            # В реале корпоративные программы не отменяются менеджером только потому,
            # что "сессия поменялась". Снижаем активность естественно: просто не спавним новые.

        all_orders = []
        for agent in list(self.active_agents):
            try:
                orders = agent.generate_orders(order_book, market_context)
                if orders:
                    all_orders.extend(orders)
            except Exception:
                pass

        return all_orders

    # -------------------------------------------------------------
    def on_order_filled(self, order_id, price, qty, side, slippage=0.0):
        for agent in self.active_agents:
            try:
                agent.on_order_filled(order_id, price, qty, side, slippage)
            except Exception:
                pass

        # после филлов тоже обновляем live net, чтобы быстрее реагировать
        self._recompute_live_net()

    def restore_capital(self):
        pass

    def perceive_market(self, market_context):
        return "corp_flow_manager"
