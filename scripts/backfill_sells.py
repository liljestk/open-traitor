"""Backfill missing SELL trades from Coinbase order history into the stats DB."""

import os
import sys

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras
from src.core.coinbase_client import CoinbaseClient

DB_URL = os.environ["DATABASE_URL"]
client = CoinbaseClient(
    api_key=os.environ["COINBASE_API_KEY"],
    api_secret=os.environ["COINBASE_API_SECRET"],
    paper_mode=False,
)

# 1. Fetch all SELL orders from Coinbase
orders_resp = client._rest_client.list_orders(
    order_status=["FILLED"],
    order_side="SELL",
    limit=100,
    sort_by="LAST_FILL_TIME",
)
sell_orders = orders_resp.orders if hasattr(orders_resp, "orders") else []
print(f"Found {len(sell_orders)} SELL orders on Coinbase")

# 2. Filter to EUR pairs and connect to DB
conn = psycopg2.connect(DB_URL)
conn.cursor_factory = psycopg2.extras.RealDictCursor
cur = conn.cursor()

inserted = 0
skipped = 0

for o in sell_orders:
    d = o if isinstance(o, dict) else o.__dict__ if hasattr(o, "__dict__") else {}
    pair = d.get("product_id", "")
    if not pair.endswith("-EUR") and not pair.endswith("-EURC"):
        skipped += 1
        continue

    qty = float(d.get("filled_size", 0))
    price = float(d.get("average_filled_price", 0))
    fee = float(d.get("total_fees", 0))
    value = float(d.get("filled_value", 0))
    ts = str(d.get("created_time", ""))

    if qty <= 0 or price <= 0:
        skipped += 1
        continue

    # Check if this sell already exists (by pair, action, ts)
    cur.execute(
        "SELECT COUNT(*) as cnt FROM trades WHERE pair = %s AND action = %s AND ts = %s",
        (pair, "sell", ts),
    )
    if cur.fetchone()["cnt"] > 0:
        skipped += 1
        continue

    # Find matching buy to calculate PnL
    pnl = None
    cur.execute(
        "SELECT price FROM trades WHERE pair = %s AND action = %s AND ts < %s ORDER BY ts DESC LIMIT 1",
        (pair, "buy", ts),
    )
    buy_row = cur.fetchone()
    if buy_row:
        buy_price = float(buy_row["price"])
        pnl = (price - buy_price) * qty - fee

    # Insert the sell trade with the original Coinbase timestamp
    cur.execute(
        """INSERT INTO trades
           (ts, exchange, pair, action, quantity, price, quote_amount,
            confidence, signal_type, stop_loss, take_profit, reasoning,
            pnl, fee_quote, is_rotation, approved_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            ts, "coinbase", pair, "sell", qty, price, value,
            0, "backfill", 0, 0, "Backfilled from Coinbase order history",
            pnl, fee, 0, "auto",
        ),
    )
    inserted += 1
    print(f"  + {ts[:19]} {pair} SELL qty={qty} price={price} pnl={pnl}")

conn.commit()
conn.close()
print(f"\nDone: {inserted} sells inserted, {skipped} skipped")
