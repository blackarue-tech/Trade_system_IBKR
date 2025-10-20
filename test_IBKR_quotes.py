#test_IBKR_quotes.py


from ib_insync import IB, Stock

ib = IB()
ib.connect('127.0.0.1', 4001, clientId=300)

symbol = "AAPL"  # use a liquid stock
contract = Stock(symbol, "SMART", "USD")
ib.qualifyContracts(contract)
ticker = ib.reqMktData(contract, "", False, False)
ib.sleep(1.5)
ib.cancelMktData(contract)

print({
    "bid": ticker.bid,
    "ask": ticker.ask,
    "last": ticker.last,
    "volume": ticker.volume
})