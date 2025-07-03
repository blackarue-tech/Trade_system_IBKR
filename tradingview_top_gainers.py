
# tradingview_top_gainers.py - Updated TradingView Top Gainers Scraper with Stable Selector

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time

options = Options()
options.add_argument('--headless')
options.add_argument('--disable-gpu')
options.add_argument('--no-sandbox')
options.add_argument('--window-size=1920,1080')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

try:
    driver.get('https://www.tradingview.com/markets/stocks-usa/market-movers-gainers/')
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, "//table[contains(@class, 'tv-data-table')]")))
    time.sleep(3)  # allow full render

    rows = driver.find_elements(By.XPATH, "//table[contains(@class, 'tv-data-table')]//tbody/tr")

    top_gainers = []
    for row in rows:
        try:
            symbol = row.find_element(By.XPATH, ".//td[1]//a").text.strip()
            price = float(row.find_element(By.XPATH, ".//td[2]").text.strip().replace(',', ''))
            if price > 50:
                top_gainers.append((symbol, price))
        except Exception as e:
            continue

    print("✅ Top Gainers Above $50:")
    for sym, price in top_gainers:
        print(f"{sym} - ${price}")

except Exception as e:
    print(f"❌ Failed to fetch TradingView top gainers: {e}")
finally:
    driver.quit()
