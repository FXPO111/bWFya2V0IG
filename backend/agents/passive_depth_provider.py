import uuid
import random
import time
import math

from backend.core.order import Order, OrderSide, OrderType

def _ctx_now(ctx):
    try:
        v = getattr(ctx, "now_ts", None)
        return float(v) if v is not None else time.time()
    except Exception:
        return time.time()


class PassiveDepthProvider:
    """
    Банковский FX-LP под твою архитектуру (облегчённая, более живая версия).

    Роль:
      - НЕ трогает спред и best bid/ask (это делает маркет-мейкер).
      - Держит неровную фоновую глубину 0.06..2.0+
      - Даёт реальные «дыры», кластеры и стены, но не бетонирует рынок.
      - Реагирует на тренд, агрессивный поток и инвентарь.
    """

    # ============================================================
    # ИНИЦИАЛИЗАЦИЯ
    # ============================================================
    def __init__(
        self,
        agent_id,
        capital,
        min_depth_percent=0.01,
        max_depth_percent=0.01,
        order_lifetime=10.0
    ):
        self.agent_id = agent_id
        self.capital = float(capital)

        # --- геометрия глубины ---
        self.levels = 600        # CALIB: 200→120. At 200: g=0.513→BBO~3000/provider.
                                    # At 120: g=0.870→BBO~5060/provider→total~10120 ≈ EUR target.
        self.start_i = 6             # с какого i начинаются "дальние" (0.06)
        self.core_offsets = []       # LP не лезет в ядро рядом с mid
        self.tick_step = 0.001

        # --- базовая кривая глубины (до всех модификаторов) ---
        # CALIB: EUR BBO equiv = 10,116 contracts (0.9 lot * 11240).
        # Sim has 2 depth providers, current combined BBO = 3288.
        # Target ~8000-10000: increase base_depth_qty 4000 → 6000 per provider.
        self.base_depth_qty = 3000.0      # CALIB: was 4000
        self.depth_lambda = 0.030
        self.depth_cap_ratio = 0.12       # CALIB: was 0.10 (slightly more headroom)
        self.depth_noise_amp = 0.18

        # --- "подагенты" внутри LP (разные стили) ---
        self.profile_weights = {
            "mean_reverter": 0.35,   # против движения, но уже не доминирует
            "trend_accepter": 0.30,  # поддаётся тренду
            "sticky_liquidity": 0.35 # даёт стены/кластеры
        }

        # --- дозаправка ---
        # уменьшаем размер дозаливки: LP не должен вываливать огромные порции
        self.place_chunk_core = 3500.0    # CALIB: was 2500
        self.place_chunk_depth = 1500.0   # CALIB: was 3500

        # --- вероятность пропуска уровней ---
        # раньше near=0 → сплошная «ванна»; добавляем дыр
        self.skip_prob_near = 0.05         # 0.06–0.25
        self.skip_prob_mid = 0.08          # 0.25–1.0
        self.skip_prob_far = 0.12          # >1.0

        # --- айсберги ---
        self.iceberg_prob_near = 0.04
        self.iceberg_prob_mid = 0.10
        self.iceberg_prob_far = 0.18
        self.iceberg_min_vis = 1200.0
        self.iceberg_max_vis = 3200.0
        self.iceberg_min_ratio = 2.5
        self.iceberg_max_ratio = 8.0

        # --- TTL ---
        # заметно сокращаем длительность жизни ближних уровней
        self.ttl_core_min = 2.0
        self.ttl_core_max = 5.0
        self.ttl_near_min = 4.0
        self.ttl_near_max = 10.0
        self.ttl_mid_min = 8.0
        self.ttl_mid_max = 20.0
        self.ttl_far_min = 15.0
        self.ttl_far_max = 30.0

        # --- backstop ---
        self.backstop_offsets = [2.0, 2.5, 3.0, 3.5]
        # дальние стены тоже облегчаем: 2% капитала вместо 5
        self.backstop_depth_ratio = 0.007

        # --- структурные неровности ---
        self.cluster_price_fracs = [0.00, 0.25, 0.50, 0.75]
        self.cluster_eps = 0.01
        self.cluster_mult_min = 1.2
        self.cluster_mult_max = 1.8

        # дырявые зоны (тонкая книга)
        self.hole_bands = []  # список {low, high, until}
        # увеличиваем частоту и ширину дыр, чтобы были реальные проскоки
        self.hole_prob = 0.07
        self.hole_min_offset = 0.25
        self.hole_max_offset = 1.8
        self.hole_min_width = 0.10
        self.hole_max_width = 0.25
        self.hole_depth_scale = 0.16
        self.hole_max_bands = 5

        # "липкие" уровни-стены
        self.sticky_levels_bid = {}  # price -> {mult, until}
        self.sticky_levels_ask = {}
        # немного увеличиваем шанс липких уровней, но ослабляем сам размер
        self.sticky_prob = 0.03
        self.max_sticky_levels = 4
        self.sticky_min_offset = 0.30
        self.sticky_max_offset = 1.5
        self.sticky_mult_min = 1.5
        self.sticky_mult_max = 2.0
        self.sticky_ttl_min = 45.0
        self.sticky_ttl_max = 120.0

        # --- bootstrap ---
        self.bootstrap_active = False
        self.bootstrap_deadline = 0.0
        self.bootstrap_queue = []
        self.existing_liquidity = {'bid': {}, 'ask': {}}

        # --- учёт ордеров LP ---
        self.all_active_orders = []         # все живые ордера LP

        # --- инвентарь / PnL / режим риска ---
        self.position = 0.0
        self.cash = float(capital)
        self.inv_max_ratio = 0.20          # максимум |pos*mid|/capital (чуть строже)

        self.last_mid = None
        self.mark_mid = None               # для mark-to-market
        self.equity = float(capital)       # cash + pos*mark_mid
        self.max_equity = float(capital)
        self.min_equity = float(capital)
        self.rolling_pnl = []              # последние N значений equity
        self.rolling_pnl_maxlen = 600

        # режим риска: "normal" / "stressed" / "off"
        self.risk_mode = "normal"
        self.drawdown_stressed = 0.03      # -3%
        self.drawdown_off = 0.06           # -6%

        # --- краткосрочный тренд / вола ---
        self.trend_ema_fast = 0.0
        self.trend_ema_slow = 0.0
        self.vol_ema = 0.0
        self.trend_alpha_fast = 0.25
        self.trend_alpha_slow = 0.08
        self.vol_alpha = 0.15

        # --- память по недавним экстремумам ---
        self.recent_highs = []
        self.recent_lows = []
        self.extrema_maxlen = 20
        self.last_extreme_mid = None
        self.extreme_threshold = 0.12  # относительное движение для фиксации экстремума (в %)

        # --- память по недавнему агрессивному потоку ---
        self.last_flow_update_ts = 0.0
        self.flow_buy_volume = 0.0
        self.flow_sell_volume = 0.0
        self.flow_window_sec = 30.0

    # ============================================================
    # BOOTSTRAP
    # ============================================================
    def begin_bootstrap(self, saved_liq: dict, seconds: float = 15.0):
        self.bootstrap_queue.clear()

        def _norm(side):
            out = []
            for px, vol in (side or {}).items():
                try:
                    p2 = round(float(px), 3)
                    v2 = float(vol)
                    if v2 > 0:
                        out.append((p2, v2))
                except Exception:
                    continue
            return out

        bids = sorted(_norm(saved_liq.get('bids')), key=lambda x: -x[0])
        asks = sorted(_norm(saved_liq.get('asks')), key=lambda x: x[0])

        for p, v in bids:
            self.bootstrap_queue.append(('bid', p, v))
            self.existing_liquidity['bid'][p] = v

        for p, v in asks:
            self.bootstrap_queue.append(('ask', p, v))
            self.existing_liquidity['ask'][p] = v

        self.bootstrap_active = True
        base_now = getattr(self, "_now_ts", time.time())
        self.bootstrap_deadline = base_now + float(seconds)

    # ============================================================
    # HELPERS
    # ============================================================
    def _cleanup_expired(self, now: float):
        alive = []
        for o in self.all_active_orders:
            ttl = getattr(o, "ttl_expire", None)
            if ttl is None or ttl > now:
                alive.append(o)
        self.all_active_orders = alive

    def _own_volume_at(self, price: float, side: OrderSide) -> float:
        return sum(
            o.volume
            for o in self.all_active_orders
            if o.price == price and o.side == side
        )

    def _depth_global_scale(self, mid: float) -> float:
        """
        Глобальный множитель g, чтобы суммарная глубина
        в среднем не превышала depth_cap_ratio от капитала.
        """
        n_levels = max(1, self.levels - self.start_i + 1)
        est_avg_qty = self.base_depth_qty * 0.6
        est_notional = est_avg_qty * float(mid) * n_levels
        cap = self.capital * self.depth_cap_ratio
        if est_notional <= 0:
            return 1.0
        g = cap / est_notional
        # нижняя граница очень маленькая: LP может почти уйти
        return max(0.01, min(2.0, g))

    def _depth_level_shape(self, i: int) -> float:
        j = max(0, i - self.start_i + 1)
        return math.exp(-self.depth_lambda * j)

    def _ttl_for_depth_level(self, offset: float, risk_mode: str) -> float:
        """
        TTL в зависимости от удаленности и режима риска.
        В risk-off LP резко очищает ближние уровни.
        """
        if offset < 0.25:
            base_min, base_max = self.ttl_near_min, self.ttl_near_max
        elif offset < 1.0:
            base_min, base_max = self.ttl_mid_min, self.ttl_mid_max
        else:
            base_min, base_max = self.ttl_far_min, self.ttl_far_max

        if risk_mode == "stressed":
            if offset < 0.5:
                base_min *= 0.5
                base_max *= 0.7
        elif risk_mode == "off":
            if offset < 0.7:
                base_min *= 0.2
                base_max *= 0.35
            else:
                base_min *= 0.7
                base_max *= 0.9

        return random.uniform(base_min, base_max)

    def _make_order(self, side, price, visible, hidden, now, ttl: float):
        o = Order(
            order_id=uuid.uuid4().hex,
            agent_id=self.agent_id,
            side=side,
            price=float(price),
            volume=float(visible),
            order_type=OrderType.LIMIT,
        )
        o.metadata = {"hidden": float(hidden)}
        o.ttl = float(ttl)
        o.ttl_expire = now + float(ttl)
        self.all_active_orders.append(o)
        return o

    # ---- инвентарь / тренд / риск ----
    def _update_trend_state(self, mid: float):
        if self.last_mid is None:
            self.last_mid = mid
            self.mark_mid = mid
            return
        diff = mid - self.last_mid
        self.trend_ema_fast = (1.0 - self.trend_alpha_fast) * self.trend_ema_fast + self.trend_alpha_fast * diff
        self.trend_ema_slow = (1.0 - self.trend_alpha_slow) * self.trend_ema_slow + self.trend_alpha_slow * diff
        self.vol_ema = (1.0 - self.vol_alpha) * self.vol_ema + self.vol_alpha * abs(diff)
        self.last_mid = mid

    def _inventory_ratio(self, mid: float) -> float:
        if self.capital <= 0.0:
            return 0.0
        notional = self.position * float(mid)
        r = notional / self.capital
        if r > self.inv_max_ratio:
            r = self.inv_max_ratio
        elif r < -self.inv_max_ratio:
            r = -self.inv_max_ratio
        return r

    def _trend_bias(self) -> float:
        if self.vol_ema <= 0.0:
            return 0.0
        fast = self.trend_ema_fast
        slow = self.trend_ema_slow
        base = fast
        if fast * slow > 0:
            base = 0.5 * fast + 0.5 * slow
        b = base / (self.vol_ema * 3.0)
        if b > 1.0:
            b = 1.0
        elif b < -1.0:
            b = -1.0
        return b

    def _update_equity_and_risk_mode(self, mid: float):
        if self.mark_mid is None:
            self.mark_mid = mid
        self.equity = self.cash + self.position * mid
        self.max_equity = max(self.max_equity, self.equity)
        self.min_equity = min(self.min_equity, self.equity)

        if self.rolling_pnl_maxlen > 0:
            self.rolling_pnl.append(self.equity)
            if len(self.rolling_pnl) > self.rolling_pnl_maxlen:
                self.rolling_pnl.pop(0)

        if self.max_equity > 0:
            dd = (self.equity - self.max_equity) / self.max_equity
        else:
            dd = 0.0

        if dd <= -self.drawdown_off:
            self.risk_mode = "off"
        elif dd <= -self.drawdown_stressed:
            self.risk_mode = "stressed"
        else:
            self.risk_mode = "normal"

    def _side_scales(self, bias: float, risk_mode: str):
        """
        bias > 0 → LP хочет распродаться (больше ask, меньше bid)
        bias < 0 → LP хочет набирать (больше bid, меньше ask)
        """
        b = max(-1.0, min(1.0, bias))
        bid_scale = max(0.2, 1.0 - 0.5 * b)
        ask_scale = max(0.2, 1.0 + 0.5 * b)

        if risk_mode == "stressed":
            bid_scale *= 0.8
            ask_scale *= 0.8
        elif risk_mode == "off":
            bid_scale *= 0.45
            ask_scale *= 0.45

        return bid_scale, ask_scale

    # ---- агрессивный поток ----
    def _update_flow_state(self, market_context, now: float):
        if market_context is None:
            return

        if isinstance(market_context, dict) or hasattr(market_context, "get"):
            try:
                buy_v = float(market_context.get("aggr_buy_volume", 0.0))
                sell_v = float(market_context.get("aggr_sell_volume", 0.0))
                window = float(market_context.get("window_sec", self.flow_window_sec))
            except Exception:
                buy_v = 0.0
                sell_v = 0.0
                window = self.flow_window_sec

            if self.last_flow_update_ts > 0:
                decay = math.exp(
                    -max(0.0, now - self.last_flow_update_ts) / max(window, 1e-6)
                )
            else:
                decay = 0.0

            self.flow_buy_volume = self.flow_buy_volume * decay + buy_v
            self.flow_sell_volume = self.flow_sell_volume * decay + sell_v
            self.last_flow_update_ts = now
            return

        skew = 0.0
        if hasattr(market_context, "get_demand_supply_skew"):
            try:
                skew = float(market_context.get_demand_supply_skew())
            except Exception:
                skew = 0.0
        else:
            self.flow_buy_volume = 0.0
            self.flow_sell_volume = 0.0
            return

        if not math.isfinite(skew):
            skew = 0.0

        skew = max(-1.0, min(1.0, skew))
        total = 1.0
        self.flow_buy_volume = (1.0 + skew) * 0.5 * total
        self.flow_sell_volume = (1.0 - skew) * 0.5 * total
        self.last_flow_update_ts = now

    def _flow_bias(self) -> float:
        total = self.flow_buy_volume + self.flow_sell_volume
        if total <= 0.0:
            return 0.0
        b = (self.flow_buy_volume - self.flow_sell_volume) / total
        return max(-0.7, min(0.7, b))

    # ---- экстремумы для кластеров/стен ----
    def _update_extremes(self, mid: float):
        if self.last_extreme_mid is None:
            self.last_extreme_mid = mid
            return

        move = mid - self.last_extreme_mid
        if self.last_extreme_mid > 0:
            rel = abs(move) / self.last_extreme_mid
        else:
            rel = 0.0

        if rel >= self.extreme_threshold / 100.0:
            if move > 0:
                self.recent_lows.append(self.last_extreme_mid)
                if len(self.recent_lows) > self.extrema_maxlen:
                    self.recent_lows.pop(0)
            else:
                self.recent_highs.append(self.last_extreme_mid)
                if len(self.recent_highs) > self.extrema_maxlen:
                    self.recent_highs.pop(0)
            self.last_extreme_mid = mid

    # ---- структурные паттерны ----
    def _update_structural_patterns(self, mid: float, now: float):
        self.hole_bands = [b for b in self.hole_bands if b["until"] > now]

        if random.random() < self.hole_prob and len(self.hole_bands) < self.hole_max_bands:
            center = random.uniform(self.hole_min_offset, self.hole_max_offset)
            width = random.uniform(self.hole_min_width, self.hole_max_width)
            low = max(0.18, center - width / 2.0)
            high = center + width / 2.0
            ttl = random.uniform(25.0, 120.0)
            self.hole_bands.append({"low": low, "high": high, "until": now + ttl})

        self.sticky_levels_bid = {
            p: info for p, info in self.sticky_levels_bid.items()
            if info["until"] > now
        }
        self.sticky_levels_ask = {
            p: info for p, info in self.sticky_levels_ask.items()
            if info["until"] > now
        }

        if random.random() < self.sticky_prob:
            if len(self.sticky_levels_bid) < self.max_sticky_levels and self.recent_lows:
                base_low = random.choice(self.recent_lows)
                off = abs(base_low - mid)
                if self.sticky_min_offset <= off <= self.sticky_max_offset:
                    p_bid = round(base_low, 3)
                else:
                    rand_off = random.uniform(self.sticky_min_offset, self.sticky_max_offset)
                    p_bid = round(mid - rand_off, 3)
                mult = random.uniform(self.sticky_mult_min, self.sticky_mult_max)
                ttl = random.uniform(self.sticky_ttl_min, self.sticky_ttl_max)
                self.sticky_levels_bid[p_bid] = {"mult": mult, "until": now + ttl}

            if len(self.sticky_levels_ask) < self.max_sticky_levels and self.recent_highs:
                base_high = random.choice(self.recent_highs)
                off = abs(base_high - mid)
                if self.sticky_min_offset <= off <= self.sticky_max_offset:
                    p_ask = round(base_high, 3)
                else:
                    rand_off = random.uniform(self.sticky_min_offset, self.sticky_max_offset)
                    p_ask = round(mid + rand_off, 3)
                mult = random.uniform(self.sticky_mult_min, self.sticky_mult_max)
                ttl = random.uniform(self.sticky_ttl_min, self.sticky_ttl_max)
                self.sticky_levels_ask[p_ask] = {"mult": mult, "until": now + ttl}

    def _hole_scale_for_offset(self, offset: float) -> float:
        for b in self.hole_bands:
            if b["low"] <= offset <= b["high"]:
                return self.hole_depth_scale
        return 1.0

    def _cluster_multiplier_for_price(self, price: float, mid: float) -> float:
        mult = 1.0
        frac = price - math.floor(price)
        for cf in self.cluster_price_fracs:
            if abs(frac - cf) <= self.cluster_eps:
                mult *= random.uniform(self.cluster_mult_min, self.cluster_mult_max)
                break

        for ext in (self.recent_highs + self.recent_lows):
            if abs(ext - price) <= 0.02:
                mult *= random.uniform(1.4, 2.3)
                break

        return mult

    def _sticky_mult_for_price(self, price: float, side: OrderSide) -> float:
        if side == OrderSide.BID:
            info = self.sticky_levels_bid.get(price)
        else:
            info = self.sticky_levels_ask.get(price)
        if not info:
            return 1.0
        return float(info.get("mult", 1.0))

    # ------------------------------------------------------------
    # CORE BAND (по умолчанию LP не участвует)
    # ------------------------------------------------------------
    def _build_core_band(self, mid: float, now: float, bid_scale: float, ask_scale: float, risk_mode: str):
        return []

    # ------------------------------------------------------------
    # MAIN
    # ------------------------------------------------------------
    def generate_orders(self, order_book, market_context=None):
        out = []
        now = _ctx_now(market_context)
        self._now_ts = now

        self._cleanup_expired(now)

        if self.bootstrap_active:
            batch = []
            for _ in range(2500):
                if not self.bootstrap_queue:
                    break
                side, px, vol = self.bootstrap_queue.pop(0)
                o = self._make_order(
                    OrderSide.BID if side == 'bid' else OrderSide.ASK,
                    px,
                    vol,
                    0.0,
                    now,
                    ttl=random.uniform(self.ttl_mid_min, self.ttl_mid_max)
                )
                batch.append(o)
            if not self.bootstrap_queue or now >= self.bootstrap_deadline:
                self.bootstrap_active = False
            return batch

        bid = order_book._best_bid_price()
        ask = order_book._best_ask_price()
        if bid is None or ask is None:
            return []
        mid = round((bid + ask) / 2.0, 5)

        self._update_trend_state(mid)
        self._update_extremes(mid)
        self._update_flow_state(market_context, now)
        self._update_equity_and_risk_mode(mid)
        self._update_structural_patterns(mid, now)

        inv_ratio = self._inventory_ratio(mid)
        trend_b = self._trend_bias()
        flow_b = self._flow_bias()

        if self.inv_max_ratio > 0:
            inv_component = inv_ratio / self.inv_max_ratio
        else:
            inv_component = 0.0

        side_bias = 0.55 * inv_component + 0.30 * trend_b + 0.15 * flow_b
        side_bias = max(-1.0, min(1.0, side_bias))

        bid_scale, ask_scale = self._side_scales(side_bias, self.risk_mode)

        depth_orders = self._build_depth_grid(mid, now, bid_scale, ask_scale, side_bias)
        out.extend(depth_orders)

        backstop_orders = self._build_backstops(mid, now, bid_scale, ask_scale)
        out.extend(backstop_orders)

        return out

    # ------------------------------------------------------------
    # DEPTH GRID
    # ------------------------------------------------------------
    def _profile_multiplier(self, profile: str, offset: float, side_bias: float) -> float:
        if profile == "mean_reverter":
            return 1.0 + 0.5 * (-side_bias)
        if profile == "trend_accepter":
            return 1.0 + 0.5 * side_bias
        if profile == "sticky_liquidity":
            if 0.4 <= offset <= 1.2:
                return 1.25
            return 0.9
        return 1.0

    def _iceberg_prob_for_offset(self, offset: float) -> float:
        if offset < 0.25:
            return self.iceberg_prob_near
        if offset < 1.0:
            return self.iceberg_prob_mid
        return self.iceberg_prob_far

    def _build_depth_grid(self, mid: float, now: float, bid_scale: float, ask_scale: float, side_bias: float):
        orders = []
        g = self._depth_global_scale(mid)

        for i in range(self.start_i, self.levels + 1):
            offset = i * self.tick_step
            shape = self._depth_level_shape(i)
            noise = 1.0 + random.uniform(-self.depth_noise_amp, self.depth_noise_amp)

            qty_raw = self.base_depth_qty * shape * g * noise
            if qty_raw <= 0.0:
                continue

            hole_scale = self._hole_scale_for_offset(offset)
            qty_raw *= hole_scale
            if qty_raw <= 0.0:
                continue

            profile_mult = 0.0
            for name, w in self.profile_weights.items():
                profile_mult += w * self._profile_multiplier(name, offset, side_bias)
            qty_raw *= profile_mult

            qty_raw = max(1000.0, min(qty_raw, 35000.0))

            if offset < 0.25:
                skip_p = self.skip_prob_near
            elif offset < 1.0:
                skip_p = self.skip_prob_mid
            else:
                skip_p = self.skip_prob_far

            if skip_p > 0.0 and random.random() < skip_p:
                continue

            p_bid = round(mid - offset, 3)
            p_ask = round(mid + offset, 3)

            cluster_bid = self._cluster_multiplier_for_price(p_bid, mid)
            cluster_ask = self._cluster_multiplier_for_price(p_ask, mid)
            sticky_bid = self._sticky_mult_for_price(p_bid, OrderSide.BID)
            sticky_ask = self._sticky_mult_for_price(p_ask, OrderSide.ASK)

            base_bid = qty_raw * cluster_bid * sticky_bid * bid_scale
            base_ask = qty_raw * cluster_ask * sticky_ask * ask_scale

            ttl = self._ttl_for_depth_level(offset, self.risk_mode)

            # BID
            if base_bid > 0.0:
                iceberg_prob = self._iceberg_prob_for_offset(offset)
                if random.random() < iceberg_prob:
                    vis_frac = random.uniform(0.18, 0.40)
                    visible_target = max(self.iceberg_min_vis,
                                         min(self.iceberg_max_vis, base_bid * vis_frac))
                    ratio = random.uniform(self.iceberg_min_ratio, self.iceberg_max_ratio)
                    total_target = min(visible_target * ratio, base_bid * 2.5)
                    hidden_target = max(0.0, total_target - visible_target)
                else:
                    visible_target = base_bid
                    hidden_target = 0.0

                have_bid = self._own_volume_at(p_bid, OrderSide.BID)
                need_visible = visible_target - have_bid
                if need_visible > 400.0:
                    place_visible = min(need_visible, self.place_chunk_depth)
                    hidden_for_order = hidden_target * (place_visible / visible_target) if visible_target > 0 else 0.0
                    o = self._make_order(
                        OrderSide.BID,
                        p_bid,
                        place_visible,
                        hidden_for_order,
                        now,
                        ttl=ttl
                    )
                    orders.append(o)

            # ASK
            if base_ask > 0.0:
                iceberg_prob = self._iceberg_prob_for_offset(offset)
                if random.random() < iceberg_prob:
                    vis_frac = random.uniform(0.18, 0.40)
                    visible_target = max(self.iceberg_min_vis,
                                         min(self.iceberg_max_vis, base_ask * vis_frac))
                    ratio = random.uniform(self.iceberg_min_ratio, self.iceberg_max_ratio)
                    total_target = min(visible_target * ratio, base_ask * 2.5)
                    hidden_target = max(0.0, total_target - visible_target)
                else:
                    visible_target = base_ask
                    hidden_target = 0.0

                have_ask = self._own_volume_at(p_ask, OrderSide.ASK)
                need_visible = visible_target - have_ask
                if need_visible > 400.0:
                    place_visible = min(need_visible, self.place_chunk_depth)
                    hidden_for_order = hidden_target * (place_visible / visible_target) if visible_target > 0 else 0.0
                    o = self._make_order(
                        OrderSide.ASK,
                        p_ask,
                        place_visible,
                        hidden_for_order,
                        now,
                        ttl=ttl
                    )
                    orders.append(o)

        return orders

    # ------------------------------------------------------------
    # BACKSTOPS
    # ------------------------------------------------------------
    def _build_backstops(self, mid: float, now: float, bid_scale: float, ask_scale: float):
        orders = []
        back_cap = self.capital * self.backstop_depth_ratio / 2.0
        base_qty = back_cap / max(mid, 1e-6)
        base_qty = max(3000.0, base_qty)

        if self.risk_mode == "stressed":
            base_qty *= 0.8
        elif self.risk_mode == "off":
            base_qty *= 0.5

        for off in self.backstop_offsets:
            p_bid = round(mid - off, 3)
            p_ask = round(mid + off, 3)

            cluster_bid = self._cluster_multiplier_for_price(p_bid, mid)
            cluster_ask = self._cluster_multiplier_for_price(p_ask, mid)
            sticky_bid = self._sticky_mult_for_price(p_bid, OrderSide.BID)
            sticky_ask = self._sticky_mult_for_price(p_ask, OrderSide.ASK)

            qty_bid_target = base_qty * cluster_bid * sticky_bid * bid_scale
            qty_ask_target = base_qty * cluster_ask * sticky_ask * ask_scale

            have_bid = self._own_volume_at(p_bid, OrderSide.BID)
            need_bid = qty_bid_target - have_bid
            if need_bid > 800.0:
                ttl = random.uniform(self.ttl_far_min, self.ttl_far_max)
                if self.risk_mode == "off":
                    ttl *= 1.4
                o1 = self._make_order(
                    OrderSide.BID,
                    p_bid,
                    need_bid,
                    0.0,
                    now,
                    ttl=ttl
                )
                orders.append(o1)

            have_ask = self._own_volume_at(p_ask, OrderSide.ASK)
            need_ask = qty_ask_target - have_ask
            if need_ask > 800.0:
                ttl = random.uniform(self.ttl_far_min, self.ttl_far_max)
                if self.risk_mode == "off":
                    ttl *= 1.4
                o2 = self._make_order(
                    OrderSide.ASK,
                    p_ask,
                    need_ask,
                    0.0,
                    now,
                    ttl=ttl
                )
                orders.append(o2)

        return orders

    # ============================================================
    # FILL LOGIC — айсберг refill + инвентарь / PnL
    # ============================================================
    def on_order_filled(self, order_id, price, qty, side):
        price = float(price)
        qty = float(qty)

        for o in list(self.all_active_orders):
            if o.order_id == order_id:
                if side == OrderSide.BID:
                    self.position += qty
                    self.cash -= qty * price
                elif side == OrderSide.ASK:
                    self.position -= qty
                    self.cash += qty * price

                hidden = float(o.metadata.get("hidden", 0.0))
                if hidden > 0.0:
                    refill = min(hidden, qty)
                    hidden -= refill
                    o.metadata["hidden"] = hidden
                    o.volume += refill
                else:
                    self.all_active_orders.remove(o)
                return