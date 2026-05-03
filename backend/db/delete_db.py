import sqlite3
from datetime import datetime, timedelta

# Путь к базе данных
db_path = "candles.db"

# Подключение к базе
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Расчет времени 20 минут назад
twenty_minutes_ago = int((datetime.utcnow() - timedelta(minutes=1)).timestamp())

# Удаление записей новее 20 минут
cursor.execute("DELETE FROM candles WHERE timestamp >= ?", (twenty_minutes_ago,))
print(f"Удалено записей: {cursor.rowcount}")

# Сохранение изменений
conn.commit()
conn.close()
