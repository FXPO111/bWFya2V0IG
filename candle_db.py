import sqlite3
import os
import time

from models import Candle

DB_FILE = 'candles.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS candles (
            interval TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (interval, timestamp)
        )
    ''')
    conn.commit()
    return conn

def fill_incomplete_candle(candles, interval):
    if not candles:
        return candles  # Если свечей нет, возвращаем пустой список

    last_candle = candles[-1]  # Берем последнюю свечу
    last_timestamp = last_candle.timestamp  # Время последней свечи

    # Время до текущего момента
    current_time = int(time.time())
    time_diff = current_time - last_timestamp  # Разница во времени

    print(f"last_timestamp: {last_timestamp}, current_time: {current_time}, time_diff: {time_diff}")

    # Сколько свечей нужно добавить
    missing_candles = time_diff // interval  # Количество недостающих свечей
    print(f"missing_candles: {missing_candles}")

    # Если времени не хватает для добавления хотя бы одной свечи
    if missing_candles == 0:
        print("No missing candles.")
        return candles

    # Заполняем недостающие свечи
    for i in range(missing_candles):
        filled_candle = Candle(
            timestamp=last_timestamp + (i + 1) * interval,  # Время для следующей свечи
            open=last_candle.close,  # Открытие на уровне закрытия последней свечи
            high=last_candle.close,  # Макс. и мин. цена не меняются
            low=last_candle.close,
            close=last_candle.close,
            volume=0.0  # Объем будет равен 0
        )
        candles.append(filled_candle)

    return candles




def insert_candle(conn, interval, candle):
    cur = conn.cursor()
    cur.execute('''
        INSERT OR REPLACE INTO candles (interval, timestamp, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        interval,
        candle.timestamp,
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume
    ))

    conn.commit()


def load_candles(conn, interval, from_ts, to_ts):
    cur = conn.cursor()
    cur.execute('''
        SELECT timestamp, open, high, low, close, volume
        FROM candles
        WHERE interval = ? AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC
    ''', (interval, from_ts, to_ts))
    rows = cur.fetchall()
    return [
        Candle(
            timestamp=row[0],
            open=row[1],
            high=row[2],
            low=row[3],
            close=row[4],
            volume=row[5]
        )
        for row in rows
    ]

