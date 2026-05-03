# order.py

import time
from enum import Enum
from typing import Optional
import logging

class OrderSide(Enum):
    BID = "bid"
    ASK = "ask"

class OrderType(Enum):
    LIMIT = "limit"
    MARKET = "market"
    CANCEL = "cancel"

class Order:
    def __init__(
        self,
        order_id: str,
        agent_id: str,
        side: OrderSide,
        volume: float,
        price: Optional[float] = None,
        order_type: OrderType = OrderType.LIMIT,
        ttl: Optional[int] = None,
        metadata=None
    ):
        """
        :param order_id: уникальный идентификатор ордера
        :param agent_id: идентификатор агента, который создал этот ордер
        :param side: OrderSide.BID (покупка) или OrderSide.ASK (продажа)
        :param volume: начальный объём
        :param price: цена (для лимитного ордера), None для рыночного
        :param order_type: OrderType.LIMIT или OrderType.MARKET
        :param ttl: время жизни (в "тиках"); None = бессрочно
        """
        self.order_id = order_id
        self.agent_id = agent_id
        self.side = side
        self.volume = volume
        self.price = price
        self.order_type = order_type
        self.ttl = ttl
        self.metadata = metadata or {}

        self.filled_volume = 0.0
        self.timestamp = time.time()

        self.source: Optional[str] = None

    def remaining_volume(self) -> float:
        """
        Сколько ещё объёма осталось исполнить.
        """
        return max(self.volume - self.filled_volume, 0.0)

    def is_active(self) -> bool:
        """
        Ордер активен, если остался объём AND (TTL не задан или более 0).
        """
        still_have = self.remaining_volume() > 0
        if self.ttl is not None:
            return still_have and (self.ttl > 0)
        return still_have

    def fill(self, qty: float) -> float:
        """
        Заполняет ордер в объёме min(qty, оставшийся объём).
        Возвращает фактически заполненный объём.
        """
        fill_qty = min(qty, self.remaining_volume())
        self.filled_volume += fill_qty
        return fill_qty

    def tick_ttl(self):
        """
        Уменьшает TTL на 1 (если он задан).
        """
        if self.ttl is not None:
            self.ttl = max(self.ttl - 1, 0)

    def cancel(self):
        """
        Отменяет ордер: TTL = 0, объём считается заполненным.
        """
        self.ttl = 0
        # Чтобы remaining_volume() точно стал 0:
        self.filled_volume = self.volume

    def to_dict(self):
        try:
            return {
                "order_id": self.order_id,
                "agent_id": self.agent_id,
                "side": self.side.name,
                "type": self.order_type.name,
                "price": self.price,
                "volume": self.volume,
                "filled": self.filled_volume,
                "timestamp": self.timestamp,
                "ttl": self.ttl
            }
        except Exception as e:
            logging.error(f"Error in Order.to_dict(): {e}")
            return {}


    def __repr__(self):
        return (f"Order(id={self.order_id}, agent={self.agent_id}, side={self.side.value}, "
                f"price={self.price}, volume={self.volume}, filled={self.filled_volume}, ttl={self.ttl})")


# ──────────────────────────────────────────────────────────────
#  Global price tick & quantiser (0.05 pip = 0.00005)
# ──────────────────────────────────────────────────────────────
TICK = 0.001


def quant(price: float) -> float:
    """Snap *price* to the nearest 0.01-pip grid (5 decimals)."""
    steps = round(price / TICK)          # nearest grid index
    return round(steps * TICK, 5)        # back to float, 5 d.p.
