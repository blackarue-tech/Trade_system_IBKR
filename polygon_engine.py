# polygon_engine.py - Structured logging + fail-safe upgrades

import time
import schedule
from datetime import datetime
from pytz import timezone

from polygon_utils import get_top_gainers_polygon, filter_gainers_for_momentum
from modules.log_utils import log

LOCAL_TZ = timezone("America/Toronto")

def job_get_top_gainers():
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log(f"🚀 Running get_top_gainers_polygon() at {now}", "INFO", "PolygonEngine")
    try:
        df = get_top_gainers_polygon(limit=13)
        log(f"Top gainers snapshot:\n{df}", "INFO", "PolygonEngine")
    except Exception as e:
        log(f"Error in get_top_gainers_polygon: {e}", "ERROR", "PolygonEngine")

def job_filter_momentum():
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log(f"⚡ Running filter_gainers_for_momentum() at {now}", "INFO", "PolygonEngine")
    try:
        filter_gainers_for_momentum()
    except Exception as e:
        log(f"Error in filter_gainers_for_momentum: {e}", "ERROR", "PolygonEngine")

def main():
    log("✅ Polygon Engine Started.", "INFO", "PolygonEngine")
    log(" - Fetches top gainers every 30 minutes.", "INFO", "PolygonEngine")
    log(" - Filters for momentum every 2 minutes.", "INFO", "PolygonEngine")

    # Schedule jobs
    schedule.every(30).minutes.do(job_get_top_gainers)
    schedule.every(2).minutes.do(job_filter_momentum)

    # Run immediately on start for initialization
    job_get_top_gainers()
    job_filter_momentum()

    while True:
        schedule.run_pending()
        time.sleep(5)

if __name__ == "__main__":
    main()
