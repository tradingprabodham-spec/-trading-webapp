"""
config_store.py — thread-safe JSON-file-backed settings storage.

Everything the person configures in the web UI (symbols, ratios, quantities, credentials,
DRY_RUN/live mode) is persisted here so it survives restarts.
"""

import json
import threading
from pathlib import Path

CONFIG_FILE = Path("app_config.json")
_lock = threading.RLock()  # reentrant — load_config() calls save_config() while already holding it

DEFAULT_CONFIG = {
    "global": {
        "dry_run": True,
        "delta_env": "testnet",       # "testnet" or "live"
        "delta_api_key": "",
        "delta_api_secret": "",
        "upstox_access_token": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "price_poll_secs": 15,
        "ema_poll_secs": 300,
    },
    "symbols": [
        {
            "broker": "delta", "symbol": "BTCUSD", "enabled": True, "timeframe": "1d",
            "ratio_mode": "1:1", "target_unit": "usd", "target_value": 1000,
            "stoploss_unit": "usd", "stoploss_value": 1000,
            "quantity_mode": "fixed", "quantity_value": 1,
            "rupee_risk_amount": 1000, "usd_inr_rate": 83.0,
        },
        {
            "broker": "delta", "symbol": "XAUTUSD", "enabled": True, "timeframe": "1d",
            "ratio_mode": "1:1", "target_unit": "inr", "target_value": 1000,
            "stoploss_unit": "inr", "stoploss_value": 1000,
            "quantity_mode": "fixed", "quantity_value": 1,
            "rupee_risk_amount": 1000, "usd_inr_rate": 83.0,
        },
    ],
}


def load_config() -> dict:
    with _lock:
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text())
            except Exception:
                pass
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict):
    with _lock:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
