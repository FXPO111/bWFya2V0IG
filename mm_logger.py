import os
import csv
from datetime import datetime

os.makedirs("mm_logs", exist_ok=True)
SUMMARY_MINUTE_LOG = "mm_logs/summary_minute.csv"

if not os.path.exists(SUMMARY_MINUTE_LOG):
    with open(SUMMARY_MINUTE_LOG, mode="w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "agent_id", "total_capital", "total_cash", "total_inventory",
            "avg_mid_price", "total_top_volume"
        ])

def log_summary_minute(
    timestamp, agent_id, total_capital, total_cash,
    total_inventory, avg_mid_price, total_top_volume
):
    with open(SUMMARY_MINUTE_LOG, mode="a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            agent_id, round(total_capital,2), round(total_cash,2),
            round(total_inventory,4), round(avg_mid_price,5),
            round(total_top_volume,2)
        ])
