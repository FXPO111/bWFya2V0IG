import tracemalloc
import time
import os

tracemalloc.start()

MEMORY_LOG_FILE = "logs/memory_usage.log"

def log_memory_snapshot(label="Snapshot", top=10):
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics('lineno')

    with open(MEMORY_LOG_FILE, "a") as f:
        f.write(f"\n==== {label} @ {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        for i, stat in enumerate(top_stats[:top], 1):
            f.write(f"#{i}: {stat}\n")
        total = sum(stat.size for stat in top_stats)
        f.write(f"Total captured (top {top}): {total / 1024:.1f} KiB\n")
