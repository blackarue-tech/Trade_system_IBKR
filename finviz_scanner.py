# finviz_scanner.py - Robust Finviz top gainers fetcher

import requests
from bs4 import BeautifulSoup

def get_finviz_top_gainers(limit=10):
    url = "https://finviz.com/screener.ashx?v=111&s=ta_topgainers"
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.content, "html.parser")

    # Find table rows containing symbols
    table = soup.find("table", {"class": "table-light"}) or soup.find("table", {"class": "screener-view-table"})
    if not table:
        print("⚠️ Table not found on Finviz page.")
        return []

    symbols = []
    rows = table.find_all("tr")[1:]  # skip header row
    for row in rows:
        cols = row.find_all("td")
        if len(cols) > 1:
            symbol = cols[1].get_text(strip=True)
            symbols.append(symbol)
            if len(symbols) >= limit:
                break
    with open("finviz_debug.html", "w", encoding="utf-8") as f:
        f.write(soup.prettify())
    print("✅ Saved debug HTML for inspection.")            
    print(f"✅ Pulled {len(symbols)} top gainers from Finviz.")
    return symbols

# Test run
if __name__ == "__main__":
    top_gainers = get_finviz_top_gainers(limit=10)
    print("Top Gainers:", top_gainers)