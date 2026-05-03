# band_wall_provider.py
import time
import math
import random
import uuid
from collections import deque

from order import Order, OrderSide, OrderType, TICK, quant


class AdaptiveBandWallProvider:
    """
    Держит "полосу" лимитной ликвидности на ~10 тиков от цены (не ММ).
    Не требует никаких параметров в server.py кроме (agent_id, capital).

    Ключевое:
      - Полоса 10..13 тиков (4 уровня) вместо 1 уровня
      - Объём автокалибруется от: (перцентиль глубины в стакане) + (активность сделок) + (вола)
      - Делает top-up: доводит объём на своих уровнях до target, а не тупо ставит фикс
      - Обновляет не каждый тик: репрайс только если якорь цены сдвинулся
    """

    def __init__(self, agent_id: str, capital: float):
        self.agent_id = agent_id
        self.capital = float(capital)

        # агент сам задаёт частоту (server уважает loop_interval)
        self.loop_interval = random.uniform(0.22, 0.55)

        # наши активные лимитки: ключ (side, price) -> {"oid":..., "qty":..., "ts":...}
        self._live = {}

        # история mid для оценки волы
        self._mid_hist = deque(maxlen=80)
        self._last_best_bid = None
        self._last_best_ask = None
        self._last_anchor_update = 0.0

        # кулдауны на "усиление стены" (чтобы не паттернилось)
        self._boost_cooldown_until = 0.0

    # ---------- утилиты ----------

    def _best_bid_ask(self, ob):
        bb = ob._best_bid_price()
        ba = ob._best_ask_price()
        if bb is None or ba is None:
            # fallback на last_trade_price
            lp = getattr(ob, "last_trade_price", None)
            if lp is None:
                return None, None
            # грубо: вокруг last_trade
            return quant(lp - TICK), quant(lp + TICK)
        return float(bb), float(ba)

    def _snapshot_maps(self, ob, depth=200):
        snap = ob.get_order_book_snapshot(depth=depth) or {}
        bids = snap.get("bids") or []
        asks = snap.get("asks") or []

        bid_map = {float(x["price"]): float(x["volume"]) for x in bids if "price" in x and "volume" in x}
        ask_map = {float(x["price"]): float(x["volume"]) for x in asks if "price" in x and "volume" in x}
        return bid_map, ask_map

    def _recent_flow(self, trade_history, now_ts: float, lookback_s: float = 8.0):
        """net_flow>0 => доминировали buy market, <0 => sell market"""
        if not trade_history:
            return 0.0, 0.0

        net = 0.0
        tot = 0.0
        cutoff = now_ts - lookback_s
        # trade_history у тебя растёт, поэтому идём с конца
        for t in reversed(trade_history):
            ts = float(t.get("timestamp", 0.0) or 0.0)
            if ts < cutoff:
                break
            vol = float(t.get("volume", 0.0) or 0.0)
            side = str(t.get("taker_side", "")).lower()
            sgn = 1.0 if side == "buy" else -1.0 if side == "sell" else 0.0
            net += sgn * vol
            tot += vol
        return net, tot / max(lookback_s, 1e-9)  # vps

    def _vol_ticks(self):
        if len(self._mid_hist) < 10:
            return 0.0
        diffs = [abs(self._mid_hist[i] - self._mid_hist[i - 1]) for i in range(1, len(self._mid_hist))]
        if not diffs:
            return 0.0
        # robust: median abs move
        diffs.sort()
        med = diffs[len(diffs) // 2]
        return float(med) / max(TICK, 1e-12)

    def _percentile(self, xs, p):
        if not xs:
            return 0.0
        xs = sorted(xs)
        k = int(round((p / 100.0) * (len(xs) - 1)))
        k = max(0, min(len(xs) - 1, k))
        return float(xs[k])

    def _cap_level_max(self):
        # НЕ фикс: растёт ~ sqrt(capital), чтобы не было “одни и те же 5000”
        # под твои масштабы (когда в стакане тысячи) это адекватно.
        sc = math.sqrt(max(1.0, self.capital))
        base = 350.0 + 0.62 * sc  # 150M => ~7800; 300M => ~11k
        return float(base)

    # ---------- ордер-операции ----------

    def _cancel(self, oid: str):
        return Order(
            order_id=str(oid),
            agent_id=self.agent_id,
            side=OrderSide.BID,
            volume=0.0,
            price=None,
            order_type=OrderType.CANCEL,
            ttl=None,
            metadata={"latency_ms": random.randint(0, 25), "latency_jitter_ms": random.randint(0, 15)},
        )

    def _place_limit(self, side: OrderSide, price: float, qty: float, ttl_ticks: int):
        return Order(
            order_id=uuid.uuid4().hex,
            agent_id=self.agent_id,
            side=side,
            volume=float(qty),
            price=float(quant(price)),
            order_type=OrderType.LIMIT,
            ttl=int(ttl_ticks),
            metadata={},
        )

    # ---------- основной цикл ----------

    def generate_orders(self, order_book, market_context):
        now = time.time()

        bb, ba = self._best_bid_ask(order_book)
        if bb is None or ba is None:
            return []

        mid = 0.5 * (bb + ba)
        self._mid_hist.append(mid)

        spread_ticks = max(1, int(round((ba - bb) / max(TICK, 1e-12))))

        # якорь обновляем не постоянно
        anchor_changed = False
        if self._last_best_bid is None or self._last_best_ask is None:
            anchor_changed = True
        else:
            # если best сдвинулся ощутимо — переставляем
            if abs(bb - self._last_best_bid) >= 2 * TICK or abs(ba - self._last_best_ask) >= 2 * TICK:
                anchor_changed = True
        if now - self._last_anchor_update >= 1.8:
            anchor_changed = True

        self._last_best_bid, self._last_best_ask = bb, ba
        if anchor_changed:
            self._last_anchor_update = now

        # --- режим рынка (для масштаба объёмов) ---
        vol_ticks = self._vol_ticks()
        net_flow, vps = self._recent_flow(getattr(order_book, "trade_history", []), now, lookback_s=8.0)

        # классификация режима (простая, но не "if-else на 2 строки")
        activity = 0.55 * min(vps / 2500.0, 3.0) + 0.45 * min(vol_ticks / 8.0, 3.0)  # 0..~3
        if activity < 0.65:
            regime = "calm"
            mult = 1.15
        elif activity < 1.35:
            regime = "active"
            mult = 1.55
        else:
            regime = "stress"
            mult = 2.05

        # сторона усиления против давления (как в реале часто делают "сдерживание")
        ask_bias = 1.0
        bid_bias = 1.0
        if net_flow > 0:
            # покупают маркетом => усиливаем стену сверху
            ask_bias *= 1.35
            bid_bias *= 0.92
        elif net_flow < 0:
            bid_bias *= 1.35
            ask_bias *= 0.92

        # --- целевая зона: 10..13 тиков, но не внутрь широкого спреда ---
        base_ticks = max(6, spread_ticks + 2)
        band_ticks = [base_ticks, base_ticks + 1, base_ticks + 2, base_ticks + 3]

        # веса внутри полосы (чтобы не было "одинаково на всех уровнях")
        w = [0.46, 0.28, 0.16, 0.10]

        # --- снимаем текущую глубину, чтобы делать top-up ---
        bid_map, ask_map = self._snapshot_maps(order_book, depth=200)

        # baseline глубины вокруг (8..20 тиков)
        def band_samples(side: str):
            out = []
            if side == "bid":
                for p, v in bid_map.items():
                    d = int(round((bb - p) / TICK))
                    if 8 <= d <= 20:
                        out.append(v)
            else:
                for p, v in ask_map.items():
                    d = int(round((p - ba) / TICK))
                    if 8 <= d <= 20:
                        out.append(v)
            return out

        bid_samples = band_samples("bid")
        ask_samples = band_samples("ask")

        # перцентили дают адаптацию к твоему рынку (а не "поставь 5000")
        p70_bid = self._percentile(bid_samples, 70)
        p85_bid = self._percentile(bid_samples, 85)
        p70_ask = self._percentile(ask_samples, 70)
        p85_ask = self._percentile(ask_samples, 85)

        # target_total_side — сколько суммарно хотим иметь в полосе (видимого)
        cap_level_max = self._cap_level_max()
        cap_side_max = 4.0 * cap_level_max

        # динамика: если рынок пустой — не уходим в ноль, иначе стена не появится
        base_bid = max(12000.0, 0.6 * p70_bid + 0.4 * p85_bid)
        base_ask = max(12000.0, 0.6 * p70_ask + 0.4 * p85_ask)

        target_band_bid = min(cap_side_max, base_bid * mult * bid_bias * random.uniform(0.92, 1.12))
        target_band_ask = min(cap_side_max, base_ask * mult * ask_bias * random.uniform(0.92, 1.12))

        # редкие "красные" стенки (>=10k) без скрипта:
        # только в stress/active и не чаще кулдауна.
        if now >= self._boost_cooldown_until and regime in ("active", "stress"):
            prob = 0.08 if regime == "active" else 0.16
            prob *= min(1.6, 1.0 + abs(net_flow) / 8000.0)
            if random.random() < prob:
                # усиление на 15–35 секунд
                boost = random.uniform(1.35, 1.95)
                target_band_bid = min(cap_side_max * 1.6, target_band_bid * boost)
                target_band_ask = min(cap_side_max * 1.6, target_band_ask * boost)
                self._boost_cooldown_until = now + random.uniform(20.0, 55.0)

        # TTL: чтобы было видно на heatmap и при этом обновлялось
        ttl_ticks = random.randint(70, 210)  # ~21..63 сек при tick 0.3s

        orders = []

        # helper: сколько уже стоит на конкретном уровне, и сколько из этого наше
        def our_qty_at(side: OrderSide, price: float):
            k = (side.value, float(quant(price)))
            rec = self._live.get(k)
            return float(rec["qty"]) if rec else 0.0

        def set_level(side: OrderSide, price: float, desired_our: float):
            price = float(quant(price))
            k = (side.value, price)
            cur = self._live.get(k)

            # если у нас там есть — решаем, надо ли менять
            if cur is not None:
                cur_qty = float(cur["qty"])
                # мелкие поправки не дёргаем
                if desired_our <= 1e-6:
                    orders.append(self._cancel(cur["oid"]))
                    self._live.pop(k, None)
                    return
                rel = abs(desired_our - cur_qty) / max(1.0, cur_qty)
                if rel < 0.18 and not anchor_changed:
                    return
                # иначе пересоздаём
                orders.append(self._cancel(cur["oid"]))
                self._live.pop(k, None)

            if desired_our <= 1e-6:
                return

            o = self._place_limit(side, price, desired_our, ttl_ticks=ttl_ticks)
            self._live[k] = {"oid": o.order_id, "qty": float(desired_our), "ts": now}
            orders.append(o)

        # расчёт уровней по сторонам
        def build_side(side: OrderSide):
            nonlocal orders

            if side == OrderSide.BID:
                target_total = target_band_bid
                snap_map = bid_map
                ref = bb
                sign = -1.0
            else:
                target_total = target_band_ask
                snap_map = ask_map
                ref = ba
                sign = 1.0

            # распределяем target_total по уровням полосы
            for i, dticks in enumerate(band_ticks):
                px = ref + sign * dticks * TICK

                # сколько уже стоит суммарно (включая нас)
                snap_vol = float(snap_map.get(float(quant(px)), 0.0))

                # оценка чужого объёма на уровне
                ours = our_qty_at(side, px)
                others = max(0.0, snap_vol - ours)

                lvl_target = target_total * w[i] * random.uniform(0.90, 1.10)

                min_lvl = 5000.0

                # в активном/стресс режиме стенка естественно жирнее
                if regime == "active":
                    min_lvl *= 1.35
                elif regime == "stress":
                    min_lvl *= 1.85

                # делаем ближние уровни полосы (10-11 тиков) сильнее, дальние (12-13) чуть мягче
                if i == 0:
                    floor_lvl = min_lvl * 1.15
                elif i == 1:
                    floor_lvl = min_lvl
                elif i == 2:
                    floor_lvl = min_lvl * 0.75
                else:
                    floor_lvl = min_lvl * 0.55

                lvl_target = max(lvl_target, floor_lvl)

                # сколько должны поставить мы, чтобы довести до lvl_target
                need = max(0.0, lvl_target - others)

                # ограничение по капиталу/масштабу (авто, не фикс)
                lvl_cap = cap_level_max * (1.15 if i == 0 else 1.0)
                if regime == "stress":
                    lvl_cap *= 1.35

                desired_our = min(need, lvl_cap)

                set_level(side, px, desired_our)

        build_side(OrderSide.BID)
        build_side(OrderSide.ASK)

        # подчистка "наших" уровней, которые вышли из полосы (если якорь сменился)
        if anchor_changed:
            valid_prices = set()
            for dticks in band_ticks:
                valid_prices.add((OrderSide.BID.value, float(quant(bb - dticks * TICK))))
                valid_prices.add((OrderSide.ASK.value, float(quant(ba + dticks * TICK))))

            for k in list(self._live.keys()):
                if k not in valid_prices:
                    orders.append(self._cancel(self._live[k]["oid"]))
                    self._live.pop(k, None)

        return orders

    def on_order_filled(self, order_id, price, volume, side: OrderSide):
        """
        Хук от сервера: уведомление о частичном/полном исполнении.
        Нам нужен для (1) трекинга инвентаря, (2) очистки _live если нашу лимитку съели.
        """
        try:
            v = float(volume or 0.0)
        except Exception:
            v = 0.0

        # инвентарь (для будущего skew, сейчас не мешает)
        if side == OrderSide.BID:
            self.inventory = getattr(self, "inventory", 0.0) + v
        elif side == OrderSide.ASK:
            self.inventory = getattr(self, "inventory", 0.0) - v

        # если это наш ордер — удаляем из _live, чтобы агент заново сделал top-up
        # _live хранит { (side.value, price): {"oid":..., ...} }
        try:
            oid = str(order_id)
        except Exception:
            oid = order_id

        for k, rec in list(self._live.items()):
            if rec.get("oid") == oid:
                self._live.pop(k, None)
                break

