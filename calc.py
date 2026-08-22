"""
calc.py — currency and position-sizing calculators shared by the whole app.

Every symbol's target/stoploss/quantity can be configured independently:
  - ratio_mode: "1:1" (target == stoploss) or "custom" (set separately)
  - target_unit / stoploss_unit: "usd" (plain price points) or "inr" (rupees, converted via rate)
  - quantity_mode: "fixed" (exact contract count) or "from_rupees" (auto-sized so a stoploss hit
    loses approximately the rupee amount you specify)
"""

from dataclasses import dataclass


@dataclass
class SymbolConfig:
    broker: str            # "delta" or "upstox"
    symbol: str             # e.g. "BTCUSD", "XAUTUSD", "RELIANCE"
    enabled: bool = True
    timeframe: str = "1d"   # "1d" or "15m"
    ratio_mode: str = "1:1"  # "1:1" or "custom"
    target_unit: str = "usd"     # "usd" or "inr"
    target_value: float = 1000
    stoploss_unit: str = "usd"   # only used when ratio_mode == "custom"
    stoploss_value: float = 1000
    quantity_mode: str = "fixed"  # "fixed" or "from_rupees"
    quantity_value: float = 1     # used when quantity_mode == "fixed"
    rupee_risk_amount: float = 1000  # used when quantity_mode == "from_rupees"
    usd_inr_rate: float = 83.0


def usd_to_inr(usd: float, rate: float) -> float:
    return usd * rate


def inr_to_usd(inr: float, rate: float) -> float:
    return inr / rate if rate else 0.0


def resolve_target_stoploss_usd(cfg: SymbolConfig):
    """Returns (target_usd_move, stoploss_usd_move) — both as positive price-point distances."""
    rate = cfg.usd_inr_rate or 83.0
    target_usd = cfg.target_value if cfg.target_unit == "usd" else inr_to_usd(cfg.target_value, rate)
    if cfg.ratio_mode == "1:1":
        return target_usd, target_usd
    stoploss_usd = cfg.stoploss_value if cfg.stoploss_unit == "usd" else inr_to_usd(cfg.stoploss_value, rate)
    return target_usd, stoploss_usd


def resolve_quantity(cfg: SymbolConfig, stoploss_usd_move: float) -> float:
    """
    Returns the contract quantity to trade.
      - "fixed": exactly cfg.quantity_value
      - "from_rupees": sized so that a stoploss hit loses ~cfg.rupee_risk_amount,
        rounded to the nearest whole contract (min 1) since most exchanges require integer sizes.
    """
    if cfg.quantity_mode == "fixed":
        return cfg.quantity_value
    rate = cfg.usd_inr_rate or 83.0
    if stoploss_usd_move <= 0:
        return 1
    raw_qty = cfg.rupee_risk_amount / (stoploss_usd_move * rate)
    return max(1, round(raw_qty))


def preview(cfg: SymbolConfig) -> dict:
    """Human-readable summary of what this config actually resolves to, for the settings UI."""
    target_usd, stoploss_usd = resolve_target_stoploss_usd(cfg)
    qty = resolve_quantity(cfg, stoploss_usd)
    rate = cfg.usd_inr_rate or 83.0
    est_win_inr = target_usd * qty * rate
    est_loss_inr = stoploss_usd * qty * rate
    return {
        "target_usd_move": round(target_usd, 4),
        "stoploss_usd_move": round(stoploss_usd, 4),
        "quantity": qty,
        "est_profit_at_target_inr": round(est_win_inr, 2),
        "est_loss_at_stoploss_inr": round(est_loss_inr, 2),
    }
