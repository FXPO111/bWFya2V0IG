# trade_logger.py
import time, threading
from collections import defaultdict
from builtins import print as _p        

INTERVAL = 60            # секунд между сводками
_stats   = defaultdict(lambda: {"qty": 0, "trades": 0})
_next    = time.time() + INTERVAL
_lock    = threading.Lock()

def log(agent_id: str, qty: float):
    global _next
    with _lock:
        s = _stats[agent_id]
        s["qty"]    += qty
        s["trades"] += 1

        now = time.time()
        if now >= _next:
            _flush(now)
            _next = now + INTERVAL

def _flush(ts):
    _p(f"\n[{time.strftime('%H:%M:%S', time.localtime(ts))}] 1-min summary")
    for aid, s in _stats.items():
        _p(f"  {aid:<14} trades:{s['trades']:5d}   qty:{s['qty']:,.0f}")
    _stats.clear()