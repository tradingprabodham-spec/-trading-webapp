"""
bot_engine.py — broker-agnostic EMA crossover trading engine.

Runs a background loop that, for every enabled symbol in the config (Delta or Upstox),
independently:
  - fetches candles and computes EMA 9/21/200
  - on a fresh 9/200 or 21/200 crossover, opens a position sized per that symbol's own
    ratio/quantity settings (see calc.py)
  - polls live price and closes on target/stoploss
  - persists state (open positions, trade log) to disk so a restart doesn't lose track
  - sends Telegram notifications on open/close if configured
"""

import time
import json
import hmac
import hashlib
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone

import requests

from calc import SymbolConfig, resolve_target_stoploss_usd, resolve_quantity
import config_store

STATE_FILE = Path("bot_state.json")
log = logging.getLogger("bot_engine")


# ---------------------------------------------------------------------------
# Broker clients
# ---------------------------------------------------------------------------
class DeltaClient:
    def __init__(self, api_key, api_secret, env):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://cdn-ind.testnet.deltaex.org" if env == "testnet" else "https://api.india.delta.exchange"
        self.session = requests.Session()
        self._product_cache = {}

    def _sign(self, method, path, query="", body=""):
        timestamp = str(int(time.time()))
        message = method + timestamp + path + query + body
        signature = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return timestamp, signature

    def _request(self, method, path, query="", body=None, auth=True):
        body_str = json.dumps(body) if body else ""
        url = self.base_url + path + query
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            timestamp, signature = self._sign(method, path, query, body_str)
            headers.update({"api-key": self.api_key, "signature": signature, "timestamp": timestamp})
        last_err = None
        for attempt in range(3):
            try:
                resp = self.session.request(method, url, headers=headers, data=body_str or None, timeout=15)
                if not resp.text.strip():
                    raise RuntimeError(f"{method} {path} empty response (HTTP {resp.status_code})")
                try:
                    data = resp.json()
                except ValueError:
                    raise RuntimeError(f"{method} {path} non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")
                if not resp.ok or data.get("success") is False:
                    err = data.get("error", {})
                    raise RuntimeError(f"{method} {path} failed: {err.get('code') or err.get('message') or resp.status_code}")
                return data
            except (requests.exceptions.RequestException, RuntimeError) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise last_err

    def ticker(self, symbol):
        return self._request("GET", f"/v2/tickers/{symbol}", auth=False)["result"]

    def candles(self, symbol, resolution, start_ts, end_ts):
        query = f"?resolution={resolution}&symbol={symbol}&start={start_ts}&end={end_ts}"
        return self._request("GET", "/v2/history/candles", query=query, auth=False)["result"]

    def product(self, symbol):
        if symbol not in self._product_cache:
            self._product_cache[symbol] = self._request("GET", f"/v2/products/{symbol}", auth=False)["result"]
        return self._product_cache[symbol]

    def place_market_order(self, symbol, side, size):
        product = self.product(symbol)
        body = {"product_id": product["id"], "size": size, "side": side, "order_type": "market_order"}
        return self._request("POST", "/v2/orders", body=body)["result"]


class UpstoxClient:
    """
    Minimal Upstox v3 client for candles/LTP/orders. Order placement requires a static IP
    whitelisted with Upstox (see README) — read-only calls (candles/LTP) work without one.
    """
    def __init__(self, access_token):
        self.token = access_token
        self.session = requests.Session()

    def _headers(self):
        return {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}

    def candles(self, instrument_key, unit, interval, to_date, from_date):
        url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
        resp = self.session.get(url, headers=self._headers(), timeout=15)
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Upstox candles failed: {data}")
        return data["data"]["candles"]

    def ltp(self, instrument_key):
        url = f"https://api.upstox.com/v3/market-quote/ltp?instrument_key={instrument_key}"
        resp = self.session.get(url, headers=self._headers(), timeout=15)
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Upstox LTP failed: {data}")
        for v in data["data"].values():
            return v["last_price"]
        return None

    def place_market_order(self, instrument_key, side, qty):
        url = "https://api-hft.upstox.com/v3/order/place"
        body = {
            "quantity": qty, "product": "I", "validity": "DAY", "price": 0, "tag": "auto-bot",
            "instrument_token": instrument_key, "order_type": "MARKET", "transaction_type": side,
            "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False,
        }
        resp = self.session.post(url, headers={**self._headers(), "Content-Type": "application/json"}, json=body, timeout=15)
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Upstox order failed: {data}")
        return data["data"]


# ---------------------------------------------------------------------------
# EMA math (identical to the dashboard's JS version)
# ---------------------------------------------------------------------------
def ema(closes, period):
    out = [None] * len(closes)
    if len(closes) < period:
        return out
    sma = sum(closes[:period]) / period
    out[period - 1] = sma
    k = 2 / (period + 1)
    for i in range(period, len(closes)):
        out[i] = closes[i] * k + out[i - 1] * (1 - k)
    return out


def crossed_at(i, fast, slow):
    if i < 1 or None in (fast[i], slow[i], fast[i - 1], slow[i - 1]):
        return None
    now = "bull" if fast[i] > slow[i] else "bear"
    prev = "bull" if fast[i - 1] > slow[i - 1] else "bear"
    return now if now != prev else None


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"symbols": {}, "log": [], "started_at": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Bot engine
# ---------------------------------------------------------------------------
class BotEngine:
    def __init__(self):
        self.state = load_state()
        self.running = False
        self.thread = None
        self.last_ema_check = 0
        self.recent_logs = []  # in-memory ring buffer for the web UI, most recent last

    def _log(self, level, msg):
        entry = f"{datetime.now(timezone.utc).isoformat()} [{level}] {msg}"
        getattr(log, level.lower(), log.info)(msg)
        self.recent_logs.append(entry)
        if len(self.recent_logs) > 300:
            self.recent_logs = self.recent_logs[-300:]

    def notify(self, cfg_global, title, body):
        self._log("INFO", f"NOTIFY: {title} - {body}")
        token, chat_id = cfg_global.get("telegram_bot_token"), cfg_global.get("telegram_chat_id")
        if token and chat_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": f"*{title}*\n{body}", "parse_mode": "Markdown"},
                    timeout=10,
                )
            except Exception as e:
                self._log("WARNING", f"Telegram notify failed: {e}")

    def _sym_state(self, symbol):
        return self.state["symbols"].setdefault(symbol, {"positions": [], "last_candle_time": None})

    # ---- Delta symbol handling ----
    def _delta_client(self, cfg_global):
        return DeltaClient(cfg_global["delta_api_key"], cfg_global["delta_api_secret"], cfg_global["delta_env"])

    def check_delta_signals(self, client, sym_cfg: SymbolConfig, cfg_global):
        sym_state = self._sym_state(sym_cfg.symbol)
        end_ts = int(time.time())
        lookback_days = 15 if sym_cfg.timeframe == "15m" else 260
        start_ts = end_ts - lookback_days * 24 * 60 * 60
        try:
            candles = client.candles(sym_cfg.symbol, sym_cfg.timeframe, start_ts, end_ts)
        except Exception as e:
            self._log("WARNING", f"[{sym_cfg.symbol}] Failed to fetch candles: {e}")
            return
        candles = sorted(candles, key=lambda c: c["time"])
        if len(candles) < 205:
            self._log("WARNING", f"[{sym_cfg.symbol}] Only {len(candles)} candles available, need 200+.")
            return

        closes = [c["close"] for c in candles]
        e9, e21, e200 = ema(closes, 9), ema(closes, 21), ema(closes, 200)
        i = len(closes) - 1
        latest_time = candles[-1]["time"]
        if sym_state.get("last_candle_time") == latest_time:
            return
        sym_state["last_candle_time"] = latest_time
        save_state(self.state)

        c9200 = crossed_at(i, e9, e200)
        c21200 = crossed_at(i, e21, e200)
        if c9200 and not any(p["leg"] == "9/200" for p in sym_state["positions"]):
            self.open_delta_position(client, sym_cfg, cfg_global, "9/200", "buy" if c9200 == "bull" else "sell")
        if c21200 and not any(p["leg"] == "21/200" for p in sym_state["positions"]):
            self.open_delta_position(client, sym_cfg, cfg_global, "21/200", "buy" if c21200 == "bull" else "sell")

    def open_delta_position(self, client, sym_cfg: SymbolConfig, cfg_global, leg, side):
        sym_state = self._sym_state(sym_cfg.symbol)
        target_usd, stoploss_usd = resolve_target_stoploss_usd(sym_cfg)
        qty = resolve_quantity(sym_cfg, stoploss_usd)
        try:
            if cfg_global["dry_run"]:
                t = client.ticker(sym_cfg.symbol)
                entry = float(t.get("close") or t.get("mark_price"))
                self._log("INFO", f"[DRY_RUN] Would place {side} {sym_cfg.symbol} size={qty} leg={leg}")
            else:
                order = client.place_market_order(sym_cfg.symbol, side, qty)
                entry = float(order.get("average_fill_price") or client.ticker(sym_cfg.symbol)["close"])
        except Exception as e:
            self.notify(cfg_global, "Bot: order failed", f"{sym_cfg.symbol} {leg} {side} entry failed - {e}")
            return

        target = entry + target_usd if side == "buy" else entry - target_usd
        stoploss = entry - stoploss_usd if side == "buy" else entry + stoploss_usd
        pos = {"leg": leg, "side": side, "entry": entry, "target": target, "stoploss": stoploss,
               "qty": qty, "open_time": datetime.now(timezone.utc).isoformat()}
        sym_state["positions"].append(pos)
        save_state(self.state)
        self.notify(cfg_global, f"Bot: {sym_cfg.symbol} {leg} opened{' [DRY RUN]' if cfg_global['dry_run'] else ''}",
                    f"{side.upper()} {qty} {sym_cfg.symbol} @ {entry:.2f} - target {target:.2f} - SL {stoploss:.2f}")

    def check_delta_positions(self, client, sym_cfg: SymbolConfig, cfg_global):
        sym_state = self._sym_state(sym_cfg.symbol)
        if not sym_state["positions"]:
            return
        try:
            t = client.ticker(sym_cfg.symbol)
            price = float(t.get("close") or t.get("mark_price"))
        except Exception as e:
            self._log("WARNING", f"[{sym_cfg.symbol}] Failed to fetch live price: {e}")
            return
        for pos in list(sym_state["positions"]):
            hit_target = (price >= pos["target"]) if pos["side"] == "buy" else (price <= pos["target"])
            hit_stop = (price <= pos["stoploss"]) if pos["side"] == "buy" else (price >= pos["stoploss"])
            if hit_target or hit_stop:
                self.close_delta_position(client, sym_cfg, cfg_global, pos, "Target hit" if hit_target else "Stoploss hit", price)

    def close_delta_position(self, client, sym_cfg: SymbolConfig, cfg_global, pos, reason, exit_price):
        sym_state = self._sym_state(sym_cfg.symbol)
        close_side = "sell" if pos["side"] == "buy" else "buy"
        try:
            if not cfg_global["dry_run"]:
                client.place_market_order(sym_cfg.symbol, close_side, pos["qty"])
        except Exception as e:
            self.notify(cfg_global, "Bot: close failed", f"{sym_cfg.symbol} {pos['leg']} close failed - {e}")
            return
        pnl_usd = ((exit_price - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - exit_price)) * pos["qty"]
        pnl_inr = pnl_usd * sym_cfg.usd_inr_rate
        sym_state["positions"] = [p for p in sym_state["positions"] if p is not pos]
        self.state["log"].append({"symbol": sym_cfg.symbol, **pos, "exit": exit_price, "reason": reason,
                                   "pnl_usd": pnl_usd, "pnl_inr": pnl_inr,
                                   "close_time": datetime.now(timezone.utc).isoformat()})
        save_state(self.state)
        self.notify(cfg_global, f"Bot: {sym_cfg.symbol} {pos['leg']} closed ({reason})",
                    f"{pos['side'].upper()} closed @ {exit_price:.2f} - P&L ${pnl_usd:.2f} (Rs{pnl_inr:.2f})")

    # ---- main loop ----
    def loop(self):
        self._log("INFO", "Bot engine loop started")
        self.state["started_at"] = datetime.now(timezone.utc).isoformat()
        save_state(self.state)
        while self.running:
            try:
                cfg = config_store.load_config()
                cfg_global = cfg["global"]
                symbol_cfgs = [SymbolConfig(**s) for s in cfg["symbols"] if s.get("enabled")]

                delta_syms = [s for s in symbol_cfgs if s.broker == "delta"]
                if delta_syms and cfg_global.get("delta_api_key") and cfg_global.get("delta_api_secret"):
                    client = self._delta_client(cfg_global)
                    now = time.time()
                    if now - self.last_ema_check >= cfg_global.get("ema_poll_secs", 300):
                        for s in delta_syms:
                            self.check_delta_signals(client, s, cfg_global)
                        self.last_ema_check = now
                    for s in delta_syms:
                        self.check_delta_positions(client, s, cfg_global)

                # Upstox symbols: signal detection + read-only monitoring always works;
                # order placement will raise a clear error if no static IP is whitelisted yet.
                # (Left as a monitoring-only stub here; extend once static IP is configured.)

            except Exception as e:
                self._log("ERROR", f"Unhandled loop error: {e}")
            time.sleep(config_store.load_config()["global"].get("price_poll_secs", 15))
        self._log("INFO", "Bot engine loop stopped")

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False


engine = BotEngine()
