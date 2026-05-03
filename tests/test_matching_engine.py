import pytest
import uuid
from matching_engine import MatchingEngine
from order import Order, OrderSide, OrderType


def make_order(side, price=None, volume=1.0, agent_id="test_agent", ttl=None):
    return Order(
        order_id=str(uuid.uuid4()),
        side=side,
        price=price,
        volume=volume,
        agent_id=agent_id,
        order_type=OrderType.MARKET if price is None else OrderType.LIMIT,
        ttl=ttl,
    )


def test_market_buy_fills_against_existing_asks():
    engine = MatchingEngine()
    
    # Добавим два лимитных sell-ордера
    engine.add_order(make_order(OrderSide.ASK, price=1.1000, volume=2.0, agent_id="seller1"))
    engine.add_order(make_order(OrderSide.ASK, price=1.1010, volume=2.0, agent_id="seller2"))

    # Добавим маркетный buy-ордер на 3.0 — он должен заполниться на 2.0 по 1.1000 и 1.0 по 1.1010
    trades = engine.add_order(make_order(OrderSide.BID, volume=3.0, agent_id="buyer1"))

    assert len(trades) == 2
    assert trades[0]['price'] == 1.1000
    assert trades[0]['volume'] == 2.0
    assert trades[1]['price'] == 1.1010
    assert trades[1]['volume'] == 1.0

    # Проверяем, что объёмы ордербука обновились
    best_ask = engine.order_book.get_best_ask()
    assert best_ask == 1.1010
    remaining_ask_volume = sum(level.volume for level in engine.order_book.asks.levels)
    assert remaining_ask_volume == 1.0

def test_market_sell_fills_against_existing_bids():
    engine = MatchingEngine()
    
    engine.add_order(make_order(OrderSide.BID, price=1.0990, volume=1.0, agent_id="buyer1"))
    engine.add_order(make_order(OrderSide.BID, price=1.0980, volume=2.0, agent_id="buyer2"))

    trades = engine.add_order(make_order(OrderSide.ASK, volume=2.5, agent_id="seller1"))

    assert len(trades) == 2
    assert trades[0]['price'] == 1.0990
    assert trades[0]['volume'] == 1.0
    assert trades[1]['price'] == 1.0980
    assert trades[1]['volume'] == 1.5

    best_bid = engine.order_book.get_best_bid()
    assert best_bid == 1.0980
    remaining_bid_volume = sum(level.volume for level in engine.order_book.bids.levels)
    assert remaining_bid_volume == 0.5

def test_market_order_partial_fill():
    engine = MatchingEngine()

    engine.add_order(make_order(OrderSide.BID, price=1.1000, volume=1.0, agent_id="buyer1"))
    trades = engine.add_order(make_order(OrderSide.ASK, volume=5.0, agent_id="seller1"))

    assert len(trades) == 1
    assert trades[0]['volume'] == 1.0
    assert trades[0]['price'] == 1.1000

    # Остаток должен быть "поглощён", т.е. маркет ордер на оставшиеся 4.0 не исполнен
    remaining_bids = sum(level.volume for level in engine.order_book.bids.levels)
    assert remaining_bids == 0.0

def test_market_order_no_liquidity():
    engine = MatchingEngine()

    trades = engine.add_order(make_order(OrderSide.BID, volume=1.0, agent_id="buyer1"))
    assert trades == []  # Нечем исполнять
    assert len(engine.order_book.asks.levels) == 0
    assert len(engine.order_book.bids.levels) == 0
