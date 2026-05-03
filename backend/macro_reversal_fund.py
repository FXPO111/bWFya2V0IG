import uuid
import time
import math
import random
from collections import deque
from typing import Dict, Any, List, Optional, Tuple

from order import Order, OrderSide, OrderType, quant

TICK = 0.01


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class EMA:
    def __init__(self, alpha: float, init: float = 0.0):
        self.alpha = float(alpha)
        self.value = float(init)
        self.inited = False

    def update(self, x: float) -> float:
        x = float(x)
        if not self.inited:
            self.value = x
            self.inited = True
        else:
            self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value


class MacroReversalFund:
    """
    Macro Reversal Fund (институциональный агент) — ГИБРИД:

    MACRO слой:
      - ловит устойчивое "перекос/перегрев тренда" и делает контр-кампании
      - несколько независимых саб-фондов, с лагом старта и долгим горизонтом
      - смешанное исполнение (limit/market), participation cap от оборота

    MICRO слой:
      - небольшие тактические контр-удары на экстремуме агрессии/отклонения
      - короткий горизонт, маленький notional, строгие лимиты и cooldown

    Важно:
      - есть прогрев сигналов (warmup), чтобы не "хуярить с ходу" на первых тиках.
      - интерфейсы не меняются (generate_orders / on_order_filled).
    """

    def __init__(self, agent_id: str, capital: float, num_subfunds: int = 3):
        self.agent_id = str(agent_id)
        self.capital = float(capital)

        self.inventory: float = 0.0
        self.cash: float = float(capital)

        # внутренние кампании
        self.subfunds: List[Dict[str, Any]] = []
        # уважать num_subfunds из server.py
        self.max_subfunds = int(num_subfunds)

        # ── глобальный контроль темпа / прогрев
        self.start_ts = time.time()
        self.min_step_interval = 0.35
        self.last_action_ts = 0.0

        # прогрев сигналов: до прогрева можно собирать фичи, но не торговать
        self.signal_warmup_sec = 75.0
        self.min_mids_for_signal = 55
        self.min_trades_for_signal = 18

        # cooldown-ы на старт кампаний (чтобы не спамить саб-фонды)
        self.last_macro_campaign_ts = 0.0
        self.last_micro_campaign_ts = 0.0
        self.macro_campaign_cooldown_sec = 9.5 * 60.0
        self.micro_campaign_cooldown_sec = 32.0

        # трекинг рынка
        self.mid_ema_fast = EMA(0.30)
        self.mid_ema_slow = EMA(0.06)
        self.ret_ema = EMA(0.25)
        self.vol_ema = EMA(0.10)
        self.trade_flow = EMA(0.25, 0.0)  # дисбаланс агрессии

        self.recent_mids = deque(maxlen=400)
        self.recent_trades = deque(maxlen=500)  # (ts, side, price, qty)

        # риск
        self.risk_state = "normal"  # normal / reduced / flat
        self.drawdown_ema = EMA(0.18, 0.0)
        self.pnl_hist = deque(maxlen=400)

        # устойчивость направления (для MACRO фильтра)
        self._trend_dir_hist = deque(maxlen=90)

        # order meta
        self._order_meta: Dict[str, Dict[str, Any]] = {}

    # ───────────────────────────── util ─────────────────────────────

    def mark_to_market(self, mid: float) -> float:
        return self.cash + self.inventory * float(mid) - self.capital

    def fill(self, side: OrderSide, price: float, qty: float) -> None:
        price = float(price)
        qty = float(qty)
        signed = qty if side == OrderSide.BID else -qty
        self.inventory += signed
        self.cash -= signed * price

    # ───────────────────────── trade feed ─────────────────────────

    def _update_trades(self, order_book) -> None:
        if not hasattr(order_book, "trade_history"):
            return
        if not order_book.trade_history:
            return

        last_seen_ts = self.recent_trades[-1][0] if self.recent_trades else 0.0

        # order_book.trade_history может быть deque → slicing нельзя
        for t in list(order_book.trade_history)[-80:]:
            ts = float(t.get("timestamp", 0.0))
            if ts <= last_seen_ts:
                continue
            side = t.get("side", None)  # обычно "buy"/"sell"
            price = float(t.get("price", 0.0))
            qty = float(t.get("quantity", t.get("qty", 0.0)) or 0.0)
            if price <= 0.0 or qty <= 0.0:
                continue
            self.recent_trades.append((ts, side, price, qty))

        # обновляем flow-метрику
        if self.recent_trades:
            s = 0.0
            total = 0.0
            for (ts, side, price, qty) in list(self.recent_trades)[-40:]:
                q = float(qty)
                total += q
                if side == "buy":
                    s += q
                elif side == "sell":
                    s -= q
            if total > 0.0:
                self.trade_flow.update(_clip(s / total, -1.0, 1.0))

    def _extract_features(self, order_book) -> Dict[str, Any]:
        best_bid = None
        best_ask = None

        if hasattr(order_book, "_best_bid_price"):
            try:
                best_bid = order_book._best_bid_price()
            except Exception:
                best_bid = None
        if hasattr(order_book, "_best_ask_price"):
            try:
                best_ask = order_book._best_ask_price()
            except Exception:
                best_ask = None

        if best_bid is not None:
            best_bid = float(best_bid)
        if best_ask is not None:
            best_ask = float(best_ask)

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

        if self.last_mid is not None and mid is not None:
            ret = mid - self.last_mid
        else:
            ret = 0.0

        self.last_mid = mid if mid is not None else self.last_mid

        if mid is not None:
            self.recent_mids.append(mid)
            self.mid_ema_fast.update(mid)
            self.mid_ema_slow.update(mid)

            self.ret_ema.update(ret)
            self.vol_ema.update(abs(ret))

            pnl = self.mark_to_market(mid)
            self.pnl_hist.append((now, pnl))

            if len(self.pnl_hist) > 2:
                dd = pnl - max(x[1] for x in self.pnl_hist)
                self.drawdown_ema.update(dd)

        feat = {
            "mid": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": float(spread),
            "ret": float(ret),
            "vol": float(self.vol_ema.value),
            "trend_fast": float(self.mid_ema_fast.value),
            "trend_slow": float(self.mid_ema_slow.value),
            "flow": float(self.trade_flow.value),
        }
        return feat

    # ───────────────────────── risk ─────────────────────────

    def _update_risk_state(self, mid: Optional[float]) -> None:
        if mid is None:
            return

        pnl = self.mark_to_market(mid)
        peak = max((x[1] for x in self.pnl_hist), default=pnl)
        dd = pnl - peak  # dd отрицательный

        dd_frac = dd / max(1.0, self.capital)

        vol = float(self.vol_ema.value)
        vol_frac = vol / max(1.0, mid)

        if dd_frac < -0.012 or vol_frac > 0.0045:
            self.risk_state = "flat"
        elif dd_frac < -0.006 or vol_frac > 0.0032:
            self.risk_state = "reduced"
        else:
            self.risk_state = "normal"

    # ───────────────────────── signals ─────────────────────────

    def _signal_ready(self, now: float) -> bool:
        if (now - self.start_ts) < float(self.signal_warmup_sec):
            return False
        if len(self.recent_mids) < int(self.min_mids_for_signal):
            return False
        if len(self.recent_trades) < int(self.min_trades_for_signal):
            return False
        return True

    def _trend_stats(self, feat: Dict[str, Any]) -> Tuple[int, float, float, float]:
        """
        Возвращает:
          trend_dir: +1/-1/0
          diff_bps: fast-slow в bps
          dev_bps: mid-slow в bps
          flow_aligned: flow * trend_dir
        """
        mid = feat["mid"]
        if mid is None:
            return 0, 0.0, 0.0, 0.0

        fast = feat["trend_fast"]
        slow = feat["trend_slow"]
        flow = feat["flow"]

        if fast is None or slow is None:
            return 0, 0.0, 0.0, 0.0

        mid = float(mid)
        fast = float(fast)
        slow = float(slow)

        diff = fast - slow
        diff_bps = (diff / max(1e-9, mid)) * 1e4
        dev = mid - slow
        dev_bps = (dev / max(1e-9, mid)) * 1e4

        if abs(diff_bps) < 1e-9:
            return 0, float(diff_bps), float(dev_bps), 0.0

        trend_dir = 1 if diff_bps > 0.0 else -1
        flow_aligned = float(flow) * float(trend_dir)
        return int(trend_dir), float(diff_bps), float(dev_bps), float(flow_aligned)

    def _trend_extension(self, feat: Dict[str, Any]) -> Tuple[int, float]:
        """
        Определяем "перекос тренда" на базе fast/slow EMA и агрессии.
        Возвращает (dir, score), где dir: +1 (uptrend), -1 (downtrend), 0 (no)
        score ~ 0..1
        """
        mid = feat["mid"]
        if mid is None:
            return 0, 0.0

        if len(self.recent_mids) < 20:
            return 0, 0.0

        trend_dir, diff_bps, dev_bps, flow_aligned = self._trend_stats(feat)
        if trend_dir == 0:
            return 0, 0.0

        # вола в bps (рет по mid)
        vol = float(feat.get("vol", 0.0))
        vol_bps = (vol / max(1e-9, float(mid))) * 1e4

        # нормируем перекос относительно текущей "шумихи" (vol)
        # чтобы на нулевой воле не триггерить микро-диффы
        denom = max(0.55, 2.8 * vol_bps + 0.45)
        ext_norm = abs(diff_bps) / denom  # ~ 0..?

        score = 0.0
        score += _clip(ext_norm / 2.2, 0.0, 1.25) * 0.62
        score += _clip(max(0.0, flow_aligned) / 0.72, 0.0, 1.0) * 0.38
        score = _clip(score, 0.0, 1.0)

        # нижний порог на diff_bps — не реагируем на мелочь
        if abs(diff_bps) < 1.25:
            return 0, 0.0

        return trend_dir, score

    def _micro_reversion_signal(self, feat: Dict[str, Any]) -> Tuple[int, float]:
        """
        Тактический контр-сигнал:
          - экстремальная агрессия (flow)
          - заметное отклонение от slow EMA (dev_bps)
        Возвращает (direction, score), где direction — куда хотим (позиция),
        т.е. +1 long, -1 short.
        """
        mid = feat["mid"]
        if mid is None:
            return 0, 0.0

        trend_dir, diff_bps, dev_bps, flow_aligned = self._trend_stats(feat)
        flow = float(feat.get("flow", 0.0))

        # хотим фейдить экстремальную агрессию, но только если цена реально "уехала"
        abs_flow = abs(flow)
        abs_dev = abs(dev_bps)

        if abs_flow < 0.66 or abs_dev < 0.95:
            return 0, 0.0

        # направление: против flow и против dev
        if flow > 0.0 and dev_bps > 0.0:
            direction = -1  # short
        elif flow < 0.0 and dev_bps < 0.0:
            direction = +1  # long
        else:
            return 0, 0.0

        score = 0.0
        score += _clip((abs_flow - 0.66) / 0.34, 0.0, 1.0) * 0.55
        score += _clip((abs_dev - 0.95) / 3.6, 0.0, 1.0) * 0.45
        score = _clip(score, 0.0, 1.0)

        return int(direction), float(score)

    def _update_trend_persistence(self, feat: Dict[str, Any]) -> None:
        """
        Для MACRO: считаем устойчивость направления fast-slow.
        """
        trend_dir, diff_bps, dev_bps, flow_aligned = self._trend_stats(feat)
        if trend_dir == 0:
            self._trend_dir_hist.append(0)
            return

        # не учитываем слабый дрейф
        if abs(diff_bps) < 1.6:
            self._trend_dir_hist.append(0)
        else:
            self._trend_dir_hist.append(int(trend_dir))

    def _trend_persistence(self) -> float:
        h = list(self._trend_dir_hist)
        if not h:
            return 0.0
        # считаем долю согласованного направления
        # если 0 много — будет размывать, что и нужно
        s = float(sum(h))
        denom = float(len(h))
        return float(_clip(abs(s) / max(1.0, denom), 0.0, 1.0))

    # ───────────────────────── campaigns ─────────────────────────

    def _make_subfund(
        self,
        name: str,
        direction: int,
        target_notional: float,
        horizon_sec: float,
        start_lag_sec: float,
        kind: str = "macro",
        participation_cap: Optional[float] = None,
        min_place_interval: Optional[float] = None,
    ) -> Dict[str, Any]:
        if participation_cap is None:
            participation_cap = random.uniform(0.02, 0.08) if kind == "macro" else random.uniform(0.005, 0.020)
        if min_place_interval is None:
            min_place_interval = random.uniform(0.55, 1.35) if kind == "macro" else random.uniform(0.22, 0.85)

        return {
            "name": name,
            "kind": str(kind),  # "macro" | "micro"
            "state": "warmup",  # warmup -> build -> unwind -> done
            "direction": int(direction),
            "target_notional": float(target_notional),
            "executed_notional": 0.0,
            "horizon_sec": float(horizon_sec),
            "start_ts": time.time(),
            "start_lag_sec": float(start_lag_sec),
            "position": 0.0,  # inventory внутри саб-фонда
            "last_px": None,
            "last_place_ts": 0.0,
            "min_place_interval": float(min_place_interval),
            "participation_cap": float(participation_cap),
        }

    def _current_active_notional(self, mid: float) -> float:
        s = 0.0
        for sub in self.subfunds:
            if sub["state"] in ("warmup", "build", "unwind"):
                s += abs(float(sub["position"])) * float(mid)
        return float(s)

    def _active_counts(self) -> Tuple[int, int]:
        macro = 0
        micro = 0
        for s in self.subfunds:
            if s["state"] in ("warmup", "build", "unwind"):
                if s.get("kind") == "micro":
                    micro += 1
                else:
                    macro += 1
        return macro, micro

    def _maybe_start_campaigns(self, feat: Dict[str, Any], now: float) -> None:
        mid = feat["mid"]
        if mid is None:
            return
        mid = float(mid)

        if self.risk_state == "flat":
            return

        # без прогрева — никаких новых кампаний
        if not self._signal_ready(now):
            return

        # лимит по общему риску/капиталу
        cur_notional = self._current_active_notional(mid)
        if cur_notional > 0.35 * self.capital:
            return

        macro_active, micro_active = self._active_counts()

        # ── MACRO (устойчивый перегрев тренда)
        trend_dir, ext_score = self._trend_extension(feat)
        persistence = self._trend_persistence()

        # контр-направление: если тренд вверх — фонд шортит, и наоборот
        if trend_dir != 0:
            _, diff_bps, dev_bps, flow_aligned = self._trend_stats(feat)

            macro_trigger = (
                abs(diff_bps) >= 2.35 and
                ext_score >= 0.58 and
                persistence >= 0.42 and
                flow_aligned >= 0.10
            )

            # reduced => требуем сильнее сигнал
            if self.risk_state == "reduced":
                macro_trigger = macro_trigger and (ext_score >= 0.66) and (abs(diff_bps) >= 2.8)

            if macro_trigger:
                if (now - self.last_macro_campaign_ts) >= float(self.macro_campaign_cooldown_sec):
                    # ограничим число MACRO саб-фондов
                    if macro_active < max(1, int(self.max_subfunds)):
                        base = self.capital * random.uniform(0.02, 0.075)
                        target = base * (0.65 + 0.85 * ext_score)

                        horizon = random.uniform(45 * 60, 150 * 60)
                        start_lag = random.uniform(6 * 60, 22 * 60)

                        direction = -trend_dir
                        name = f"MRF_M_{len(self.subfunds)+1}"
                        sub = self._make_subfund(
                            name=name,
                            direction=direction,
                            target_notional=target,
                            horizon_sec=horizon,
                            start_lag_sec=start_lag,
                            kind="macro",
                            participation_cap=random.uniform(0.02, 0.07),
                            min_place_interval=random.uniform(0.65, 1.55),
                        )
                        self.subfunds.append(sub)
                        self.last_macro_campaign_ts = now

        # ── MICRO (тактический фейд агрессии/отклонения)
        # микрослой может стартовать чаще, но маленьким объёмом и с жёстким cooldown
        micro_dir, micro_score = self._micro_reversion_signal(feat)
        if micro_dir != 0 and micro_score >= 0.40:
            if (now - self.last_micro_campaign_ts) >= float(self.micro_campaign_cooldown_sec):
                # ограничим количество микро-исполнений
                max_micro = max(2, int(round(self.max_subfunds * 1.5)))
                if micro_active < max_micro:
                    # микрослой в сумме тоже ограничим риском
                    # (внутри общей 0.35*capital уже есть, но дополнительно подрежем)
                    micro_cap = 0.10 * self.capital
                    micro_notional = 0.0
                    for s in self.subfunds:
                        if s.get("kind") == "micro" and s["state"] in ("warmup", "build", "unwind"):
                            micro_notional += abs(float(s["position"])) * mid
                    if micro_notional < micro_cap:
                        base = self.capital * random.uniform(0.0022, 0.0105)
                        target = base * (0.75 + 0.85 * micro_score)

                        horizon = random.uniform(95.0, 280.0)
                        start_lag = random.uniform(0.0, 12.0)

                        name = f"MRF_u_{len(self.subfunds)+1}"
                        sub = self._make_subfund(
                            name=name,
                            direction=int(micro_dir),
                            target_notional=float(target),
                            horizon_sec=float(horizon),
                            start_lag_sec=float(start_lag),
                            kind="micro",
                            participation_cap=random.uniform(0.006, 0.018),
                            min_place_interval=random.uniform(0.22, 0.78),
                        )
                        self.subfunds.append(sub)
                        self.last_micro_campaign_ts = now

    def _s_curve(self, x: float) -> float:
        x = _clip(x, 0.0, 1.0)
        return x * x * (3.0 - 2.0 * x)

    def _desired_notional_for_subfund(self, sub: Dict[str, Any], now: float) -> float:
        kind = sub.get("kind", "macro")
        horizon = float(sub["horizon_sec"])
        # для micro не используем "минимум 30 минут"
        if kind == "macro":
            horizon = max(1800.0, horizon)
        else:
            horizon = max(65.0, horizon)

        t = (now - float(sub["start_ts"]) - float(sub["start_lag_sec"])) / horizon
        prog = self._s_curve(t)
        return float(sub["target_notional"]) * prog

    # ───────────────── генерация ордеров саб-фондов ─────────────────

    def _recent_turnover_60s(self, now: float) -> float:
        if not self.recent_trades:
            return 0.0

        cutoff = now - 60.0
        notional = 0.0
        for (ts, side, price, qty) in reversed(self.recent_trades):
            if float(ts) < cutoff:
                break
            notional += float(price) * float(qty)
        return float(notional)

    def _build_execution_orders(
        self,
        sub: Dict[str, Any],
        mid: float,
        best_bid: float,
        best_ask: float,
        qty_total: float,
        aggressiveness: float,
    ) -> List[Order]:
        orders: List[Order] = []
        direction = int(sub["direction"])
        if direction == 0:
            return orders

        side = OrderSide.BID if direction > 0 else OrderSide.ASK
        spread = max(TICK, float(best_ask) - float(best_bid))

        kind = sub.get("kind", "macro")

        # число слайсов
        n_slices = random.randint(2, 6) if kind == "macro" else random.randint(2, 4)

        # доля маркетов растёт с aggressiveness и flow, но micro не должен быть "в лоб" всегда
        flow_abs = abs(float(self.trade_flow.value))
        if kind == "macro":
            mkt_frac = _clip(0.18 + 0.52 * aggressiveness + 0.18 * flow_abs, 0.08, 0.80)
        else:
            mkt_frac = _clip(0.15 + 0.45 * aggressiveness + 0.10 * flow_abs, 0.05, 0.70)

        remaining = float(qty_total)
        for _ in range(n_slices):
            if remaining <= 0.0:
                break

            # чуть рандомим, но не выходим за остаток
            raw = (qty_total / n_slices) * random.uniform(0.75, 1.25)
            qty = float(min(remaining, max(0.0, raw)))
            if qty <= 0.0:
                break
            remaining -= qty

            use_mkt = random.random() < mkt_frac

            if use_mkt:
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=qty,
                    price=None,
                    order_type=OrderType.MARKET,
                    ttl=None,
                )
            else:
                # лимитка: пассивно, но иногда ближе к топу
                if kind == "macro":
                    edge = random.uniform(0.18, 0.90) * spread
                    edge_mult = random.uniform(0.06, 0.30)
                else:
                    edge = random.uniform(0.10, 0.65) * spread
                    edge_mult = random.uniform(0.05, 0.35)

                if side == OrderSide.BID:
                    price = float(best_bid) + edge * edge_mult
                else:
                    price = float(best_ask) - edge * edge_mult

                price = float(quant(price))
                ttl = random.uniform(0.6, 2.2) if kind == "macro" else random.uniform(0.35, 1.15)

                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=qty,
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=float(ttl),
                )

            orders.append(o)
            self._order_meta[o.order_id] = {"sub": sub["name"], "phase": "build"}

        return orders

    def _unwind_execution_orders(self, sub: Dict[str, Any], mid: float, best_bid: float, best_ask: float, qty_total: float) -> List[Order]:
        orders: List[Order] = []
        pos = float(sub["position"])
        if pos == 0.0:
            return orders

        side = OrderSide.ASK if pos > 0 else OrderSide.BID
        spread = max(TICK, float(best_ask) - float(best_bid))

        kind = sub.get("kind", "macro")
        n_slices = random.randint(2, 5) if kind == "macro" else random.randint(2, 4)

        risk_boost = 0.0
        if self.risk_state == "flat":
            risk_boost = 0.45
        elif self.risk_state == "reduced":
            risk_boost = 0.22

        if kind == "macro":
            mkt_frac = _clip(0.18 + 0.25 * random.random() + risk_boost, 0.18, 0.78)
        else:
            # micro unwind быстрее, но не всегда 100% маркет
            mkt_frac = _clip(0.22 + 0.32 * random.random() + risk_boost, 0.22, 0.88)

        remaining = float(qty_total)
        for _ in range(n_slices):
            if remaining <= 0.0:
                break

            raw = (qty_total / n_slices) * random.uniform(0.75, 1.25)
            qty = float(min(remaining, max(0.0, raw)))
            if qty <= 0.0:
                break
            remaining -= qty

            use_mkt = random.random() < mkt_frac

            if use_mkt:
                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=qty,
                    price=None,
                    order_type=OrderType.MARKET,
                    ttl=None,
                )
            else:
                if kind == "macro":
                    edge = random.uniform(0.10, 0.52) * spread
                    edge_mult = random.uniform(0.18, 0.60)
                else:
                    edge = random.uniform(0.08, 0.45) * spread
                    edge_mult = random.uniform(0.18, 0.70)

                if side == OrderSide.BID:
                    price = float(best_bid) + edge * edge_mult
                else:
                    price = float(best_ask) - edge * edge_mult

                price = float(quant(price))
                ttl = random.uniform(0.4, 1.4) if kind == "macro" else random.uniform(0.25, 0.95)

                o = Order(
                    order_id=str(uuid.uuid4()),
                    agent_id=self.agent_id,
                    side=side,
                    volume=qty,
                    price=float(round(price, 5)),
                    order_type=OrderType.LIMIT,
                    ttl=float(ttl),
                )

            orders.append(o)
            self._order_meta[o.order_id] = {"sub": sub["name"], "phase": "unwind"}

        return orders

    def _generate_orders_for_subfund(self, sub: Dict[str, Any], feat: Dict[str, Any], now: float) -> List[Order]:
        orders: List[Order] = []

        mid = feat["mid"]
        best_bid = feat["best_bid"]
        best_ask = feat["best_ask"]

        if mid is None or sub["state"] not in ("build", "unwind") or int(sub["direction"]) == 0:
            return orders
        if best_bid is None or best_ask is None:
            return orders

        # throttle per-subfund
        if (now - float(sub.get("last_place_ts", 0.0))) < float(sub.get("min_place_interval", 0.6)):
            return orders

        mid = float(mid)
        best_bid = float(best_bid)
        best_ask = float(best_ask)

        turnover_60s = self._recent_turnover_60s(now)
        if turnover_60s <= 0.0:
            turnover_60s = self.capital * 0.02  # fallback

        participation_cap = float(sub.get("participation_cap", 0.04))
        max_child_notional = float(turnover_60s) * float(participation_cap)

        # --- build ---
        if sub["state"] == "build":
            desired = self._desired_notional_for_subfund(sub, now)
            gap = float(desired) - float(sub["executed_notional"])
            if gap <= mid:
                return orders

            qty_total = min(gap / mid, max_child_notional / mid)
            if qty_total * mid < mid:
                return orders

            # агрессивность зависит от ext_score и flow; micro дополнительно может быть активнее
            _, ext_score = self._trend_extension(feat)
            flow_abs = abs(float(self.trade_flow.value))
            kind = sub.get("kind", "macro")
            if kind == "macro":
                aggressiveness = _clip(ext_score + 0.25 * flow_abs, 0.0, 1.0)
            else:
                # micro: не привязываемся к ext_score целиком, но учитываем flow
                aggressiveness = _clip(0.40 + 0.45 * flow_abs + 0.15 * random.random(), 0.0, 1.0)

            if self.risk_state == "reduced":
                aggressiveness *= 0.78

            orders.extend(self._build_execution_orders(sub, mid, best_bid, best_ask, qty_total, aggressiveness))

        # --- unwind ---
        elif sub["state"] == "unwind":
            pos = float(sub["position"])
            if abs(pos) * mid < mid:
                sub["state"] = "done"
                return orders

            qty_total = min(abs(pos), max_child_notional / mid)
            if qty_total * mid < mid:
                return orders

            orders.extend(self._unwind_execution_orders(sub, mid, best_bid, best_ask, qty_total))

        if orders:
            sub["last_place_ts"] = float(now)

        return orders

    # ───────────────────────── основной цикл ─────────────────

    def generate_orders(self, order_book, market_context=None, **kwargs) -> List[Order]:
        now = time.time()
        if now - self.last_action_ts < self.min_step_interval:
            return []

        self._update_trades(order_book)
        feat = self._extract_features(order_book)

        # обновим устойчивость тренда, даже если ещё не торгуем
        self._update_trend_persistence(feat)

        self._update_risk_state(feat["mid"])

        orders: List[Order] = []

        # risk-off: только размотка
        if self.risk_state == "flat":
            for sub in self.subfunds:
                if float(sub.get("position", 0.0)) != 0.0 and sub.get("state") != "unwind":
                    sub["state"] = "unwind"
            if self.inventory == 0.0:
                return []

        # до прогрева сигналов — не стартуем кампании и не ставим ордера (кроме unwind в risk-off выше)
        if not self._signal_ready(now):
            return []

        self._maybe_start_campaigns(feat, now)

        for sub in self.subfunds:
            if sub["state"] == "warmup":
                if (now - float(sub["start_ts"])) >= float(sub["start_lag_sec"]):
                    sub["state"] = "build"
                else:
                    continue

            if sub["state"] in ("build", "unwind"):
                orders.extend(self._generate_orders_for_subfund(sub, feat, now))

        if orders:
            self.last_action_ts = now
        return orders

    # ───────────────────────── приём исполнений ─────────────────

    def on_order_filled(self, order_id: str, price: float, qty: float, side: OrderSide, slippage: float = 0.0) -> None:
        qty = float(qty)
        price = float(price)
        if qty <= 0.0 or price <= 0.0:
            return

        self.fill(side, price, qty)

        meta = self._order_meta.get(order_id)
        if not meta:
            return

        sub_name = meta.get("sub")
        phase = meta.get("phase", "")

        for sub in self.subfunds:
            if sub.get("name") != sub_name:
                continue

            kind = sub.get("kind", "macro")

            signed = qty if side == OrderSide.BID else -qty
            sub["position"] = float(sub.get("position", 0.0)) + signed

            if phase == "build":
                sub["executed_notional"] = float(sub.get("executed_notional", 0.0)) + abs(qty) * price

                # при достижении цели: micro почти всегда разматывает, macro часто держит
                if float(sub["executed_notional"]) >= float(sub["target_notional"]) * random.uniform(0.92, 1.02):
                    if kind == "micro":
                        if random.random() < 0.88 or self.risk_state != "normal":
                            sub["state"] = "unwind"
                    else:
                        if random.random() < 0.55 or self.risk_state != "normal":
                            sub["state"] = "unwind"

            elif phase == "unwind":
                if abs(float(sub.get("position", 0.0))) * price < price:
                    sub["state"] = "done"

            break

    # ───────────────── внешние хуки ─────────────────

    def restore_capital(self) -> None:
        pass

    def perceive_market(self, market_context) -> str:
        return "macro_reversal_fund"


# хак: чтобы _extract_features работал без AttributeError на первом тике
MacroReversalFund.last_mid = None
