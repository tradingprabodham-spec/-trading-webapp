"""
app.py — web server for the combined Delta + Upstox EMA auto-trading system.

Run locally:   python app.py
Deploy:        same as before (Railway etc.) — start command: python app.py
The web UI (single page) lets you configure everything (symbols, ratios, quantities,
credentials, DRY_RUN/live) and shows live bot status. All settings persist to app_config.json.
"""

import os
import logging
from flask import Flask, request, jsonify, render_template

import config_store
from calc import SymbolConfig, preview
from bot_engine import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/screener")
def screener():
    return render_template("screener.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(config_store.load_config())


@app.route("/api/config", methods=["POST"])
def set_config():
    cfg = request.get_json(force=True)
    config_store.save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/preview", methods=["POST"])
def preview_symbol():
    """Given one symbol's settings, return the resolved target/SL/quantity/est P&L."""
    data = request.get_json(force=True)
    cfg = SymbolConfig(**data)
    return jsonify(preview(cfg))


@app.route("/api/status")
def status():
    return jsonify({
        "running": engine.running,
        "state": engine.state,
        "logs": engine.recent_logs[-100:],
    })


@app.route("/api/start", methods=["POST"])
def start_bot():
    engine.start()
    return jsonify({"ok": True, "running": engine.running})


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    engine.stop()
    return jsonify({"ok": True, "running": engine.running})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
