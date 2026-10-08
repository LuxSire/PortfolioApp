"""styles.py -- Defensive vs Growth style tilt (explicit instruction).

Each theme in data/theme_taxonomy.json may carry "style": "defensive" | "growth"
(every other theme is neutral). A ticker's style is the style of its FIRST styled
theme in data/ticker_themes.json (themes are listed most relevant first), so WMT
(consumer_retail, consumer_staples) is defensive; a ticker with no styled theme is
neutral.

Style tilt = net growth weight - net defensive weight, where net = longs - shorts.
Long growth / short defensive pushes it positive, long defensive / short growth
negative. |tilt| may not exceed STYLE_TILT_LIMIT -- used by the trading robot
(weights as % of NetLiquidation), the backtest and the target-portfolio optimizer
(each leg 100% equal-weight, like their sector limits).
"""

import json
import os

STYLE_TILT_LIMIT = 0.20
TAXONOMY_FILE = os.path.join("data", "theme_taxonomy.json")
TICKER_THEMES_FILE = os.path.join("data", "ticker_themes.json")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def load_ticker_styles():
    """{ticker: "growth" | "defensive"} for every ticker with a styled theme."""
    theme_style = {t["key"]: t["style"] for t in _load(TAXONOMY_FILE, []) if t.get("style")}
    out = {}
    for ticker, themes in _load(TICKER_THEMES_FILE, {}).items():
        for th in themes or []:
            if th in theme_style:
                out[ticker] = theme_style[th]
                break
    return out


def style_sign(style):
    """Direction a LONG position in this style moves the tilt (+1 growth, -1 defensive, 0 neutral)."""
    return {"growth": 1.0, "defensive": -1.0}.get(style, 0.0)


def tilt(weights, styles):
    """weights: {ticker: signed weight}; returns net growth - net defensive."""
    return sum(w * style_sign(styles.get(t)) for t, w in weights.items())


def leg_tilt(long_tickers, short_tickers, styles):
    """Tilt of two equal-weight legs (each leg 100% gross)."""
    t = 0.0
    if long_tickers:
        t += sum(style_sign(styles.get(x)) for x in long_tickers) / len(long_tickers)
    if short_tickers:
        t -= sum(style_sign(styles.get(x)) for x in short_tickers) / len(short_tickers)
    return t
