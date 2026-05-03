import os
import time
import uuid
import logging, builtins
import numpy as np
import random
import threading
import collections
from enum import Enum

import signal
import sys

import memory_logger

from collections import deque

from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit
from flask import Flask, send_from_directory, request, jsonify

# --- market telemetry ---
import csv, os, time


class MarketLogger:
    def __init__(self, path="sim_log.csv", flush_every=1000):
        self.path = path
        self.flush_every = flush_every
        self._f = open(self.path, "a", newline="", buffering=1)
        self._w = csv.writer(self._f)
        if os.stat(self.path).st_size == 0:
            self._w.writerow([
                "ts", "event", "bid", "ask", "mid", "spread",
                "bid_sz1", "ask_sz1", "last_trade",
                "trade_price", "trade_qty", "trade_side", "taker_id", "maker_id"
            ])
        self._book_last = None
        self._n = 0

    def on_book(self, ob):
        ts = time.time()
        bid = ob._best_bid_price()
        ask = ob._best_ask_price()
        if bid is None or ask is None:
            return
        mid = (bid + ask) / 2.0
        spread = ask - bid
        # лучшая глубина
        bq = ob._best_bid_size() if hasattr(ob, "_best_bid_size") else None
        aq = ob._best_ask_size() if hasattr(ob, "_best_ask_size") else None

        # пишем только если топ-оф-бук реально изменился
        cur = (bid, ask, bq, aq)
        if cur != self._book_last:
            self._w.writerow([ts, "book", bid, ask, mid, spread, bq, aq,
                              getattr(ob, "last_trade_price", None),
                              "", "", "", "", ""])
            self._book_last = cur
            self._n += 1
            if self._n % self.flush_every == 0:
                self._f.flush()

    def on_trades(self, trades):
        if not trades:
            return
        ts = time.time()
        for t in trades:
            price = float(t["price"])
            qty = float(t["volume"])
            side = str(t.get("taker_side", "")).lower()
            taker = t.get("taker", "")
            maker = t.get("maker", "")
            self._w.writerow([ts, "trade", "", "", "", "", "", "",
                              getattr(order_book, "last_trade_price", None),
                              price, qty, side, taker, maker])
        self._n += len(trades)
        if self._n % self.flush_every == 0:
            self._f.flush()


# ── тута свечки ──────────────────────────────────────────
from candle_db import init_db, insert_candle, load_candles, fill_incomplete_candle
from models import Candle
# ── тута ликвидность ─────────────────────────────────────
from liquidity_db import init_db as init_liquidity_db, load_liquidity_state, save_liquidity_state
# ───тута кластера ────────────────────────────────
from trade_db import init_db as init_trdb, insert_batch as tr_insert_batch, load_trades as tr_load_trades

# ─────────────────────────────────────────────────────────

conn = init_db()
liquidity_conn = init_liquidity_db()

TRDB = init_trdb()
TR_BUF = collections.deque(maxlen=500_000)

USER_TRADES = deque(maxlen=10000)

# --- runtime control / frontend queues ---
SHUTTING_DOWN = False
_shutdown_started = False

PENDING_TRADES = collections.deque(maxlen=100_000)
_ACCOUNT_DIRTY = True
_ACCOUNT_STATS_DIRTY = True
_CONDITIONAL_DIRTY = True

# ── silence per-trade spam ─────────────
# orig_print = builtins.print
# builtins.print = lambda *a, **k: None
# logging.disable(logging.CRITICAL)
# ─────────────────────────────────

# ─────────────────────────────────
from trade_logger import _flush as flush_trades
# ─────────────────────────────────


from order import Order, OrderSide, OrderType
from order_book import OrderBook
from market_context import MarketContextAdvanced
from market_maker import AdvancedMarketMaker
from market_maker2 import AdvancedMarketMaker2
from retail_trader import RetailTrader
# from return_liquidity_agent import ReturnLiquidityAgent
# from strategist_agent import InstitutionalExecutor
from is_pressure_executor import ISPressureExecutor
# from macro_campaign_executor import MacroCampaignExecutor
from liquidity_seeker import LiquiditySeeker
# from anti_trend_hedger import AntiTrendHedger
from latency_arb import LatencyArb
from passive_depth_provider import PassiveDepthProvider
# from macro_reversal_fund import MacroReversalFund
# from flow_campaign_executor import FlowCampaignExecutor
from risk_parity_vol_control_fund import RiskParityVolControlFund
from band_wall_provider import AdaptiveBandWallProvider
from dealer_risk_transfer import DealerRiskTransfer
from customer_aggregator_flow import CustomerAggregatorFlow
from tier1_jpm import Tier1JPMBank
# from corporate_flow_twap import CorporateFlowTWAP
# from trend_impuls import ReversalAgent
from tier1_ubs import Tier1UBSBank
from corporate_flow_manager import CorporateFlowManager
from breakout_agent import BreakoutTrader
from reversion_brain import ReversionBrain
# from fund_disruptor import RegimeShockDisruptor
# from trend_agent import TrendAgent
from multi_hour_metaorder_agent import MultiHourMetaOrderAgent
from agent_variants import build_variant_agents
from agent_variants2 import build_variant_agents2

# --- Логирование ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger("OrderBookServer")

# --- Flask + SocketIO ---
BASE_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(BASE_DIR)
FRONTEND_DIR = os.path.join(PROJECT_ROOT, 'frontend')

app = Flask(__name__, static_folder=FRONTEND_DIR, template_folder=FRONTEND_DIR)
socketio = SocketIO(app, cors_allowed_origins="*")


# ---- trades -> SQLite flush task ----
def trade_flush_task():
    while not SHUTTING_DOWN:
        socketio.sleep(0.05)
        if not TR_BUF:
            continue
        batch = []
        while TR_BUF and len(batch) < 5000:
            batch.append(TR_BUF.popleft())
        # trade_db.insert_batch
        tr_insert_batch(TRDB, batch)


# --- Книга и стартовые лимитные ордера ---
order_book = OrderBook()
order_book.market_context = MarketContextAdvanced()

LOGGER = MarketLogger(path="sim_log.csv", flush_every=500)


# --- Начальная ликвидность: 6 уровней по 20 ---
def inject_initial_liquidity():
    base = 100.0
    for i in range(1, 7):
        order_book.add_order(Order(
            order_id=str(uuid.uuid4()), agent_id=f"seed_buy_{i}",
            side=OrderSide.BID,
            volume=20.0,
            price=round(base - i * 0.5, 2),
            order_type=OrderType.LIMIT, ttl=None
        ))
        order_book.add_order(Order(
            order_id=str(uuid.uuid4()), agent_id=f"seed_sell_{i}",
            side=OrderSide.ASK,
            volume=20.0,
            price=round(base + i * 0.5, 2),
            order_type=OrderType.LIMIT, ttl=None
        ))
    seed_order_buy = Order(
        order_id=str(uuid.uuid4()),
        agent_id="seed_buy",
        side=OrderSide.BID,
        volume=10.0,
        price=99.50,
        order_type=OrderType.LIMIT,
        ttl=None
    )
    seed_order_sell = Order(
        order_id=str(uuid.uuid4()),
        agent_id="seed_sell",
        side=OrderSide.ASK,
        volume=10.0,
        price=100.50,
        order_type=OrderType.LIMIT,
        ttl=None
    )
    order_book.add_order(seed_order_buy)
    order_book.add_order(seed_order_sell)


import json
from collections import deque as _deque

_AGENT_STATE_FILE = "agent_states.json"

# Атрибуты, которые мы умеем сохранять/восстанавливать обобщённо.
# Агент может переопределить поведение через get_state()/set_state().
_STATE_ATTRS = [
    # позиция / инвентарь
    'inventory', 'position', 'net_position', 'inventory_skew',
    'long_inventory', 'short_inventory',
    'cum_buy_qty', 'cum_sell_qty', 'cum_buy', 'cum_sell',
    'total_filled', 'fill_count',
    # капитал
    'capital', 'cash', 'available_capital', 'used_capital',
    # ценовые ориентиры
    'last_price', 'reference_price', 'anchor_price', 'vwap',
    # EMA / индикаторы
    'ema_fast', 'ema_slow', 'ema_short', 'ema_long', 'last_ema',
    # тренд/режим
    'trend_direction', 'trend_strength', 'bias', 'mode',
    'regime', 'phase',
    # счётчики активности
    'order_count', 'active', 'campaign_active',
    'target_qty', 'remaining_qty', 'elapsed',
]


def _to_json(obj):
    """Рекурсивно превращает любые вложенные структуры в JSON-совместимые."""
    # Enum -> его значение (строка)
    if isinstance(obj, Enum):
        return obj.value
    # deque / list
    if isinstance(obj, (_deque, list)):
        return [_to_json(item) for item in obj]
    # dict — обрабатываем и ключи, и значения
    if isinstance(obj, dict):
        return {str(k): _to_json(v) for k, v in obj.items()}
    # float-like (включая numpy.float64)
    if hasattr(obj, '__float__'):
        return float(obj)
    # int-like, но не bool
    if hasattr(obj, '__int__') and not isinstance(obj, bool):
        return int(obj)
    # None, bool, str, обычные числа — как есть
    return obj


def _save_agent_states(agents):
    states = {}
    for agent in agents:
        aid = agent.agent_id
        # Если агент даёт get_state()
        if callable(getattr(agent, 'get_state', None)):
            try:
                raw_state = agent.get_state()
                states[aid] = _to_json(raw_state)  # <-- глубокая очистка
                continue
            except Exception as e:
                logger.warning(f"[SaveState] {aid}.get_state() failed: {e}")

        # Иначе собираем автоатрибуты
        s = {}
        for attr in _STATE_ATTRS:
            val = getattr(agent, attr, None)
            if val is not None:
                try:
                    s[attr] = _to_json(val)  # <-- глубокая очистка
                except Exception:
                    pass

        # histories и ema тоже
        for hist_attr in ('price_history', 'prev_mid_prices', 'recent_volatility',
                          'mid_prices', 'trade_prices'):
            val = getattr(agent, hist_attr, None)
            if val is not None:
                try:
                    lst = list(val)[-500:]
                    s[hist_attr] = _to_json(lst)
                except Exception:
                    pass

        ema_dict = getattr(agent, 'ema_values', None)
        if isinstance(ema_dict, dict):
            try:
                s['ema_values'] = _to_json(ema_dict)
            except Exception:
                pass

        states[aid] = s

    # Финальная страховка — сериализуем ещё раз, чтобы исключить ошибки верхнего уровня
    try:
        json_str = json.dumps(states, allow_nan=False)  # позволит проверить
        with open(_AGENT_STATE_FILE, 'w') as f:
            f.write(json_str)
        logger.info(f"[SaveState] Saved states for {len(states)} agents → {_AGENT_STATE_FILE}")
    except Exception as e:
        logger.error(f"[SaveState] JSON still failed: {e}. Attempting fallback...")

        # Крайний случай: если всё равно не сериализуется — пробуем force
        def force_str(obj):
            if isinstance(obj, (dict, list)):
                return _to_json(obj)
            return str(obj)

        try:
            with open(_AGENT_STATE_FILE, 'w') as f:
                json.dump(states, f, default=str)  # отдаём всё как строку
            logger.warning("[SaveState] States saved with default=str fallback")
        except Exception as e2:
            logger.error(f"[SaveState] Complete failure to save: {e2}")


def _restore_agent_states(agents):
    """Восстанавливаем состояние агентов из JSON-файла если есть."""
    if not os.path.exists(_AGENT_STATE_FILE):
        logger.info("[RestoreState] No saved agent states found — cold start")
        return

    try:
        with open(_AGENT_STATE_FILE) as f:
            states = json.load(f)
    except Exception as e:
        logger.error(f"[RestoreState] Failed to read {_AGENT_STATE_FILE}: {e}")
        return

    restored = 0
    for agent in agents:
        aid = agent.agent_id
        s = states.get(aid)
        if not s:
            continue

        # Агент может реализовать set_state() сам
        if callable(getattr(agent, 'set_state', None)):
            try:
                agent.set_state(s)
                restored += 1
                continue
            except Exception as e:
                logger.warning(f"[RestoreState] {aid}.set_state() failed: {e}")

        # Иначе — обобщённое восстановление
        for attr in _STATE_ATTRS:
            if attr in s:
                try:
                    setattr(agent, attr, s[attr])
                except Exception:
                    pass

        # Восстанавливаем histories как deque нужного maxlen
        for hist_attr in ('price_history', 'prev_mid_prices', 'recent_volatility',
                          'mid_prices', 'trade_prices'):
            if hist_attr not in s:
                continue
            existing = getattr(agent, hist_attr, None)
            if existing is None:
                continue
            maxlen = existing.maxlen if isinstance(existing, _deque) else None
            try:
                new_deq = _deque(s[hist_attr], maxlen=maxlen)
                setattr(agent, hist_attr, new_deq)
            except Exception:
                pass

        # EMA-словарь
        if 'ema_values' in s and isinstance(getattr(agent, 'ema_values', None), dict):
            try:
                agent.ema_values.update(s['ema_values'])
            except Exception:
                pass

        # update_emas если есть метод
        if callable(getattr(agent, 'update_emas', None)):
            hist = getattr(agent, 'price_history', None) or getattr(agent, 'prev_mid_prices', None)
            if hist:
                try:
                    for price in list(hist)[-20:]:
                        agent.update_emas(price)
                except Exception:
                    pass

        restored += 1

    logger.info(f"[RestoreState] Restored states for {restored}/{len(agents)} agents")


def graceful_shutdown(*args):
    global SHUTTING_DOWN, _shutdown_started

    if _shutdown_started:
        logger.warning("Forced shutdown requested.")
        os._exit(1)

    _shutdown_started = True
    SHUTTING_DOWN = True

    try:
        logger.info("Saving order book state before shutdown...")
        snapshot = order_book.get_order_book_snapshot(depth=1000)
        save_liquidity_state(liquidity_conn, {
            'bids': {entry['price']: entry['volume'] for entry in snapshot['bids']},
            'asks': {entry['price']: entry['volume'] for entry in snapshot['asks']},
            'last_trade_price': order_book.last_trade_price,
        })

        if 'AGENTS' in globals():
            _save_agent_states(AGENTS)

        for db_conn in (conn, liquidity_conn, TRDB):
            try:
                db_conn.commit()
            except Exception:
                pass

        try:
            LOGGER._f.flush()
        except Exception:
            pass

        logger.info("Shutdown complete.")

    except Exception as e:
        logger.error(f"Shutdown error: {e}")

    finally:
        os._exit(0)


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

# Загружаем ликвидность
saved_liquidity = load_liquidity_state(liquidity_conn)

# Восстанавливаем last_trade_price если есть сохранённое
_saved_last_price = saved_liquidity.get('last_trade_price')
if _saved_last_price is not None:
    try:
        order_book.last_trade_price = float(_saved_last_price)
        logger.info(f"[Restore] last_trade_price = {order_book.last_trade_price}")
    except (TypeError, ValueError):
        pass

market_phases_history: deque = deque(maxlen=500)
_last_phase_name = None
_last_phase_start = None
_last_microphase = None


# --- Инициализация начальных сделок ---
def inject_initial_trades():
    logger.info("[Init] Injecting initial market orders to trigger first trades")
    market_orders = [
        Order(str(uuid.uuid4()), "seeder_m1", OrderSide.BID, 2.0, None, OrderType.MARKET, None),
        Order(str(uuid.uuid4()), "seeder_m2", OrderSide.ASK, 1.0, None, OrderType.MARKET, None),
        Order(str(uuid.uuid4()), "seeder_m3", OrderSide.BID, 1.0, None, OrderType.MARKET, None),
        Order(str(uuid.uuid4()), "seeder_m4", OrderSide.ASK, 2.0, None, OrderType.MARKET, None),
    ]
    for mo in market_orders:
        order_book.add_order(mo)


#  Список пидоров
TAKER_CAP_MULT = 2.0

retail = RetailTrader(agent_id="retail1", capital=40_000_000.0 * TAKER_CAP_MULT, num_subagents=10)
is_press1 = ISPressureExecutor(agent_id="is_press1", capital=175_000_000.0 * TAKER_CAP_MULT)  # CALIB: 250M→150M, reduces excess drift
is_press2 = ISPressureExecutor(agent_id="is_press2", capital=175_000_000.0 * TAKER_CAP_MULT)  # CALIB: 250M→150M
ls = LiquiditySeeker(agent_id="ls", capital=300_000_000.0)  # CALIB: 120M→300M
arb = LatencyArb(agent_id="lat_arb", capital=8_000_000.0)
depth_provider = PassiveDepthProvider(agent_id="depth1", capital=300_000_000.0)
depth_provider2 = PassiveDepthProvider(agent_id="depth2", capital=300_000_000.0)
band_wall = AdaptiveBandWallProvider(agent_id="band_wall1", capital=250_000_000.0)
drt1 = DealerRiskTransfer(agent_id="drt1", capital=80_000_000.0 * TAKER_CAP_MULT)
drt2 = DealerRiskTransfer(agent_id="drt2", capital=80_000_000.0 * TAKER_CAP_MULT)
caf = CustomerAggregatorFlow(agent_id="caf", capital=50_000_000.0 * TAKER_CAP_MULT)
jpm_bank = Tier1JPMBank(agent_id="tier1_jpm", capital=180_000_000.0 * TAKER_CAP_MULT)
jpm_bank2 = Tier1JPMBank(agent_id="tier1_jpm2", capital=150_000_000.0 * TAKER_CAP_MULT)
mm_total = 500_000_000.0
mm_total2 = 400_000_000.0
mm_total3 = 400_000_000.0
mm_total4 = 400_000_000.0
tier1_ubs = Tier1UBSBank(agent_id="tier1_ubs", capital=100_000_000.0 * TAKER_CAP_MULT)
corp_manager = CorporateFlowManager(agent_id="corp_mgr", capital_pool=100_000_000.0 * TAKER_CAP_MULT)
corp_manager2 = CorporateFlowManager(agent_id="corp_mgr2", capital_pool=100_000_000.0)
# corp_manager3 = CorporateFlowManager(agent_id="corp_mgr3", capital_pool=100_000_000.0)
meta_1 = MultiHourMetaOrderAgent(agent_id="meta_1", capital=1_500_000 * TAKER_CAP_MULT, profile="pov",      seed=None)   # seed=None → новый случайный при каждом запуске
meta_2 = MultiHourMetaOrderAgent(agent_id="meta_2", capital=1_500_000 * TAKER_CAP_MULT, profile="twap",     seed=None)
meta_3 = MultiHourMetaOrderAgent(agent_id="meta_3", capital=800_000 * TAKER_CAP_MULT,   profile="deadline", seed=None)
reversion = ReversionBrain(agent_id="reversion_brain", capital=45_000_000.0 * TAKER_CAP_MULT)
# risk_vol = RiskParityVolControlFund(agent_id="pulse_inst_1", capital=60_000_000.0)
#variant_agents = build_variant_agents(base_capital=30_000_000)
#variant_agents2 = build_variant_agents2(base_capital=100_000_000)

mm_agents = [
    AdvancedMarketMaker(agent_id="mm1", capital=mm_total * 0.45),
    AdvancedMarketMaker(agent_id="mm2", capital=mm_total * 0.35),
    AdvancedMarketMaker(agent_id="mm3", capital=mm_total * 0.20),
]

mm_agents2 = [
    AdvancedMarketMaker2(agent_id="mm4", capital=mm_total2 * 0.45),
    AdvancedMarketMaker2(agent_id="mm5", capital=mm_total2 * 0.35),
    AdvancedMarketMaker2(agent_id="mm6", capital=mm_total2 * 0.20),
]

mm_agents3 = [
    AdvancedMarketMaker2(agent_id="mm7", capital=mm_total2 * 0.45),
    AdvancedMarketMaker2(agent_id="mm8", capital=mm_total2 * 0.35),
    AdvancedMarketMaker2(agent_id="mm9", capital=mm_total2 * 0.20),
]

mm_agents4 = [
    AdvancedMarketMaker2(agent_id="mm10", capital=mm_total2 * 0.45),
    AdvancedMarketMaker2(agent_id="mm11", capital=mm_total2 * 0.35),
    AdvancedMarketMaker2(agent_id="mm12", capital=mm_total2 * 0.20),
]

AGENTS = [
    *mm_agents,
    *mm_agents2,
    *mm_agents3,
    *mm_agents4,
    retail,
    is_press1,
    is_press2,
    ls,
    arb,
    depth_provider,
    depth_provider2,
    band_wall,
    drt1,
    drt2,
    caf,
    jpm_bank,
    jpm_bank2,
    tier1_ubs,
    corp_manager,
    corp_manager2,
    # corp_manager3,
    meta_1,
    meta_2,
    meta_3,
    reversion,
    # risk_vol,
    #*variant_agents,
    #*variant_agents2,
]

# Если ликвидности нет — значит первый запуск, инжектим
BOOTSTRAP_UNTIL = None

if saved_liquidity['bids'] or saved_liquidity['asks']:
    # НЕ добавляем ордера напрямую в order_book.
    # Просим провайдера глубины переиграть их под своим agent_id.
    depth_provider.begin_bootstrap(saved_liquidity, seconds=15.0)
    BOOTSTRAP_UNTIL = time.time() + 15.0

    # Прогреваем market_context историческими ценами чтобы не стартовал в "undefined" фазе
    if order_book.last_trade_price:
        ref = order_book.last_trade_price
        if hasattr(order_book.market_context, 'update'):
            now_ts = time.time()
            for i in range(20, 0, -1):  # 20 синтетических тиков в прошлое
                order_book.market_context.update(
                    tick=now_ts - i * 0.3,
                    mid_price=ref,
                    snapshot={'bids': [], 'asks': []},
                    sweep=False,
                )
else:
    inject_initial_liquidity()


def warmup_agents(conn, agents, n=50):
    """Прогрев агентов историческими данными из БД (последние n минут 1m-свечей)."""
    now = int(time.time())
    # Ключ таймфрейма в БД — целое число секунд (60), не строка '1m'
    candles = load_candles(conn, 60, now - n * 60, now)
    if not candles:
        logger.warning("[Warmup] No 1m candles found — agents start cold")
        return
    mids = [(c.high + c.low) / 2 for c in candles]
    logger.info(f"[Warmup] Loaded {len(candles)} candles, price range "
                f"[{min(mids):.4f}, {max(mids):.4f}]")

    for mid in mids:
        for agent in agents:
            # простые price_history
            if hasattr(agent, "price_history"):
                agent.price_history.append(mid)
            # EMA у банковского
            if hasattr(agent, "update_emas"):
                agent.update_emas(mid)
            if hasattr(agent, "prev_mid_prices"):
                agent.prev_mid_prices.append(mid)
            # волатильность/ATR
            if hasattr(agent, "recent_volatility"):
                agent.recent_volatility.append(abs(mid - mids[-1]))  # примитивный шаг
    # для маркетмейкера
    for agent in agents:
        if hasattr(agent, "update_market_structure"):
            try:
                agent.update_market_structure(conn)
            except Exception as e:
                print(f"MarketMaker warmup error: {e}")


# Вызов сразу после AGENTS = [...]
warmup_agents(conn, AGENTS, n=50)

# Восстанавливаем сохранённые состояния агентов (перезаписывает warmup там где нужно)
_restore_agent_states(AGENTS)

# ==== USER ACCOUNT ====
USER_ID = "terminal-ui"


class Account:
    MMR = 0.005  # maintenance margin rate 0.5%

    def __init__(self, balance=100_000.0, leverage=20, mode="cross"):
        self.balance = float(balance)
        self.leverage = int(leverage)
        self.mode = mode  # "cross" | "isolated"
        self.position_qty = 0.0  # +long, -short (в контрактах)
        self.entry_price = None
        self.realized_pnl = 0.0
        self.last_fee_rate = 0.0
        self.fee_paid = 0.
        self.fee_schedule = {"maker": 0.0002, "taker": 0.0004}

    # mark = mid(bid,ask) если есть, иначе last_trade
    def mark_price(self):
        bid = order_book._best_bid_price()
        ask = order_book._best_ask_price()
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return order_book.last_trade_price or 100.0

    def notional(self, px=None):
        px = px if px is not None else self.mark_price()
        return abs(self.position_qty) * float(px)

    def upnl(self, px=None):
        if self.entry_price is None or self.position_qty == 0:
            return 0.0
        px = px if px is not None else self.mark_price()
        if self.position_qty > 0:  # long
            return (px - self.entry_price) * self.position_qty
        else:  # short
            return (self.entry_price - px) * abs(self.position_qty)

    def equity(self, px=None):
        return self.balance + self.realized_pnl + self.upnl(px)

    def used_margin(self, px=None):
        # используем IM = N/L только по открытой позиции
        return self.notional(px) / max(self.leverage, 1)

    def available_cross(self, px=None):
        px = px if px is not None else self.mark_price()
        if self.mode == "isolated":
            free_bal = self.balance + self.realized_pnl
            return free_bal - self.used_margin(px)
        return self.equity(px) - self.used_margin(px)

    def liquidation_price(self):
        if self.position_qty == 0 or self.entry_price is None:
            return None
        q = abs(self.position_qty)
        px = self.mark_price()
        im = self.notional(self.entry_price) / max(self.leverage, 1)
        mmr = self.MMR

        # изолированная: margin = IM
        # кросс: margin = IM + (equity - used_margin)  (добавляется свободка)
        if self.mode == "isolated":
            margin = im
        else:
            margin = im + max(self.available_cross(px), 0.0)

        if self.position_qty > 0:  # long
            # margin/qty + Pliq - entry = Pliq*mmr  =>  Pliq = (entry - margin/qty)/(1-mmr)
            return (self.entry_price - margin / q) / max(1.0 - mmr, 1e-9)
        else:  # short
            # margin/qty + entry - Pliq = Pliq*mmr  =>  Pliq = (entry + margin/qty)/(1+mmr)
            return (self.entry_price + margin / q) / (1.0 + mmr)

    def can_place(self, side, qty, px, order_type):
        qty = float(qty);
        px = float(px)

        ref_px = px if order_type == "limit" else self.mark_price()
        required_im = (qty * ref_px) / max(self.leverage, 1)
        max_notional = self.leverage * max(self.balance + self.realized_pnl, 0.0)

        if qty * ref_px > max_notional:
            return False, f"Превышен максимум: {qty * ref_px:.2f} > {max_notional:.2f}"

        if qty <= 0:
            return False, "Неверный объём"
        ref_px = px if order_type == "limit" else self.mark_price()
        required_im = (qty * ref_px) / max(self.leverage, 1)

        if self.mode == "isolated":
            free_bal = self.balance + self.realized_pnl
            if required_im > free_bal:
                return False, f"Недостаточно баланса: нужно {required_im:.2f}, есть {free_bal:.2f}"
        else:
            if required_im > self.equity(ref_px):
                return False, f"Недостаточно equity: нужно {required_im:.2f}, есть {self.equity(ref_px):.2f}"
        return True, ""

    def apply_fill(self, side, price, qty, is_taker=True):
        price = float(price)
        qty = float(qty)
        signed = qty if side == OrderSide.BID else -qty

        # комиссия по роли
        fee_rate = self.fee_schedule["taker"] if is_taker else self.fee_schedule["maker"]
        fee = price * qty * fee_rate
        self.last_fee_rate = fee_rate
        self.balance -= fee
        self.fee_paid += fee

        # добавление/закрытие позиции
        if self.position_qty == 0 or (self.position_qty > 0 and signed > 0) or (self.position_qty < 0 and signed < 0):
            new_qty = self.position_qty + signed
            if self.entry_price is None:
                self.entry_price = price
            else:
                wgt = abs(self.position_qty)
                self.entry_price = (self.entry_price * wgt + price * abs(signed)) / (wgt + abs(signed))
            self.position_qty = new_qty
        else:
            close_qty = min(abs(self.position_qty), abs(signed))
            pnl = (price - self.entry_price) * close_qty if self.position_qty > 0 else (
                                                                                               self.entry_price - price) * close_qty
            self.realized_pnl += pnl
            self.position_qty += signed
            if self.position_qty == 0:
                self.entry_price = None

    def snapshot(self):
        px = self.mark_price()
        return {
            "mode": self.mode,
            "leverage": self.leverage,
            "balance": round(self.balance, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "upnl": round(self.upnl(px), 2),
            "equity": round(self.equity(px), 2),
            "used_margin": round(self.used_margin(px), 2),
            "available": round(self.available_cross(px), 2),
            "position_qty": self.position_qty,
            "entry_price": self.entry_price,
            "mark_price": round(px, 2),
            "liq_price": self.liquidation_price(),
            "max_position_notional": round(max(self.leverage, 1) * max(self.balance + self.realized_pnl, 0.0), 2),
            "fee_rate": self.last_fee_rate,
            "fee_paid": round(self.fee_paid, 2),
            "last_fee_rate": self.last_fee_rate,
            "margin_mode": self.mode,
            "liq_warning": (self.liquidation_price(), self.mark_price()),
            "notional": round(self.notional(px), 2),
        }


account = Account()

# ==== TP/SL conditional triggers (OCO) ====
_conditional = []


def _place_conditional(owner, side_close, tp=None, sl=None, trailing=None, qty=0.0, trigger_by="mark"):
    """
    Создаёт TP/SL/Trailing условные ордера.
    side_close: "sell" для закрытия long, "buy" для закрытия short
    tp/sl — абсолютные цены, trailing — offset в тех же единицах
    qty — объём для закрытия
    """
    import uuid
    oco_id = uuid.uuid4().hex

    if tp is not None:
        _conditional.append({
            "owner": owner,
            "side": side_close,
            "type": "tp",
            "trigger": float(tp),
            "trail_offset": None,
            "qty": float(qty),
            "oco": oco_id,
            "reduce_only": True,
            "trigger_by": trigger_by
        })
    if sl is not None:
        _conditional.append({
            "owner": owner,
            "side": side_close,
            "type": "sl",
            "trigger": float(sl),
            "trail_offset": None,
            "qty": float(qty),
            "oco": oco_id,
            "reduce_only": True,
            "trigger_by": trigger_by
        })
    if trailing is not None:
        # trailing стоп: стартовый trigger зависит от текущей цены
        mark = account.mark_price()
        if side_close == "sell":  # закрытие long
            trigger = mark - trailing
        else:  # закрытие short
            trigger = mark + trailing
        _conditional.append({
            "owner": owner,
            "side": side_close,
            "type": "trailing",
            "trigger": trigger,
            "trail_offset": float(trailing),
            "qty": float(qty),
            "oco": oco_id,
            "reduce_only": True,
            "trigger_by": trigger_by
        })

    socketio.emit("conditional_update", _conditional)


def _eval_and_fire_conditionals():
    """
    Проверяет условия TP/SL/Trailing и исполняет при срабатывании.
    """
    if not _conditional:
        return

    last = order_book.last_trade_price or account.mark_price()
    mark = account.mark_price()
    fired_indices = []

    for i, c in enumerate(list(_conditional)):  # копия для безопасного удаления
        ref = mark if c["trigger_by"] == "mark" else last

        # trailing stop: подтягиваем trigger
        if c["type"] == "trailing" and c["trail_offset"] is not None:
            if c["side"] == "sell":  # закрытие long
                new_trigger = max(c["trigger"], mark - c["trail_offset"])
            else:  # закрытие short
                new_trigger = min(c["trigger"], mark + c["trail_offset"])
            c["trigger"] = new_trigger

        # reduce-only: проверяем что есть позиция
        if account.position_qty == 0:
            continue

        # Логика направления
        cond = False
        if c["side"] == "sell":  # закрытие long
            if c["type"] in ("tp", "trailing"):
                cond = ref >= c["trigger"]
            elif c["type"] == "sl":
                cond = ref <= c["trigger"]
        else:  # закрытие short (side=buy)
            if c["type"] in ("tp", "trailing"):
                cond = ref <= c["trigger"]
            elif c["type"] == "sl":
                cond = ref >= c["trigger"]

        if cond:
            qty = min(abs(account.position_qty), c["qty"])
            if qty <= 0:
                continue

            side = OrderSide.ASK if c["side"] == "sell" else OrderSide.BID
            px = order_book._best_bid_price() if side == OrderSide.ASK else order_book._best_ask_price()
            if px is None:
                logger.warning(f"[TP/SL] Trigger fired but no liquidity, skipping (side={side}, qty={qty})")
                continue

            # Прямое закрытие позиции
            account.apply_fill(side, px, qty, is_taker=True)

            # Событие трейда для фронта
            trade = {
                "price": px,
                "volume": qty,
                "side": "sell" if side == OrderSide.ASK else "buy",
                "initiator_side": "buy" if (side == OrderSide.BID) else "sell",
                "taker": USER_ID,
                "maker": "system"
            }
            socketio.emit("trade", trade)
            socketio.emit("account_update", account.snapshot())
            socketio.emit("positions_update", account.snapshot())

            logger.info(
                f"[TP/SL] Trigger fired: type={c['type']}, side={c['side']}, trigger={c['trigger']}, ref={ref}, qty={qty}, px={px}"
            )
            fired_indices.append(i)

            # Если OCO — убираем все ордера этой группы
            oco_id = c["oco"]
            _conditional[:] = [x for x in _conditional if x["oco"] != oco_id]

    # убрать сработавшие (OCO уже удалил группу)
    for idx in sorted(fired_indices, reverse=True):
        if idx < len(_conditional):
            _conditional.pop(idx)

    socketio.emit("conditional_update", _conditional)
    socketio.emit("positions_update", account.snapshot())


def push_positions():
    socketio.emit("positions_update", account.snapshot())


def schedule_push():
    while not SHUTTING_DOWN:
        socketio.sleep(3)
        socketio.emit("positions_update", account.snapshot())


# --- OHLC (свечи) ---

class PhaseTracker:
    def __init__(self, maxlen=300):
        self.current_phase = None
        self.start_time = None
        self.tracked_phases = deque(maxlen=maxlen)

    def update(self, market_context):
        now = int(time.time())
        phase = market_context.phase.name if market_context and market_context.phase else "undefined"
        micro = market_context.phase.reason.get("microphase",
                                                "") if market_context and market_context.phase and market_context.phase.reason else ""

        if self.current_phase != (phase, micro):
            if self.current_phase:
                self.tracked_phases.append({
                    "start": self.start_time,
                    "end": now,
                    "phase": self.current_phase[0],
                    "microphase": self.current_phase[1]
                })
            self.current_phase = (phase, micro)
            self.start_time = now

    def export(self):
        now = int(time.time())
        exported = list(self.tracked_phases)
        if self.current_phase:
            exported.append({
                "start": self.start_time,
                "end": now,
                "phase": self.current_phase[0],
                "microphase": self.current_phase[1]
            })
        return exported


phase_tracker = PhaseTracker()

USER_ORDERS = {}  # order_id -> {"is_taker": bool}


# --- HTTP ---
@app.route('/')
def index():
    return send_from_directory(FRONTEND_DIR, 'index.html')


@app.route('/styles.css')
def css():
    return send_from_directory(FRONTEND_DIR, 'styles.css')


@app.route('/scripts.js')
def js():
    return send_from_directory(FRONTEND_DIR, 'scripts.js')


@app.route('/chart/<path:filename>')
def serve_chart(filename):
    return send_from_directory(os.path.join(FRONTEND_DIR, 'chart'), filename)


@app.route('/sounds/<path:filename>')
def serve_sounds(filename):
    return send_from_directory(os.path.join(FRONTEND_DIR, 'static', 'sounds'), filename)


@app.route('/images/<path:filename>')
def serve_images(filename):
    return send_from_directory(os.path.join(FRONTEND_DIR, 'static', 'images'), filename)


@app.route("/profile")
def profile_page():
    return send_from_directory(FRONTEND_DIR, 'profile.html')


@socketio.on('get_user_trades')
def handle_get_user_trades():
    emit("user_trades", list(USER_TRADES)[-100:])


@socketio.on('get_account')
def handle_get_account():
    emit("account_update", account.snapshot())


# --- Socket.IO ---
@socketio.on('connect')
def on_connect():
    sid = request.sid
    logger.info(f"Client connected: {sid}")
    emit("orderbook_update", order_book.get_order_book_snapshot(depth=200))
    hist = []
    for t in list(order_book.trade_history)[-200:]:
        side_str = str(t.get("taker_side", "")).lower()
        t = dict(t)
        t["initiator_side"] = "buy" if side_str in ("buy", "bid") else "sell"
        hist.append(t)
    emit("history", hist)
    emit("account_update", account.snapshot())
    emit("conditional_update", _conditional)


@socketio.on('set_conditionals')
def handle_set_conditionals(data):
    try:
        tp = data.get('tp')
        sl = data.get('sl')
        trig_by = data.get('trigger_by', 'mark')

        # нет позиции — просто отдать текущее
        if abs(account.position_qty) <= 0:
            emit("conditional_update", _conditional)
            return

        # противоположная сторона для закрытия
        side_close = 'sell' if account.position_qty > 0 else 'buy'
        qty = abs(account.position_qty)

        # убрать старые reduce-only условки этого пользователя
        owner = USER_ID
        oco_ids = {c["oco"] for c in _conditional if c.get("owner") == owner and c.get("reduce_only")}
        del oco_ids  # инфо-поле, чистим всю группу ниже
        _conditional[:] = [
            c for c in _conditional
            if not (c.get("owner") == owner and c.get("reduce_only"))
        ]

        # поставить новые, если заданы
        if tp is not None or sl is not None:
            _place_conditional(
                owner, side_close,
                tp=float(tp) if tp is not None else None,
                sl=float(sl) if sl is not None else None,
                qty=qty,
                trigger_by=trig_by
            )
        # разослать актуальное состояние
        socketio.emit("conditional_update", _conditional)
    except Exception as e:
        emit("error", {"message": f"set_conditionals: {e}"})


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    logger.info(f"Client disconnected: {sid}")


@socketio.on('add_order')
def handle_add_order(data):
    try:
        side = OrderSide.BID if data.get('side') == 'buy' else OrderSide.ASK
        otype = data.get('order_type', 'limit')
        order_type = OrderType.LIMIT if otype == 'limit' else OrderType.MARKET

        qty = float(data.get('volume'))
        px = float(data.get('price')) if data.get('price') is not None else account.mark_price()

        ok, reason = account.can_place(side, qty, px, otype)
        if not ok:
            emit("error", {"message": f"Order rejected: {reason}"})
            return

        # ордер
        order = Order(
            order_id=str(uuid.uuid4()),
            agent_id=data.get('agent_id') or USER_ID,
            side=side,
            volume=qty,
            price=None if order_type == OrderType.MARKET else float(data.get('price')),
            order_type=order_type,
            ttl=None
        )
        trades = order_book.add_order(order)

        if trades:
            _process_exchange_trades(trades, candle_price_mode="trade")

        is_taker = (order_type == OrderType.MARKET)
        if (data.get('agent_id') or USER_ID) == USER_ID:
            USER_ORDERS[order.order_id] = {"is_taker": is_taker}

        # TP/SL как условные (reduce-only)
        if data.get('tpsl_enabled'):
            trig_by = data.get('trigger_by', 'mark')
            tp = float(data['tp']) if data.get('tp') not in (None, "", "null") else None
            sl = float(data['sl']) if data.get('sl') not in (None, "", "null") else None

            # направлением закрытия будет противоположная сторона
            side_close = 'sell' if side == OrderSide.BID else 'buy'

            # берём реальный текущий объём позиции (если он есть), иначе исходный объём ордера
            cond_qty = abs(account.position_qty) if account.position_qty != 0 else qty

            _place_conditional(
                USER_ID,
                side_close,
                tp=tp,
                sl=sl,
                qty=cond_qty,
                trigger_by=trig_by
            )
            logger.info(
                f"[TP/SL] Conditional placed: side_close={side_close}, tp={tp}, sl={sl}, qty={cond_qty}, trig_by={trig_by}")
        emit("confirmation", {"message": f"Order {order.order_id} accepted"})
        emit("account_update", account.snapshot())
        socketio.emit("positions_update", account.snapshot())
    except Exception as e:
        logger.error(f"add_order error: {e}")
        emit("error", {"message": str(e)})


@socketio.on('set_account')
def handle_set_account(data):
    try:
        lv = int(str(data.get('leverage', account.leverage)).replace('x', ''))
        mode = data.get('mode', account.mode)
        account.leverage = max(1, min(125, lv))
        account.mode = "isolated" if str(mode).lower().startswith("изол") or mode == "isolated" else "cross"
        emit("account_update", account.snapshot())
    except Exception as e:
        emit("error", {"message": f"set_account: {e}"})


@socketio.on('cancel_order')
def handle_cancel_order(data):
    oid = data.get('order_id')
    if not oid:
        emit("error", {"message": "Missing order_id"})
        return
    if order_book.cancel_order(oid):
        emit("confirmation", {"message": f"Order {oid} cancelled"})
        emit("account_update", account.snapshot())
    else:
        emit("error", {"message": f"Order {oid} not found or inactive"})


@socketio.on('close_position')
def handle_close_position(data):
    if account.position_qty == 0:
        emit("error", {"message": "Нет открытой позиции"})
        return

    px = order_book._best_bid_price() if account.position_qty > 0 else order_book._best_ask_price()
    if px is None:
        emit("error", {"message": "Нет ликвидности для закрытия"})
        return

    qty = abs(account.position_qty)
    pos_side = "buy" if account.position_qty > 0 else "sell"
    side = OrderSide.ASK if account.position_qty > 0 else OrderSide.BID

    # --- PnL ---
    if account.entry_price is not None:
        if account.position_qty > 0:  # long закрывался
            trade_pnl = (px - account.entry_price) * qty
        else:  # short закрывался
            trade_pnl = (account.entry_price - px) * qty
    else:
        trade_pnl = 0.0

    # Прямое схлопывание через account.apply_fill (reduce-only)
    account.apply_fill(side, px, qty, is_taker=True)

    # --- Запись итоговой сделки в историю ---
    USER_TRADES.append({
        "ts": int(time.time()),
        "side": "buy" if pos_side == "buy" else "sell",
        "price": px,
        "qty": qty,
        "fee_delta": account.last_fee_rate * px * qty * -1,
        "realized_delta": trade_pnl,
        "equity_after": account.equity()
    })
    socketio.emit("user_trades", list(USER_TRADES)[-100:])

    # Формируем простой трейд только для фронта
    trade = {
        "price": px,
        "volume": qty,
        "side": "sell" if side == OrderSide.ASK else "buy",
        "initiator_side": "buy" if (side == OrderSide.BID) else "sell",
        "taker": USER_ID,
        "maker": "system"
    }

    emit("confirmation", {"message": "Позиция закрыта"})
    socketio.emit("account_update", account.snapshot())
    socketio.emit("positions_update", account.snapshot())
    socketio.emit("trade", trade)


@socketio.on('close_partial')
def handle_close_partial(data):
    if account.position_qty == 0:
        emit("error", {"message": "Нет открытой позиции"})
        return

    try:
        qty = float(data.get("qty", 0))
    except:
        emit("error", {"message": "Неверный объём"})
        return

    if qty <= 0 or qty > abs(account.position_qty):
        emit("error", {"message": f"Неверный объём: {qty}"})
        return

    px = order_book._best_bid_price() if account.position_qty > 0 else order_book._best_ask_price()
    if px is None:
        emit("error", {"message": "Нет ликвидности для закрытия"})
        return

    pos_side = "buy" if account.position_qty > 0 else "sell"
    side = OrderSide.ASK if account.position_qty > 0 else OrderSide.BID
    account.apply_fill(side, px, qty, is_taker=True)

    # --- Запись итоговой частичной сделки в историю ---
    USER_TRADES.append({
        "ts": int(time.time()),
        "side": pos_side,
        "price": px,
        "qty": qty,
        "fee_delta": account.last_fee_rate * px * qty * -1,
        "realized_delta": account.realized_pnl,
        "equity_after": account.equity()
    })
    socketio.emit("user_trades", list(USER_TRADES)[-100:])

    trade = {
        "price": px,
        "volume": qty,
        "side": "sell" if side == OrderSide.ASK else "buy",
        "initiator_side": "buy" if (side == OrderSide.BID) else "sell",
        "taker": USER_ID,
        "maker": "system"
    }

    emit("confirmation", {"message": f"Закрыто {qty} контрактов"})
    socketio.emit("account_update", account.snapshot())
    socketio.emit("positions_update", account.snapshot())
    socketio.emit("trade", trade)


@app.route("/candles", methods=["GET"])
def get_candles():
    tf = int(request.args.get("interval", 5))
    candles = candle_managers[tf].get_candles()
    return jsonify([
        {
            "timestamp": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume
        } for c in candles
    ])


@app.route("/api/trades")
def api_trades():
    try:
        ts_from = int(request.args.get("from", str(int(time.time()) - 3600)))
        ts_to = int(request.args.get("to", str(int(time.time()))))
        limit = int(request.args.get("limit", "2000"))
        side = request.args.get("side")  # 'buy' | 'sell' | None
        order = request.args.get("order", "asc")
    except Exception:
        return jsonify({"error": "bad params"}), 400
    data = tr_load_trades(TRDB, ts_from, ts_to, limit, side, order)
    return jsonify(data)


# @socketio.on('candles')
# def handle_candles(data=None):
# test = [
#      {"timestamp": 1, "open": 100, "high": 105, "low": 95, "close": 102, "volume": 10},
#       {"timestamp": 2, "open": 102, "high": 106, "low": 101, "close": 104, "volume": 15},
#   ]
#   emit('candles', test)


@app.route("/market_phases")
def get_market_phases():
    # последние 200 фаз, чтобы не грузить лишнего
    return jsonify(phase_tracker.export())


# --- Runtime helpers / trade processing ---
def _mark_account_dirty(stats: bool = False):
    global _ACCOUNT_DIRTY, _ACCOUNT_STATS_DIRTY
    _ACCOUNT_DIRTY = True
    if stats:
        _ACCOUNT_STATS_DIRTY = True


def _mark_conditionals_dirty():
    global _CONDITIONAL_DIRTY
    _CONDITIONAL_DIRTY = True


def _enqueue_front_trade(trade):
    if not trade:
        return
    PENDING_TRADES.append(dict(trade))


def _best_mid_price(fallback=None):
    bid = order_book._best_bid_price()
    ask = order_book._best_ask_price()
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return fallback


def _process_exchange_trades(trades, candle_price_mode="trade"):
    """
    Единая обработка биржевых сделок:
    - уведомляет агентов о fill;
    - обновляет account dirty flags;
    - буферизует trades для БД;
    - кладёт trades в очередь фронта;
    - обновляет свечи в памяти и сохраняет их как раньше.

    candle_price_mode:
      "trade" — свечи обновляются ценой сделки;
      "mid"   — свечи обновляются mid-price, как это было в старом agents_loop().
    """
    if not trades:
        return

    for t in trades:
        notify_agent_fill(t)

        side_str = str(t.get("taker_side", "")).lower()
        t["initiator_side"] = "buy" if side_str in ("buy", "bid") else "sell"

        side = "buy" if t["initiator_side"] == "buy" else "sell"
        TR_BUF.append((int(time.time()), float(t["price"]), float(t["volume"]), side))

        _enqueue_front_trade(t)

        candle_price = float(t["price"])
        if candle_price_mode == "mid":
            candle_price = _best_mid_price(fallback=candle_price)

        for tf, cm in candle_managers.items():
            cm.update(candle_price, t["volume"])
            if cm.current_candle:
                insert_candle(conn, tf, cm.current_candle)


def _update_market_context(sweep_occurred=False):
    global _last_phase_name, _last_phase_start, _last_microphase

    if not hasattr(order_book, "market_context"):
        return

    ctx = order_book.market_context
    snapshot = order_book.get_order_book_snapshot(depth=20)

    now_ts = time.time()
    mono_ts = time.monotonic()

    if hasattr(ctx, "update_clock"):
        ctx.update_clock(now_ts=now_ts, mono_ts=mono_ts)

    best_bid = None
    best_ask = None

    try:
        bids = snapshot.get("bids") or []
        asks = snapshot.get("asks") or []

        if bids:
            best_bid = float(bids[0].get("price"))
        if asks:
            best_ask = float(asks[0].get("price"))
    except Exception:
        pass

    mid_price = (best_bid + best_ask) / 2.0 if best_bid is not None and best_ask is not None else None

    ctx.update(
        tick=now_ts,
        mid_price=mid_price,
        snapshot=snapshot,
        sweep=bool(sweep_occurred)
    )

    phase_tracker.update(ctx)

    phase = ctx.phase.name if ctx.phase else "undefined"
    micro = ctx.phase.reason.get("microphase", "") if ctx.phase and hasattr(ctx.phase, "reason") else ""
    now_i = int(now_ts)

    if phase != _last_phase_name or micro != _last_microphase:
        if _last_phase_name is not None and _last_phase_start is not None:
            market_phases_history.append({
                "start": _last_phase_start,
                "end": now_i,
                "phase": _last_phase_name,
                "microphase": _last_microphase or ""
            })

        _last_phase_name = phase
        _last_phase_start = now_i
        _last_microphase = micro


def _check_liquidation_and_make_trades():
    if account.position_qty == 0:
        return []

    liq = account.liquidation_price()
    mark = account.mark_price()

    if liq is None:
        return []

    if (account.position_qty > 0 and mark <= liq) or (account.position_qty < 0 and mark >= liq):
        trades = order_book.add_order(Order(
            order_id=str(uuid.uuid4()),
            agent_id=USER_ID,
            side=OrderSide.ASK if account.position_qty > 0 else OrderSide.BID,
            volume=abs(account.position_qty),
            price=None,
            order_type=OrderType.MARKET,
            ttl=None
        ))
        socketio.emit("liquidation", {"liq_price": liq, "qty": abs(account.position_qty)})
        return trades or []

    return []


def _flush_front_trades(max_events=1500):
    sent = 0
    while PENDING_TRADES and sent < max_events:
        socketio.emit("trade", PENDING_TRADES.popleft())
        sent += 1


def _emit_pending_account_state(force=False):
    global _ACCOUNT_DIRTY
    if force or _ACCOUNT_DIRTY:
        snap = account.snapshot()
        socketio.emit("account_update", snap)
        socketio.emit("positions_update", snap)
        _ACCOUNT_DIRTY = False


def _emit_pending_stats(force=False):
    global _ACCOUNT_STATS_DIRTY
    if force or _ACCOUNT_STATS_DIRTY:
        socketio.emit("account_stats", _calc_account_stats())
        socketio.emit("user_trades", list(USER_TRADES)[-500:])
        _ACCOUNT_STATS_DIRTY = False


def _emit_pending_conditionals(force=False):
    global _CONDITIONAL_DIRTY
    if force or _CONDITIONAL_DIRTY:
        socketio.emit("conditional_update", _conditional)
        _CONDITIONAL_DIRTY = False


# --- Fill уведомление ---
def notify_agent_fill(trade):
    price = trade['price']
    volume = trade['volume']

    for agent in AGENTS:
        if agent.agent_id == trade['buy_agent']:
            agent.on_order_filled(trade['buy_order_id'], price, volume, OrderSide.BID)
        if agent.agent_id == trade['sell_agent']:
            agent.on_order_filled(trade['sell_order_id'], price, volume, OrderSide.ASK)

        # учёт терминала с ролью maker/taker
        if trade['buy_agent'] == USER_ID:
            meta = USER_ORDERS.get(trade['buy_order_id'], {"is_taker": True})
            before_realized = account.realized_pnl
            before_fee = account.fee_paid
            account.apply_fill(OrderSide.BID, trade['price'], trade['volume'], is_taker=meta["is_taker"])
            realized_delta = account.realized_pnl - before_realized
            fee_delta = account.fee_paid - before_fee
            _mark_account_dirty(stats=True)

        if trade['sell_agent'] == USER_ID:
            meta = USER_ORDERS.get(trade['sell_order_id'], {"is_taker": True})
            before_realized = account.realized_pnl
            before_fee = account.fee_paid
            account.apply_fill(OrderSide.ASK, trade['price'], trade['volume'], is_taker=meta["is_taker"])
            realized_delta = account.realized_pnl - before_realized
            fee_delta = account.fee_paid - before_fee
            _mark_account_dirty(stats=True)

    _mark_account_dirty()


# --- Matching и трансляция ---
def market_engine_loop():
    """
    Быстрый цикл движка рынка.
    Frontend/broadcast отделён, но сам exchange-step не должен крутиться
    каждые 10ms, иначе микроструктура становится слишком линейной.
    """
    flush_trades.last = time.time()

    last_exchange_step = 0.0
    exchange_step_interval = 0.075  # 50ms. Если нужно грязнее — 0.075/0.10

    last_context_update = 0.0
    context_update_interval = 0.30

    pending_context_sweep = False

    while not SHUTTING_DOWN:
        try:
            now_ts = time.time()
            trades = []

            # Быстрый server loop остаётся, но стакан шагает не каждые 10ms.
            if now_ts - last_exchange_step >= exchange_step_interval:
                trades = order_book.tick() or []
                trades += order_book.match()

                if hasattr(order_book, "is_crossed") and order_book.is_crossed():
                    trades += order_book.repair_crossed_book()

                liq_trades = _check_liquidation_and_make_trades()
                if liq_trades:
                    trades += liq_trades

                if trades:
                    pending_context_sweep = True

                # Лог стакана оставляем.
                LOGGER.on_book(order_book)
                LOGGER.on_trades(trades)

                _eval_and_fire_conditionals()

                if now_ts - getattr(flush_trades, "last", 0) >= 60:
                    flush_trades(now_ts)
                    flush_trades.last = now_ts

                if trades:
                    _process_exchange_trades(trades, candle_price_mode="trade")

                last_exchange_step = now_ts

            # Контекст не должен обновляться каждые 10ms.
            if now_ts - last_context_update >= context_update_interval:
                _update_market_context(sweep_occurred=pending_context_sweep)
                pending_context_sweep = False
                last_context_update = now_ts

        except Exception as e:
            logger.error(f"market engine loop error: {e}")

        socketio.sleep(0.01)


def broadcast_loop():
    """
    Отдельный цикл фронта.
    Даже если SocketIO/JSON/клиент тормозит, market_engine_loop продолжает крутиться.
    Свечная логика не изменена: фронт всё ещё получает get_candles().
    """
    while not SHUTTING_DOWN:
        try:
            _flush_front_trades()

            # Отправляем свечи как раньше, но уже вне engine-loop.
            for tf, cm in candle_managers.items():
                socketio.emit("candles", [
                    {
                        "timestamp": c.timestamp,
                        "open": c.open,
                        "high": c.high,
                        "low": c.low,
                        "close": c.close,
                        "volume": c.volume
                    }
                    for c in cm.get_candles()
                ])

            socketio.emit("orderbook_update", order_book.get_order_book_snapshot(depth=200))
            _emit_pending_account_state(force=True)
            _emit_pending_stats()
            _emit_pending_conditionals()

        except Exception as e:
            logger.error(f"broadcast loop error: {e}")

        socketio.sleep(0.3)


# compatibility alias: старое имя больше не используется в MAIN, но оставлено на случай внешних импортов
def match_and_broadcast():
    market_engine_loop()


def _log_user_trade(side, price, qty, is_taker, fee_delta, realized_delta, equity_after):
    USER_TRADES.append({
        "ts": time.time(),
        "side": side,
        "price": float(price),
        "qty": float(qty),
        "is_taker": is_taker,
        "fee_delta": float(fee_delta),
        "realized_delta": float(realized_delta),
        "equity_after": float(equity_after),
    })


def _calc_account_stats():
    trades = list(USER_TRADES)
    realized_total = sum(t["realized_delta"] for t in trades)
    fees_total = sum(t.get("fee_delta", 0.0) for t in trades)
    closed = [t for t in trades if t["realized_delta"] != 0]
    wins = sum(1 for t in closed if t["realized_delta"] > 0)
    losses = sum(1 for t in closed if t["realized_delta"] < 0)
    winrate = wins / max(1, (wins + losses))
    # max drawdown по equity
    peak = float("-inf")
    dd = 0.0
    for t in trades:
        eq = t["equity_after"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "realized_total": round(realized_total, 2),
        "fees_total": round(fees_total, 2),
        "trades_count": len(trades),
        "closed_count": len(closed),
        "winrate": round(winrate, 4),
        "max_drawdown": round(dd, 2),
    }


def _emit_account_stats(socketio):
    _mark_account_dirty(stats=True)


# --- Отправка свечей ---


# --- Агенты: разный ритм для каждого ---
def agents_loop():
    global BOOTSTRAP_UNTIL

    base_interval = {}
    next_call = {a.agent_id: 0.0 for a in AGENTS}
    agent_cursor = 0
    max_agents_per_pass = len(AGENTS)

    def _init_interval(agent):
        # CALIB: EUR/USD has ~1167 book updates/sec; sim had ~40.
        # Faster intervals give ~120-150/sec (10x improvement, still 8x below EUR).
        if isinstance(agent, PassiveDepthProvider):
            lo, hi = 0.10, 0.28
        elif isinstance(agent, LatencyArb):
            lo, hi = 0.10, 0.30
        elif isinstance(agent, AdvancedMarketMaker):
            lo, hi = 0.10, 0.25
        elif isinstance(agent, AdvancedMarketMaker2):
            lo, hi = 0.08, 0.18
        elif isinstance(agent, RetailTrader):
            lo, hi = 0.90, 2.50
        else:
            lo, hi = 0.25, 0.80

        base = random.uniform(lo, hi)

        if hasattr(agent, "loop_interval"):
            try:
                base = float(getattr(agent, "loop_interval"))
            except Exception:
                pass

        next_call[agent.agent_id] = time.time() + random.uniform(0.0, base)
        return base

    while not SHUTTING_DOWN:
        now = time.time()
        processed_due_agents = 0
        n = len(AGENTS)

        # Ротационный проход: если много агентов due одновременно,
        # не обрабатываем всех одним большим залпом.
        for _ in range(n):
            agent = AGENTS[agent_cursor % n]
            agent_cursor = (agent_cursor + 1) % n

            if 'BOOTSTRAP_UNTIL' in globals() and BOOTSTRAP_UNTIL and now < BOOTSTRAP_UNTIL:
                if not isinstance(agent, PassiveDepthProvider):
                    continue

            if agent.agent_id not in base_interval:
                base_interval[agent.agent_id] = _init_interval(agent)

            if now < next_call[agent.agent_id]:
                continue

            try:
                if isinstance(agent, BreakoutTrader):
                    agent.update_market_structure(conn)

                if 'AbsorptionReversalAgent' in globals() and isinstance(agent, AbsorptionReversalAgent):
                    candles = candle_managers[60].get_candles()
                    agent.update_price_window(candles)

                if getattr(agent, "supports_conn", False):
                    new_orders = agent.generate_orders(order_book, order_book.market_context, conn=conn)
                else:
                    new_orders = agent.generate_orders(order_book, order_book.market_context)

                for order in new_orders:
                    trades = order_book.add_order(order)
                    LOGGER.on_trades(trades)
                    LOGGER.on_book(order_book)

                    if trades:
                        _process_exchange_trades(trades, candle_price_mode="mid")

            except Exception as e:
                logger.error(f"[Agent {agent.agent_id}] error: {e}")

            base = base_interval[agent.agent_id]
            base = max(0.03, base * random.uniform(0.98, 1.02))
            base_interval[agent.agent_id] = base

            jitter = random.uniform(-0.25 * base, 0.35 * base)
            micro_pause = random.uniform(0.0, 0.015)
            next_call[agent.agent_id] = now + max(0.01, base + jitter + micro_pause)

            processed_due_agents += 1
            if processed_due_agents >= max_agents_per_pass:
                break

        if BOOTSTRAP_UNTIL:
            done = not getattr(depth_provider, "bootstrap_active", False)
            if done or time.time() >= BOOTSTRAP_UNTIL:
                BOOTSTRAP_UNTIL = None

        socketio.sleep(0.02)


class Candle:
    def __init__(self, timestamp, open, high, low, close, volume):
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class CandleManager:
    def __init__(self, interval_seconds=5):
        self.interval = interval_seconds
        self.current_candle = None
        self.history = []

    def update(self, trade_price, trade_volume, trade_time=None):
        now = trade_time or time.time()
        bucket = int(now // self.interval) * self.interval

        if not self.current_candle or self.current_candle.timestamp != bucket:
            if self.current_candle:
                self.history.append(self.current_candle)
            self.current_candle = Candle(
                timestamp=bucket,
                open=trade_price,
                high=trade_price,
                low=trade_price,
                close=trade_price,
                volume=trade_volume
            )
            self.current_candle._has_trades = True
        else:
            c = self.current_candle
            if not getattr(c, '_has_trades', False):
                c.open = trade_price
                c.high = trade_price
                c.low = trade_price
                c._has_trades = True
            else:
                c.high = max(c.high, trade_price)
                c.low = min(c.low, trade_price)
            c.close = trade_price
            c.volume += trade_volume

    def get_candles(self, limit=1000):
        return self.history[-limit:] + ([self.current_candle] if self.current_candle else [])

    def tick(self, last_price=None):
        now = time.time()
        bucket = int(now // self.interval) * self.interval

        if not self.current_candle or self.current_candle.timestamp != bucket:
            if self.current_candle:
                self.history.append(self.current_candle)

            # Новый способ определения стартовой цены:
            ref_price = None
            if self.current_candle:
                ref_price = self.current_candle.close
            elif self.history:
                ref_price = self.history[-1].close
            elif last_price:
                ref_price = last_price
            else:
                ref_price = 100.0  # самый последний fallback

            self.current_candle = Candle(
                timestamp=bucket,
                open=ref_price,
                high=ref_price,
                low=ref_price,
                close=ref_price,
                volume=0.0
            )


def candle_tick_loop():
    while not SHUTTING_DOWN:
        # Предполагаем, что best mid-price = средняя между bid/ask
        bid = order_book._best_bid_price()
        ask = order_book._best_ask_price()
        mid = (bid + ask) / 2 if bid and ask else None

        for cm in candle_managers.values():
            cm.tick(last_price=mid)

        socketio.sleep(1.0)


# --- Инициализация менеджеров таймфреймов ---
first_run = True

candle_managers = {
    tf: CandleManager(interval_seconds=tf) for tf in [1, 15, 60, 300, 3600]
}
now = int(time.time())
from_ts = 0
to_ts = now + 100000

if first_run:
    for tf, cm in candle_managers.items():
        cm.history = fill_incomplete_candle(cm.history, tf)  # Загружаем данные из базы
        print(f"[INFO] Loaded {len(cm.history)} candles for tf {tf}s")
    first_run = False  # После подгрузки, флаг
for tf, cm in candle_managers.items():
    cm.history = load_candles(conn, tf, from_ts, to_ts)
    print(f"[INFO] Loaded {len(cm.history)} candles for tf {tf}s")


def memory_logger_loop():
    tick_count = 0
    while not SHUTTING_DOWN:
        tick_count += 1
        if tick_count % (60 * 2) == 0:
            memory_logger.log_memory_snapshot(label=f"Tick {tick_count}")
        socketio.sleep(0.5)  # даём управление другим задачам


# --- MAIN ---
if __name__ == '__main__':
    socketio.start_background_task(market_engine_loop)
    socketio.start_background_task(broadcast_loop)
    socketio.start_background_task(agents_loop)
    socketio.start_background_task(candle_tick_loop)
    socketio.start_background_task(memory_logger_loop)
    socketio.start_background_task(trade_flush_task)
    logger.info("Starting server on http://0.0.0.0:8000 …")
    socketio.run(app, host='0.0.0.0', port=8000)
