# polygon_engine.py - Structured logging + fail-safe upgrades

import time
import schedule
from datetime import datetime
from pytz import timezone

from polygon_utils import get_top_gainers_polygon, filter_gainers_for_momentum,cache_quotes_for_top_gainers
from modules.log_utils import log
from train_hybrid_model import load_training_symbols
from track_price_windows import load_all_closed_trades, analyze_price_windows, save_summary

TOP_GAINERS_JSON = "exports/top_gainers.json"
LOCAL_TZ = timezone("America/Toronto")
TRADE_PATH = "exports/live_trades.json"
OUTPUT_PATH = "exports/price_window_summary.json"

def  job_update_price_windows():
    
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log(f"🔄 Updating price windows at {now}", "INFO", "PolygonEngine")
    try:
        all_trades = load_all_closed_trades(TRADE_PATH)
        summary = analyze_price_windows(all_trades)
        save_summary(summary, OUTPUT_PATH)
    except Exception as e:
        log(f"Error in update_price_windows: {e}", "ERROR", "PolygonEngine")


def job_get_top_gainers():
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log(f"🚀 Running get_top_gainers_polygon() at {now}", "INFO", "PolygonEngine")
    try:
        df = get_top_gainers_polygon(limit=13)
        log(f"Top gainers snapshot:\n{df}", "INFO", "PolygonEngine")
    except Exception as e:
        log(f"Error in get_top_gainers_polygon: {e}", "ERROR", "PolygonEngine")
        
def job_cache_polygon_quotes():
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log(f"⚡ Caching Polygon quotes at {now}", "INFO", "PolygonEngine")
    try:
        cache_quotes_for_top_gainers()
    except Exception as e:
        log(f"Error in cache_quotes_for_top_gainers: {e}", "ERROR", "PolygonEngine")

def job_filter_momentum():
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log(f"⚡ Running filter_gainers_for_momentum() at {now}", "INFO", "PolygonEngine")
    try:
        filter_gainers_for_momentum()
    except Exception as e:
        log(f"Error in filter_gainers_for_momentum: {e}", "ERROR", "PolygonEngine")
        
def job_training_model():
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log(f"⚡ Running train_hybrid_model() at {now}", "INFO", "PolygonEngine")
    try:
        load_training_symbols(TOP_GAINERS_JSON)
    except Exception as e:
        log(f"Error in ftrain_hybrid_model.py: {e}", "ERROR", "PolygonEngine")

def main():
    log("✅ Polygon Engine Started.", "INFO", "PolygonEngine")
    log(" - Fetches top gainers every 10 minutes.", "INFO", "PolygonEngine")
    log(" - Filters for momentum every 2 minutes.", "INFO", "PolygonEngine")
    log(" - Caches quotes every 60 seconds.", "INFO", "PolygonEngine")
    log(" - Updates price windows every 3 minutes.", "INFO", "PolygonEngine")
    log(" - Retrains hybrid model every 11 minutes.", "INFO", "PolygonEngine")

    # Schedule jobs
    schedule.every(10).minutes.do(job_get_top_gainers)
    schedule.every(11).minutes.do(job_training_model)
    schedule.every(2).minutes.do(job_filter_momentum)
    schedule.every(60).seconds.do(job_cache_polygon_quotes)
    schedule.every(3).minutes.do(job_update_price_windows)

    # Run immediately on start for initialization
    job_get_top_gainers()
    job_cache_polygon_quotes()
    job_filter_momentum()
    job_training_model()
    job_update_price_windows()

    while True:
        schedule.run_pending()
        time.sleep(5)

if __name__ == "__main__":
    main()
