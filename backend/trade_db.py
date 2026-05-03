# trade_db.py
import sqlite3, time

DB_FILE = "trades.db"

def init_db():
    conn = sqlite3.connect(DB_FILE, isolation_level=None, check_same_thread=False)
    cur = conn.cursor()
    cur.executescript("""
      PRAGMA journal_mode=WAL;
      PRAGMA synchronous=NORMAL;
      PRAGMA temp_store=MEMORY;
      PRAGMA cache_size=-131072;     -- ~128 MB
      CREATE TABLE IF NOT EXISTS trades (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER NOT NULL,   -- unix seconds
        price     REAL NOT NULL,
        volume    REAL NOT NULL,
        side      TEXT NOT NULL CHECK(side IN ('buy','sell'))
      );
      CREATE INDEX IF NOT EXISTS idx_trades_ts        ON trades(timestamp);
      CREATE INDEX IF NOT EXISTS idx_trades_ts_side   ON trades(timestamp, side);
    """)
    return conn

def insert_batch(conn, rows):
    # rows: [(ts, price, vol, side), ...]
    conn.executemany(
        "INSERT INTO trades(timestamp,price,volume,side) VALUES(?,?,?,?)", rows
    )

def load_trades(conn, ts_from, ts_to, limit=1000, side=None, order="asc"):
    q = ["SELECT timestamp,price,volume,side FROM trades WHERE timestamp BETWEEN ? AND ?"]
    args = [int(ts_from), int(ts_to)]
    if side in ("buy","sell"):
        q.append("AND side=?"); args.append(side)
    q.append("ORDER BY timestamp " + ("ASC" if order=="asc" else "DESC"))
    q.append("LIMIT ?"); args.append(int(limit))
    cur = conn.cursor()
    cur.execute(" ".join(q), args)
    return [{"timestamp":r[0], "price":r[1], "volume":r[2], "side":r[3]} for r in cur.fetchall()]

def prune_older_than(conn, keep_seconds):
    cutoff = int(time.time()) - int(keep_seconds)
    conn.execute("DELETE FROM trades WHERE timestamp < ?", (cutoff,))
