import sqlite3
import os

# Использование модели Liquidity, если она требуется для хранения данных
#from models import Liquidity


DB_FILE = 'liquidity.db'


# Инициализация базы данных
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    # Создание таблицы для ликвидности
    cur.execute('''
        CREATE TABLE IF NOT EXISTS liquidity_state (
            side TEXT NOT NULL,
            price REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (side, price)
        )
    ''')

    # Создание таблицы для хранения последней цены
    cur.execute('''
        CREATE TABLE IF NOT EXISTS last_price (
            interval TEXT NOT NULL,
            price REAL NOT NULL,
            PRIMARY KEY (interval)
        )
    ''')

    conn.commit()
    return conn


# Сохранение последней цены
def save_last_price(conn, interval, price):
    cur = conn.cursor()
    cur.execute('''
        INSERT OR REPLACE INTO last_price (interval, price)
        VALUES (?, ?)
    ''', (interval, price))
    conn.commit()


# Загрузка ликвидности из базы данных
def load_liquidity_state(conn):
    cur = conn.cursor()
    cur.execute('SELECT side, price, volume FROM liquidity_state')
    rows = cur.fetchall()

    bid_liquidity = {}
    ask_liquidity = {}

    for row in rows:
        side, price, volume = row
        if side == 'bid':
            bid_liquidity[price] = volume
        elif side == 'ask':
            ask_liquidity[price] = volume

    return {'bids': bid_liquidity, 'asks': ask_liquidity}

def save_liquidity_state(conn, liquidity_snapshot):
    cur = conn.cursor()
    cur.execute('DELETE FROM liquidity_state')  # очищаем, чтобы не дублировать

    for side in ['bids', 'asks']:
        for price, volume in liquidity_snapshot[side].items():
            cur.execute('''
                INSERT INTO liquidity_state (side, price, volume)
                VALUES (?, ?, ?)
            ''', (side[:-1], price, volume))  # side: 'bids' → 'bid'

    conn.commit()



# Загрузка последней цены для заданного интервала
def load_last_price(conn, interval):
    cur = conn.cursor()
    cur.execute('SELECT price FROM last_price WHERE interval = ?', (interval,))
    row = cur.fetchone()
    return row[0] if row else None