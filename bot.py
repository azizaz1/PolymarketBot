import os
import json
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

load_dotenv()

_client = None

def get_client():
    global _client
    if _client is None:
        _client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("PRIVATE_KEY"),
            chain_id=137
        )
        _client.set_api_creds(_client.create_or_derive_api_creds())
    return _client

# Price history: { token_id: [ {price, time, question, volume}, ... ] }
price_history = {}
# Open positions: { token_id: { entry_price, size, question } }
active_positions = {}


def get_markets():
    res = requests.get(
        "https://gamma-api.polymarket.com/markets"
        "?active=true&limit=100&order=volumeClob&ascending=false&acceptingOrders=true"
    )
    res.raise_for_status()
    return res.json()


def get_orderbook(token_id):
    try:
        res = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}")
        data = res.json()
        best_bid = float(data["bids"][0]["price"]) if data.get("bids") else None
        best_ask = float(data["asks"][0]["price"]) if data.get("asks") else None
        return best_bid, best_ask
    except Exception:
        return None, None


def track_prices(markets):
    for market in markets:
        raw_ids = market.get("clobTokenIds")
        if not raw_ids:
            continue
        token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        if not token_ids:
            continue

        yes_token_id = token_ids[0]
        no_token_id = token_ids[1] if len(token_ids) > 1 else None

        bid = market.get("bestBid")
        ask = market.get("bestAsk")
        if bid is None or ask is None:
            continue

        bid, ask = float(bid), float(ask)
        mid_price = (bid + ask) / 2
        spread = ask - bid

        if yes_token_id not in price_history:
            price_history[yes_token_id] = []

        price_history[yes_token_id].append({
            "price": mid_price,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "no_token_id": no_token_id,
            "time": datetime.now().isoformat(),
            "question": market.get("question", ""),
            "volume": float(market.get("volumeClob") or market.get("volume") or 0),
        })

        # Keep last 20 data points
        price_history[yes_token_id] = price_history[yes_token_id][-20:]


def compute_signals(token_id):
    history = price_history.get(token_id, [])
    prices = [h["price"] for h in history]
    n = len(prices)

    if n < 3:
        return None

    def drift(lookback):
        if n < lookback:
            return None
        old = prices[-lookback]
        if old == 0:
            return None
        return (prices[-1] - old) / old * 100

    drift_short = drift(3)
    drift_long = drift(min(10, n))

    # Fraction of recent steps where price moved up
    steps = min(10, n - 1)
    up_count = sum(1 for i in range(n - steps, n) if prices[i] > prices[i - 1])
    consistency = up_count / steps if steps > 0 else 0.5

    return {
        "drift_short": drift_short,
        "drift_long": drift_long,
        "consistency": consistency,
        "spread": history[-1].get("spread"),
    }


def find_opportunities():
    results = []

    for token_id, history in price_history.items():
        if len(history) < 3:
            continue

        latest = history[-1]
        yes_price = latest["price"]
        volume = latest["volume"]
        question = latest["question"]
        no_token_id = latest.get("no_token_id")

        sig = compute_signals(token_id)
        if sig is None:
            continue

        drift_short = sig["drift_short"]
        drift_long = sig["drift_long"]
        consistency = sig["consistency"]
        spread = sig["spread"]

        # Quality gates
        if volume < 10000:
            continue
        if yes_price > 0.90 or yes_price < 0.02:
            continue
        if token_id in active_positions:
            continue
        if spread is not None and spread > 0.08:
            continue

        # --- YES signal ---
        score_yes = 0
        reasons_yes = []

        if drift_short and drift_short > 0:
            if drift_long and drift_long > 0 and drift_short > drift_long * 1.5:
                score_yes += 2
                reasons_yes.append(f"Accelerating momentum ({drift_short:.1f}% vs {drift_long:.1f}%)")
            elif drift_short > 12:
                score_yes += 2
                reasons_yes.append(f"Strong drift +{drift_short:.1f}%")
            elif drift_short > 5:
                score_yes += 1
                reasons_yes.append(f"Moderate drift +{drift_short:.1f}%")

        if consistency > 0.70:
            score_yes += 2
            reasons_yes.append(f"Consistent uptrend ({consistency:.0%} steps up)")
        elif consistency > 0.55:
            score_yes += 1
            reasons_yes.append(f"Mild uptrend ({consistency:.0%} steps up)")

        if spread is not None and spread < 0.04:
            score_yes += 1
            reasons_yes.append(f"Tight spread ({spread:.3f})")

        if score_yes >= 3:
            results.append({
                "token_id": token_id,
                "question": question,
                "yes_price": yes_price,
                "volume": volume,
                "drift_short": drift_short,
                "drift_long": drift_long,
                "consistency": consistency,
                "score": score_yes,
                "reasons": reasons_yes,
                "side": "YES",
            })

        # --- NO signal ---
        score_no = 0
        reasons_no = []
        no_price = round(1 - yes_price, 4)

        if no_price > 0.98 or no_price < 0.10:
            pass  # skip extreme NO prices
        else:
            if drift_short and drift_short < -5:
                if drift_short < -12:
                    score_no += 2
                    reasons_no.append(f"Strong drop {drift_short:.1f}%")
                else:
                    score_no += 1
                    reasons_no.append(f"Moderate drop {drift_short:.1f}%")

            if consistency < 0.30:
                score_no += 2
                reasons_no.append(f"Consistent downtrend ({consistency:.0%} steps up)")
            elif consistency < 0.45:
                score_no += 1
                reasons_no.append(f"Mild downtrend ({consistency:.0%} steps up)")

            if spread is not None and spread < 0.04:
                score_no += 1
                reasons_no.append(f"Tight spread ({spread:.3f})")

            if score_no >= 3 and no_token_id:
                results.append({
                    "token_id": no_token_id,
                    "question": question,
                    "yes_price": yes_price,
                    "volume": volume,
                    "drift_short": drift_short,
                    "drift_long": drift_long,
                    "consistency": consistency,
                    "score": score_no,
                    "reasons": reasons_no,
                    "side": "NO",
                })

    return sorted(results, key=lambda x: x["score"], reverse=True)


def place_bet(token_id, price, usdc_amount=2.0):
    size = round(usdc_amount / price, 2)
    order_args = OrderArgs(
        token_id=token_id,
        price=round(price + 0.01, 2),
        size=size,
        side="BUY",
    )
    signed_order = get_client().create_order(order_args)
    return get_client().post_order(signed_order, OrderType.GTC)


def place_sell(token_id, size):
    bid, _ = get_orderbook(token_id)
    if bid is None:
        print("  Cannot sell — no bid found.")
        return
    order_args = OrderArgs(
        token_id=token_id,
        price=round(bid - 0.01, 2),
        size=size,
        side="SELL",
    )
    signed_order = get_client().create_order(order_args)
    return get_client().post_order(signed_order, OrderType.GTC)


def check_exits():
    for token_id, position in list(active_positions.items()):
        bid, _ = get_orderbook(token_id)
        if bid is None:
            continue

        entry = position["entry_price"]
        pnl_pct = (bid - entry) / entry * 100
        label = position["question"][:50]

        if pnl_pct >= 40:
            print(f"  TAKE PROFIT: {label} +{pnl_pct:.1f}%")
            place_sell(token_id, position["size"])
            del active_positions[token_id]

        elif pnl_pct <= -30:
            print(f"  STOP LOSS:   {label} {pnl_pct:.1f}%")
            place_sell(token_id, position["size"])
            del active_positions[token_id]


def run_bot():
    print("Polymarket bot started.")
    print("Building price history — first signals in ~5 minutes...\n")

    cycle = 0

    while True:
        cycle += 1
        print(f"--- Cycle {cycle} | {datetime.now().strftime('%H:%M:%S')} ---")

        try:
            markets = get_markets()
            track_prices(markets)

            if active_positions:
                print(f"Open positions: {len(active_positions)}")
                check_exits()

            opportunities = find_opportunities()

            if not opportunities:
                print("No opportunities yet.")
            else:
                print(f"\nTop opportunities:")
                for op in opportunities[:5]:
                    side = op["side"]
                    price = op["yes_price"] if side == "YES" else round(1 - op["yes_price"], 4)
                    drift_s = op["drift_short"]
                    drift_l = op["drift_long"]
                    drift_str = f"{drift_s:+.1f}%" if drift_s else "n/a"
                    if drift_l:
                        drift_str += f" (10-period: {drift_l:+.1f}%)"
                    print(f"\n  [{op['score']}pts] [{side}] {op['question'][:60]}")
                    print(f"  Price: {price:.3f} | Volume: ${op['volume']:,.0f} | Drift: {drift_str}")
                    print(f"  Consistency: {op['consistency']:.0%} up | Reasons: {', '.join(op['reasons'])}")

                best = opportunities[0]
                if best["score"] >= 4:
                    side = best["side"]
                    bet_price = best["yes_price"] if side == "YES" else round(1 - best["yes_price"], 4)
                    confirm = input(f"\nBet $2 {side} on top pick? (y/n): ")
                    if confirm.strip().lower() == "y":
                        result = place_bet(best["token_id"], bet_price)
                        active_positions[best["token_id"]] = {
                            "entry_price": bet_price,
                            "size": round(2.0 / bet_price, 2),
                            "question": best["question"],
                        }
                        print(f"  Order placed: {result}")

        except Exception as e:
            print(f"Error: {e}")

        print(f"\nNext scan in 60s...")
        time.sleep(60)


if __name__ == "__main__":
    run_bot()
