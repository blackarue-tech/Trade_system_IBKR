from datetime import datetime, time
import pytz

NY = pytz.timezone("America/New_York")
OPEN = time(9,30)
CLOSE = time(16,0)

def market_is_open(dt: datetime | None = None) -> bool:
    dt = dt or datetime.now(NY)
    if dt.weekday() >= 5:
        return False
    t = dt.time()
    return OPEN <= t < CLOSE
