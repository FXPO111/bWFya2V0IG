# retail_trader.py
#
# RetailNoise: пул мелких маркет-агрессоров, создающих живой фон.
# Делает кластерную, асимметричную активность, завязанную на последние цены и локальную глубину.

import uuid
import random
from collections import deque

import numpy as np

from backend.core.order import Order, OrderSide, OrderType


class NoiseSubAgent:
    """
    Один розничный участник.
    Торгует только маркетами. Никаких лимитов, никаких умных стратегий — чистый шум,
    но с небольшим bias по тренду/контртренду и по тонкой стороне стакана.
    """

    def __init__(self, agent_id: str, capital: float, style: str = "noise"):
        self.agent_id = agent_id
        self.capital = float(capital)
        self.style = style  # "noise", "trend_chaser", "mean_reverter", "liquidity_sensitive"

        # Позиция для внутренней психологии (в PnL движок не лезем)
        self.position = 0.0
        self.entry_price = None
        self.hold_steps = 0

        # Поведенческие параметры
        self.fatigue = np.random.uniform(0.02, 0.15)      # шанс "забил на рынок" в тик
        self.mood = np.random.uniform(0.7, 1.3)           # агрессивность
        self.tilt = np.random.uniform(-0.05, 0.05)        # лёгкий перекос в активность
        self.activity_rate = np.random.uniform(0.15, 0.40)  # базовая вероятность действия
        self.reaction_delay = random.randint(1, 3)        # раз в N тиков вообще что-то решает

        # Риск и объём
        self.risk_per_trade = np.random.uniform(0.0005, 0.003)  # доля капитала на сделку
        self.size_vol = np.random.uniform(0.35, 0.65)           # разброс вокруг базового размера

        # Кулдаун между сделками, чтобы не спамил
        self.cooldown = 0
        self.cooldown_range = (1, 4)

        # Окно для микро-динамики цены
        self.price_window = 10
        self.last_prices: deque[float] = deque(maxlen=self.price_window)

    # ----------------- служебки -----------------

    @staticmethod
    def _safe_best_price(book, name, default=None):
        fn = getattr(book, name, None)
        if fn is None:
            return default
        return fn() if callable(fn) else fn

    @staticmethod
    def _l1_depth(book):
        """
        Возвращает (best_bid, bid_sz1, best_ask, ask_sz1)
        bid_sz1 / ask_sz1 считаем по видимой части уровня.
        """
        from backend.core.order_book import PriceLevel  # чтобы не создавать жёсткого цикла на импорт уровне модуля

        best_bid = NoiseSubAgent._safe_best_price(book, "_best_bid_price")
        best_ask = NoiseSubAgent._safe_best_price(book, "_best_ask_price")

        bid_sz = 0.0
        ask_sz = 0.0

        if best_bid is not None and isinstance(book.bids.get(best_bid), PriceLevel):
            try:
                bid_sz = float(book.bids[best_bid].total_volume_visible())
            except Exception:
                bid_sz = 0.0

        if best_ask is not None and isinstance(book.asks.get(best_ask), PriceLevel):
            try:
                ask_sz = float(book.asks[best_ask].total_volume_visible())
            except Exception:
                ask_sz = 0.0

        return best_bid, bid_sz, best_ask, ask_sz

    def _short_return_and_vol(self):
        """
        Простейшая микро-доходность и локальная вола на окне last_prices.
        """
        if len(self.last_prices) < 2:
            return 0.0, 0.0
        arr = np.array(self.last_prices, dtype=float)
        rets = np.diff(arr) / arr[:-1]
        if rets.size == 0:
            return 0.0, 0.0
        r = float(rets[-1])
        vol = float(np.std(rets))
        return r, vol

    # ----------------- основная логика -----------------

    def decide(self, order_book, current_step: int, session_mult: float = 1.0):
        """
        Возвращает либо Order(MARKET), либо None.
        """
        last_price = getattr(order_book, "last_trade_price", None)
        if last_price is None:
            return None

        # Обновляем окно цен
        self.last_prices.append(float(last_price))

        # Фильтры "человек сегодня не в настроении"
        if random.random() < self.fatigue:
            return None

        if self.cooldown > 0:
            self.cooldown -= 1
            return None

        if current_step % self.reaction_delay != 0:
            return None

        # Базовая вероятность действия
        base_p = self.activity_rate * self.mood * session_mult + self.tilt
        base_p = max(0.0, min(0.8, base_p))  # не даём уйти в экстремы

        if random.random() > base_p:
            return None

        short_ret, vol = self._short_return_and_vol()
        best_bid, bid_sz1, best_ask, ask_sz1 = self._l1_depth(order_book)

        # Направление
        side = self._choose_side(short_ret, vol, bid_sz1, ask_sz1)
        if side is None:
            return None

        # Объём сделки
        size = self._sample_size(last_price)
        if size <= 0.0:
            return None

        # Кулдаун после сделки
        self.cooldown = random.randint(*self.cooldown_range)

        md = {
            "role": "retail_noise",
            "style": self.style,
        }

        return Order(
            order_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            side=side,
            volume=float(size),
            price=None,
            order_type=OrderType.MARKET,
            ttl=None,
            metadata=md
        )

    # ----------------- детали принятия решения -----------------

    def _choose_side(self, short_ret: float, vol: float, bid_sz1: float, ask_sz1: float):
        """
        Разные стили принимают решение по-своему.
        Возвращает OrderSide или None.
        """
        # Защита от деления на ноль
        depth_sum = bid_sz1 + ask_sz1
        if depth_sum > 0:
            imbalance = (bid_sz1 - ask_sz1) / depth_sum  # >0 — бид толще, <0 — аск толще
        else:
            imbalance = 0.0

        # Чистый шум с лёгким уклоном в сторону микро-движения
        if self.style == "noise":
            bias = 0.5 + 0.35 * np.tanh(short_ret * 8.0)
            # немного учитываем тонкую сторону стакана: чаще бьём туда
            bias += -0.15 * np.tanh(imbalance * 4.0)
            bias = max(0.05, min(0.95, bias))
            return OrderSide.BID if random.random() < bias else OrderSide.ASK

        # Догоняет движение
        if self.style == "trend_chaser":
            if abs(short_ret) < 1e-5:
                # если движения нет — рандом
                return random.choice([OrderSide.BID, OrderSide.ASK])
            return OrderSide.BID if short_ret > 0 else OrderSide.ASK

        # Контртрендовик
        if self.style == "mean_reverter":
            if abs(short_ret) < 1e-5:
                return random.choice([OrderSide.BID, OrderSide.ASK])
            return OrderSide.ASK if short_ret > 0 else OrderSide.BID

        # Смотрит на тонкую сторону L1: чаще бьёт по тонкой
        if self.style == "liquidity_sensitive":
            if bid_sz1 <= 0 and ask_sz1 <= 0:
                return random.choice([OrderSide.BID, OrderSide.ASK])
            if bid_sz1 < ask_sz1:
                # тонкий бид — проще продавить вниз
                return OrderSide.ASK
            elif ask_sz1 < bid_sz1:
                # тонкий аск — проще выдавить вверх
                return OrderSide.BID
            else:
                return random.choice([OrderSide.BID, OrderSide.ASK])

        # На всякий случай — чистый рандом
        return random.choice([OrderSide.BID, OrderSide.ASK])

    def _sample_size(self, last_price: float) -> float:
        """
        Размер сделки: логнормаль вокруг базового риск-объёма.
        Не лезем в объёмы, которые могут сломать книгу.
        """
        if last_price <= 0:
            return 0.0

        # Базовый размер в контрактах = риск * капитал / цена
        base_qty = self.risk_per_trade * self.capital / last_price
        base_qty = max(0.2, base_qty)  # минимальный видимый объём

        # Логнормальное распределение вокруг base_qty
        mean = np.log(base_qty + 1e-9)
        sigma = self.size_vol
        raw = float(np.random.lognormal(mean=mean, sigma=sigma))

        # Ограничение сверху: розничный шум не должен быть гигантом
        max_qty = (0.02 * self.capital) / last_price  # до ~2% капитала в одну сделку
        max_qty = max(max_qty, base_qty)
        qty = max(0.0, min(raw, max_qty))

        # Чуть сгладим до "адекватных" значений
        return round(qty, 4)


class RetailTrader:
    """
    Пул розничных шумовых агентов.
    Единственная точка входа для движка: generate_orders(order_book, market_context=None)
    """

    def __init__(self, agent_id: str, capital: float, num_subagents: int = 50):
        self.agent_id = agent_id
        self.capital = float(capital)
        self.num_subagents = int(num_subagents)

        # Разваливаем капитал равномерно. Для реализма можно потом весить по стилям.
        per_cap = self.capital / max(self.num_subagents, 1)

        styles_pool = (
            ["noise"] * 25
            + ["trend_chaser"] * 10
            + ["mean_reverter"] * 10
            + ["liquidity_sensitive"] * 5
        )

        self.subagents = []
        for i in range(self.num_subagents):
            style = random.choice(styles_pool)
            sub_id = f"{agent_id}_sub_{i}"
            self.subagents.append(NoiseSubAgent(sub_id, per_cap, style))

        self.current_step = 0
        self.price_history: deque[float] = deque(maxlen=500)

    # ----------------- сессионные множители -----------------

    @staticmethod
    def _session_activity_multiplier(market_context) -> float:
        """
        Пытаемся вытащить фазу/сессию из MarketContextAdvanced, но без жёстких зависимостей.
        Если ничего не понятно — возвращаем 1.0.
        """
        if market_context is None:
            return 1.0

        name = None

        # Частый вариант — market_context.phase.name
        phase = getattr(market_context, "phase", None)
        if phase is not None and hasattr(phase, "name"):
            name = str(phase.name).lower()
        # Либо что-то вроде market_context.session
        if name is None:
            sess = getattr(market_context, "session", None)
            if sess is not None:
                name = str(sess).lower()

        if not name:
            return 1.0

        # Грубая карта: Азия тухлая, Лондон и NY — активнее
        if "asia" in name or "tokyo" in name:
            return 0.35
        if "london" in name or "europe" in name:
            return 1.0
        if "ny" in name or "newyork" in name or "us" in name or "america" in name:
            return 1.2

        return 1.0

    # ----------------- точка входа для движка -----------------

    def generate_orders(self, order_book, market_context=None):
        """
        Главный метод, который дергает server.py.
        Возвращает список маркет-ордеров от всех субагентов на этот тик.
        """
        last_price = getattr(order_book, "last_trade_price", None)
        if last_price is not None:
            self.price_history.append(float(last_price))

        self.current_step += 1
        session_mult = self._session_activity_multiplier(market_context)

        orders = []
        for sub in self.subagents:
            o = sub.decide(order_book, self.current_step, session_mult=session_mult)
            if o is not None:
                orders.append(o)

        return orders
