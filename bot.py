import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler

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
    markets = []
    for offset in range(0, 500, 100):
        res = requests.get(
            "https://gamma-api.polymarket.com/markets"
            f"?active=true&limit=100&offset={offset}&enableOrderBook=true"
            "&order=volumeClob&ascending=false"
        )
        res.raise_for_status()
        page = res.json()
        markets.extend(page)
        if len(page) < 100:
            break
    return markets


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


_news_cache = {}  # { query: { "articles": [...], "fetched_at": datetime } }
_NEWS_TTL = 300   # seconds — refresh every 5 minutes
_STOP_WORDS = {
    "will", "the", "a", "an", "in", "on", "at", "to", "for", "of", "is",
    "be", "by", "or", "and", "any", "all", "from", "than", "that", "this",
    "with", "before", "after", "who", "what", "when", "where", "how",
    "does", "did", "do", "has", "have", "had", "was", "were", "are",
    "been", "get", "make", "more", "less", "per", "as", "if", "not", "no",
    "its", "his", "her", "their", "our", "above", "below", "between",
}


def _keywords(question):
    words = question.lower().replace("?", "").replace(",", "").split()
    return " ".join(w for w in words if w not in _STOP_WORDS and len(w) > 2)[:100]


def fetch_news(question):
    query = _keywords(question)
    if not query:
        return []

    now = datetime.now()
    cached = _news_cache.get(query)
    if cached and (now - cached["fetched_at"]).total_seconds() < _NEWS_TTL:
        return cached["articles"]

    try:
        url = (
            "https://news.google.com/rss/search"
            f"?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        )
        res = requests.get(url, timeout=5)
        root = ET.fromstring(res.text)
        articles = []
        for item in root.findall(".//item")[:5]:
            title = item.findtext("title", "")
            pub_date = item.findtext("pubDate", "")
            try:
                pub_dt = parsedate_to_datetime(pub_date).replace(tzinfo=None)
                age_min = int((now - pub_dt).total_seconds() // 60)
            except Exception:
                age_min = 9999
            articles.append({"title": title, "age_min": age_min})
        _news_cache[query] = {"articles": articles, "fetched_at": now}
        return articles
    except Exception:
        return []


def check_news(question):
    """Return (headline, age_minutes) for the most recent article, or (None, None)."""
    articles = fetch_news(question)
    recent = [a for a in articles if a["age_min"] < 120]
    if not recent:
        return None, None
    best = min(recent, key=lambda a: a["age_min"])
    return best["title"], best["age_min"]


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

        news_headline, news_age = check_news(question)
        if news_headline:
            if news_age < 30:
                score_yes += 3
                reasons_yes.append(f"Breaking news ({news_age}min ago)")
            else:
                score_yes += 1
                reasons_yes.append(f"Recent news ({news_age}min ago)")

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
                "news": news_headline,
                "news_age": news_age,
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

            if news_headline:
                if news_age < 30:
                    score_no += 3
                    reasons_no.append(f"Breaking news ({news_age}min ago)")
                else:
                    score_no += 1
                    reasons_no.append(f"Recent news ({news_age}min ago)")

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
                    "news": news_headline,
                    "news_age": news_age,
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


# Pending bet waiting for user confirmation: { opportunity dict } or None
_pending_bet = None

CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))


def _format_signal(op):
    side = op["side"]
    price = op["yes_price"] if side == "YES" else round(1 - op["yes_price"], 4)
    drift = op.get("drift_short")
    drift_str = f"{drift:+.1f}%" if drift else "n/a"
    lines = [
        f"[{op['score']}pts] [{side}] {op['question'][:70]}",
        f"Price: {price:.3f} | Vol: ${op['volume']:,.0f} | Drift: {drift_str}",
        f"Consistency: {op['consistency']:.0%} | {', '.join(op['reasons'])}",
    ]
    if op.get("news"):
        lines.append(f"NEWS ({op['news_age']}min ago): {op['news'][:90]}")
    return "\n".join(lines)


async def check_exits_notify(context):
    for token_id, position in list(active_positions.items()):
        # Use latest price from price_history (Gamma API data)
        history = price_history.get(token_id, [])
        if not history:
            continue
        current_price = history[-1]["price"]
        entry = position["entry_price"]
        pnl_pct = (current_price - entry) / entry * 100
        label = position["question"][:50]

        if pnl_pct >= 40:
            place_sell(token_id, position["size"])
            del active_positions[token_id]
            await context.bot.send_message(
                CHAT_ID, f"TAKE PROFIT: {label}\n+{pnl_pct:.1f}%"
            )
        elif pnl_pct <= -30:
            place_sell(token_id, position["size"])
            del active_positions[token_id]
            await context.bot.send_message(
                CHAT_ID, f"STOP LOSS: {label}\n{pnl_pct:.1f}%"
            )


async def bot_cycle(context):
    global _pending_bet
    try:
        markets = get_markets()
        track_prices(markets)

        if active_positions:
            await check_exits_notify(context)

        opportunities = find_opportunities()
        if not opportunities:
            return

        best = opportunities[0]
        if best["score"] >= 4 and _pending_bet is None:
            _pending_bet = best
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Bet $2", callback_data="bet_yes"),
                InlineKeyboardButton("Skip", callback_data="bet_no"),
            ]])
            await context.bot.send_message(
                CHAT_ID,
                _format_signal(best),
                reply_markup=keyboard,
            )
    except Exception as e:
        await context.bot.send_message(CHAT_ID, f"Error: {e}")


async def button_handler(update, context):
    global _pending_bet
    query = update.callback_query
    await query.answer()

    if query.effective_chat.id != CHAT_ID:
        return

    if query.data == "bet_yes" and _pending_bet:
        op = _pending_bet
        side = op["side"]
        bet_price = op["yes_price"] if side == "YES" else round(1 - op["yes_price"], 4)
        try:
            place_bet(op["token_id"], bet_price)
            active_positions[op["token_id"]] = {
                "entry_price": bet_price,
                "size": round(2.0 / bet_price, 2),
                "question": op["question"],
            }
            await query.edit_message_text(
                f"Bet placed! [{side}] {op['question'][:60]}\nPrice: {bet_price:.3f}"
            )
        except Exception as e:
            await query.edit_message_text(f"Order failed: {e}")
        _pending_bet = None

    elif query.data == "bet_no":
        label = _pending_bet["question"][:60] if _pending_bet else "?"
        await query.edit_message_text(f"Skipped: {label}")
        _pending_bet = None


async def cmd_start(update, context):
    if update.effective_chat.id != CHAT_ID:
        return
    await update.message.reply_text(
        "Polymarket bot running. I'll ping you when I find a signal (score >= 4).\n"
        "Commands: /status"
    )


async def cmd_status(update, context):
    if update.effective_chat.id != CHAT_ID:
        return
    if not active_positions:
        await update.message.reply_text("No open positions.")
        return
    lines = [f"Open positions ({len(active_positions)}):"]
    for pos in active_positions.values():
        lines.append(f"  {pos['question'][:45]} @ {pos['entry_price']:.3f}")
    await update.message.reply_text("\n".join(lines))


if __name__ == "__main__":
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_TOKEN not set in .env")
    if not CHAT_ID:
        raise SystemExit("TELEGRAM_CHAT_ID not set in .env")

    print("Polymarket Telegram bot starting...")
    print("Building price history — first signals in ~5 minutes.\n")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.job_queue.run_repeating(bot_cycle, interval=60, first=10)
    app.run_polling()
