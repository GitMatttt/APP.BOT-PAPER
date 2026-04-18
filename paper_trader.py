"""
=============================================================================
PAPER TRADER — Live Forward-Test of All Finalized Strategies
=============================================================================
Polls Hyperliquid 8H candles, runs all 7 strategies in paper mode,
logs every simulated trade to CSV, checks signals at each candle close.

Run continuously:  python paper_trader.py
One-shot check:    python paper_trader.py --once
30-day report:     python paper_trader.py --report

Deploy to Mac: copy to /Users/mb/Desktop/HypeBot/
Logs go to:    <script_dir>/data/paper_trading/

Strategies:
  ETH:    E20/100 CH3 LQ100 PYR8/100 BSIZ  (TV +291.3%, DD 34.1%)
  AAVE:   E10/50  CH3 LQ70  BSIZ ADX15      (TV +247.8%, DD 39.6%)
  AVAX:   E50/200 CH3 LQ60                   (TV +127.1%, DD 25.1%)
  ZEC:    E50/200 CH4 LQ100                  (TV +1012.6%, DD 36.9%)
  PENDLE: E50/200 CH3 LQ70                   (TV +143.6%, DD 20.5%)
  CRV:    E50/200 CH3 LQ100                  (TV +556.8%, DD 38.1%)
  HYPE:   E50/200 CH7 LQ100 BSIZ            (TV +112.2%, DD 27.7%)
=============================================================================
"""

import os
import sys
import csv
import json
import time
import math
import urllib.request
import urllib.error
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple

# =============================================================================
# PATHS
# =============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data", "paper_trading")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "paper_trader.log")

os.makedirs(DATA_DIR, exist_ok=True)

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("paper_trader")

# =============================================================================
# CONSTANTS
# =============================================================================
API_URL = "https://api.hyperliquid.xyz/info"
COMM = 0.00045  # 0.045% taker fee
INTERVAL_SEC = 28800  # 8 hours
INITIAL_CAPITAL = 1000.0

# Candle alignment: 8H candles close at 00:00, 08:00, 16:00 UTC
# Poll every 5 minutes after expected close, with 3-min buffer
POLL_INTERVAL = 300   # 5 minutes
CLOSE_BUFFER = 180    # 3 minutes after close before polling

# How many historical candles to fetch for indicator warm-up (EMA 200 needs ~250)
WARMUP_BARS = 300

# =============================================================================
# STRATEGY CONFIGS
# =============================================================================

@dataclass
class StrategyConfig:
    coin: str
    long_ema_fast: int
    long_ema_slow: int
    chandelier_mult: float
    long_qty_pct: int
    short_ema_fast: int = 50
    short_ema_slow: int = 200
    short_tp_pct: float = 35.0
    short_sl_pct: float = 2.0
    short_max_bars: int = 25
    short_qty_pct: int = 40
    has_bsiz: bool = False
    hi_vol_reduce: float = 0.6
    has_pyramid: bool = False
    pyr_threshold: float = 8.0
    pyr_size_pct: float = 100.0
    max_pyr: int = 2
    has_adx: bool = False
    adx_thresh: int = 15
    choppy_lq: int = 40
    choppy_stp: float = 15.0
    choppy_st: int = 15
    # Strategy type: "chandelier" (default) or "trail" (Moonshot/THE ALGO)
    strategy_type: str = "chandelier"
    trail_mult: float = 5.0       # for trail-stop strategies
    long_only: bool = False       # no short entries
    category: str = "finalized"   # finalized, challenger, experimental


# =============================================================================
# STRATEGY DEFINITIONS
# =============================================================================

# --- FINALIZED (TV-verified, production ready) ---
STRATEGIES = {
    "ETH": StrategyConfig(
        coin="ETH", long_ema_fast=20, long_ema_slow=100,
        chandelier_mult=3.0, long_qty_pct=100,
        short_tp_pct=35.0, short_sl_pct=2.0, short_max_bars=25, short_qty_pct=40,
        has_bsiz=True, has_pyramid=True, pyr_threshold=8.0, pyr_size_pct=100.0,
        category="finalized",
    ),
    "AAVE": StrategyConfig(
        coin="AAVE", long_ema_fast=10, long_ema_slow=50,
        chandelier_mult=3.0, long_qty_pct=70,
        short_tp_pct=40.0, short_sl_pct=3.0, short_max_bars=30, short_qty_pct=40,
        has_bsiz=True, has_adx=True, adx_thresh=15,
        choppy_lq=40, choppy_stp=15.0, choppy_st=15,
        category="finalized",
    ),
    "AVAX": StrategyConfig(
        coin="AVAX", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=20.0, short_sl_pct=5.0, short_max_bars=30, short_qty_pct=40,
        category="finalized",
    ),
    "ZEC": StrategyConfig(
        coin="ZEC", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=4.0, long_qty_pct=100,
        short_tp_pct=20.0, short_sl_pct=7.0, short_max_bars=15, short_qty_pct=40,
        category="finalized",
    ),
    "PENDLE": StrategyConfig(
        coin="PENDLE", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=70,
        short_tp_pct=40.0, short_sl_pct=3.0, short_max_bars=15, short_qty_pct=40,
        category="finalized",
    ),
    "CRV": StrategyConfig(
        coin="CRV", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=100,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=50, short_qty_pct=40,
        category="finalized",
    ),
    "HYPE": StrategyConfig(
        coin="HYPE", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=7.0, long_qty_pct=100,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=20, short_qty_pct=40,
        has_bsiz=True, category="finalized",
    ),

    # --- CHALLENGERS (not yet TV-verified or alternative strategies) ---
    "SOL": StrategyConfig(
        coin="SOL", long_ema_fast=10, long_ema_slow=50,
        chandelier_mult=4.0, long_qty_pct=100,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=20,
        category="challenger",
    ),
    "LINK": StrategyConfig(
        coin="LINK", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=7.0, long_qty_pct=60,
        short_tp_pct=20.0, short_sl_pct=3.0, short_max_bars=30, short_qty_pct=40,
        category="challenger",
    ),
    # Moonshot EMA 8/21 T5.0 L+S pyr=3 33% equity (live Signum strategy)
    "HYPE_Moonshot": StrategyConfig(
        coin="HYPE", long_ema_fast=8, long_ema_slow=21,
        chandelier_mult=5.0, long_qty_pct=33,
        short_ema_fast=8, short_ema_slow=21,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=20, short_qty_pct=33,
        has_pyramid=True, pyr_threshold=5.0, pyr_size_pct=100.0, max_pyr=3,
        strategy_type="trail", trail_mult=5.0,
        category="challenger",
    ),
    # THE ALGO NoShorts Trail3 TP2 (live Signum strategy)
    "HYPE_TheAlgo": StrategyConfig(
        coin="HYPE", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=100,
        long_only=True,
        category="challenger",
    ),

    # --- EXPERIMENTAL (ETH params on OOS coins) ---
    "OP_exp": StrategyConfig(
        coin="OP", long_ema_fast=20, long_ema_slow=100,
        chandelier_mult=3.0, long_qty_pct=100,
        short_tp_pct=35.0, short_sl_pct=2.0, short_max_bars=25, short_qty_pct=40,
        category="experimental",
    ),
    "ARB_exp": StrategyConfig(
        coin="ARB", long_ema_fast=20, long_ema_slow=100,
        chandelier_mult=3.0, long_qty_pct=100,
        short_tp_pct=35.0, short_sl_pct=2.0, short_max_bars=25, short_qty_pct=40,
        category="experimental",
    ),
    "INJ_exp": StrategyConfig(
        coin="INJ", long_ema_fast=20, long_ema_slow=100,
        chandelier_mult=3.0, long_qty_pct=100,
        short_tp_pct=35.0, short_sl_pct=2.0, short_max_bars=25, short_qty_pct=40,
        category="experimental",
    ),
    "TIA_exp": StrategyConfig(
        coin="TIA", long_ema_fast=20, long_ema_slow=100,
        chandelier_mult=3.0, long_qty_pct=100,
        short_tp_pct=35.0, short_sl_pct=2.0, short_max_bars=25, short_qty_pct=40,
        category="experimental",
    ),
    # AAVE params on OOS coins
    "LINK_aave": StrategyConfig(
        coin="LINK", long_ema_fast=10, long_ema_slow=50,
        chandelier_mult=3.0, long_qty_pct=70,
        short_tp_pct=40.0, short_sl_pct=3.0, short_max_bars=30, short_qty_pct=40,
        has_bsiz=True, category="experimental",
    ),
    "UNI_aave": StrategyConfig(
        coin="UNI", long_ema_fast=10, long_ema_slow=50,
        chandelier_mult=3.0, long_qty_pct=70,
        short_tp_pct=40.0, short_sl_pct=3.0, short_max_bars=30, short_qty_pct=40,
        has_bsiz=True, category="experimental",
    ),
    "CRV_aave": StrategyConfig(
        coin="CRV", long_ema_fast=10, long_ema_slow=50,
        chandelier_mult=3.0, long_qty_pct=70,
        short_tp_pct=40.0, short_sl_pct=3.0, short_max_bars=30, short_qty_pct=40,
        has_bsiz=True, category="experimental",
    ),

    # --- SHORT-ONLY (fixed exit, no chandelier) ---
    "ADA_Short": StrategyConfig(
        coin="ADA", long_ema_fast=12, long_ema_slow=26,
        chandelier_mult=99.0, long_qty_pct=0,
        short_ema_fast=12, short_ema_slow=26,
        short_tp_pct=15.0, short_sl_pct=5.0, short_max_bars=30, short_qty_pct=100,
        long_only=False, category="finalized",
    ),
    "MELANIA_Short": StrategyConfig(
        coin="MELANIA", long_ema_fast=20, long_ema_slow=50,
        chandelier_mult=99.0, long_qty_pct=0,
        short_ema_fast=20, short_ema_slow=50,
        short_tp_pct=5.0, short_sl_pct=20.0, short_max_bars=50, short_qty_pct=100,
        long_only=False, category="finalized",
    ),
    "BCH_Short": StrategyConfig(
        coin="BCH", long_ema_fast=10, long_ema_slow=50,
        chandelier_mult=99.0, long_qty_pct=0,
        short_ema_fast=10, short_ema_slow=50,
        short_tp_pct=20.0, short_sl_pct=7.0, short_max_bars=20, short_qty_pct=100,
        has_bsiz=True, long_only=False, category="finalized",
    ),

    # --- TOURNAMENT WAVE 2 (2026-04-01) — Top 10 new winners ---
    "kFLOKI": StrategyConfig(
        coin="kFLOKI", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),
    "kPEPE": StrategyConfig(
        coin="kPEPE", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),
    "AR": StrategyConfig(
        coin="AR", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),
    "kSHIB": StrategyConfig(
        coin="kSHIB", long_ema_fast=20, long_ema_slow=100,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),
    "OP": StrategyConfig(
        coin="OP", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),
    "FTM": StrategyConfig(
        coin="FTM", long_ema_fast=20, long_ema_slow=100,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),
    "CELO": StrategyConfig(
        coin="CELO", long_ema_fast=20, long_ema_slow=100,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),
    "ENS": StrategyConfig(
        coin="ENS", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),
    "WLD": StrategyConfig(
        coin="WLD", long_ema_fast=20, long_ema_slow=100,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),
    "XRP": StrategyConfig(
        coin="XRP", long_ema_fast=50, long_ema_slow=200,
        chandelier_mult=3.0, long_qty_pct=60,
        short_tp_pct=30.0, short_sl_pct=5.0, short_max_bars=25, short_qty_pct=40,
        category="finalized",
    ),

    # =========================================================================
    # AGGRESSIVE — backtest-verified 500%+/1000%+ winners (2021-onward data)
    # =========================================================================

    # --- 1000%+ CLUB (from extended 2021 history backtest) ---
    "SOL_moon": StrategyConfig(
        coin="SOL", long_ema_fast=8, long_ema_slow=21,
        chandelier_mult=2.5, long_qty_pct=100,
        short_ema_fast=8, short_ema_slow=21,
        short_tp_pct=15.0, short_sl_pct=5.0, short_max_bars=12, short_qty_pct=100,
        has_pyramid=True, pyr_threshold=5.0, pyr_size_pct=100.0, max_pyr=2,
        category="aggressive",
    ),
    "AVAX_moon": StrategyConfig(
        coin="AVAX", long_ema_fast=10, long_ema_slow=30,
        chandelier_mult=3.0, long_qty_pct=100,
        short_ema_fast=10, short_ema_slow=30,
        short_tp_pct=15.0, short_sl_pct=5.0, short_max_bars=12, short_qty_pct=100,
        has_pyramid=True, pyr_threshold=5.0, pyr_size_pct=100.0, max_pyr=2,
        category="aggressive",
    ),

    # --- 500%+ TIER ---
    "ETH_moon": StrategyConfig(
        coin="ETH", long_ema_fast=8, long_ema_slow=21,
        chandelier_mult=2.5, long_qty_pct=100,
        short_ema_fast=8, short_ema_slow=21,
        short_tp_pct=15.0, short_sl_pct=5.0, short_max_bars=12, short_qty_pct=100,
        has_pyramid=True, pyr_threshold=5.0, pyr_size_pct=100.0, max_pyr=2,
        category="aggressive",
    ),
    "NEAR_moon": StrategyConfig(
        coin="NEAR", long_ema_fast=10, long_ema_slow=30,
        chandelier_mult=3.0, long_qty_pct=100,
        short_ema_fast=10, short_ema_slow=30,
        short_tp_pct=15.0, short_sl_pct=5.0, short_max_bars=12, short_qty_pct=100,
        has_pyramid=True, pyr_threshold=5.0, pyr_size_pct=100.0, max_pyr=2,
        category="aggressive",
    ),
    "kPEPE_moon": StrategyConfig(
        coin="kPEPE", long_ema_fast=10, long_ema_slow=30,
        chandelier_mult=3.0, long_qty_pct=100,
        short_ema_fast=10, short_ema_slow=30,
        short_tp_pct=15.0, short_sl_pct=5.0, short_max_bars=12, short_qty_pct=100,
        has_pyramid=True, pyr_threshold=5.0, pyr_size_pct=100.0, max_pyr=2,
        category="aggressive",
    ),

    # --- 400%+ tier: DOGE (+442%), kBONK (+444%), HYPE (+401%) ---
    "DOGE_yolo": StrategyConfig(
        coin="DOGE", long_ema_fast=5, long_ema_slow=13,
        chandelier_mult=2.0, long_qty_pct=100,
        short_ema_fast=5, short_ema_slow=13,
        short_tp_pct=15.0, short_sl_pct=3.0, short_max_bars=10, short_qty_pct=100,
        has_pyramid=True, pyr_threshold=5.0, pyr_size_pct=100.0, max_pyr=3,
        category="aggressive",
    ),
    "kBONK_moon": StrategyConfig(
        coin="kBONK", long_ema_fast=5, long_ema_slow=13,
        chandelier_mult=2.0, long_qty_pct=100,
        short_ema_fast=5, short_ema_slow=13,
        short_tp_pct=20.0, short_sl_pct=5.0, short_max_bars=10, short_qty_pct=100,
        has_pyramid=True, pyr_threshold=5.0, pyr_size_pct=100.0, max_pyr=3,
        category="aggressive",
    ),
    "HYPE_moon": StrategyConfig(
        coin="HYPE", long_ema_fast=10, long_ema_slow=30,
        chandelier_mult=3.0, long_qty_pct=100,
        short_ema_fast=10, short_ema_slow=30,
        short_tp_pct=15.0, short_sl_pct=5.0, short_max_bars=12, short_qty_pct=100,
        has_pyramid=True, pyr_threshold=5.0, pyr_size_pct=100.0, max_pyr=2,
        category="aggressive",
    ),
}

# TV benchmark for 30-day report comparison
TV_BENCHMARKS = {
    "ETH":    {"tv_ret": 291.3, "tv_dd": 34.1, "tv_rd": 8.54},
    "AAVE":   {"tv_ret": 247.8, "tv_dd": 39.6, "tv_rd": 6.26},
    "AVAX":   {"tv_ret": 127.1, "tv_dd": 25.1, "tv_rd": 5.07},
    "ZEC":    {"tv_ret": 1012.6, "tv_dd": 36.9, "tv_rd": 27.43},
    "PENDLE": {"tv_ret": 143.6, "tv_dd": 20.5, "tv_rd": 7.00},
    "CRV":    {"tv_ret": 556.8, "tv_dd": 38.1, "tv_rd": 14.59},
    "HYPE":   {"tv_ret": 112.2, "tv_dd": 27.7, "tv_rd": 4.05},
    "SOL":    {"tv_ret": None, "tv_dd": None, "tv_rd": None},  # not TV-verified
    "LINK":   {"tv_ret": None, "tv_dd": None, "tv_rd": None},  # not TV-verified
    "HYPE_Moonshot": {"tv_ret": 7874.0, "tv_dd": 16.57, "tv_rd": 475.0},
    "HYPE_TheAlgo":  {"tv_ret": 175.0, "tv_dd": 27.7, "tv_rd": 6.32},
}

# Strategy display order for summary
DISPLAY_ORDER = [
    # Finalized
    "ETH", "AAVE", "AVAX", "ZEC", "PENDLE", "CRV", "HYPE",
    # Challengers
    "SOL", "LINK", "HYPE_Moonshot", "HYPE_TheAlgo",
    # Experimental
    "OP_exp", "ARB_exp", "INJ_exp", "TIA_exp",
    "LINK_aave", "UNI_aave", "CRV_aave",
]


# =============================================================================
# HYPERLIQUID API
# =============================================================================

def fetch_candles_api(coin: str, interval: str = "8h",
                      start_ts: float = None, end_ts: float = None) -> list:
    """Fetch OHLCV candles from Hyperliquid public API."""
    if start_ts is None:
        start_ts = time.time() - WARMUP_BARS * INTERVAL_SEC
    if end_ts is None:
        end_ts = time.time()

    start_ms = int(start_ts * 1000)
    end_ms = int(end_ts * 1000)

    all_raw = []
    cursor = start_ms
    while cursor < end_ms:
        window = min(cursor + 5000 * INTERVAL_SEC * 1000, end_ms)
        body = json.dumps({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": cursor, "endTime": window}
        }).encode()
        req = urllib.request.Request(API_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = json.loads(resp.read())
        except Exception as e:
            log.warning(f"API error for {coin}: {e}")
            break

        if not raw:
            break

        all_raw.extend(raw)
        last_t = max(int(r["t"]) for r in raw)
        cursor = last_t + INTERVAL_SEC * 1000
        if cursor < end_ms:
            time.sleep(0.15)

    # Deduplicate and sort
    seen = set()
    unique = []
    for r in all_raw:
        t = int(r["t"])
        if t not in seen:
            seen.add(t)
            unique.append(r)
    unique.sort(key=lambda r: int(r["t"]))

    candles = []
    for r in unique:
        candles.append({
            "ts": int(r["t"]) // 1000,
            "o": float(r["o"]),
            "h": float(r["h"]),
            "l": float(r["l"]),
            "c": float(r["c"]),
            "v": float(r["v"]),
        })
    return candles


# =============================================================================
# INDICATORS
# =============================================================================

def compute_ema(closes: List[float], period: int) -> List[float]:
    """Compute EMA series. Returns list same length as closes (NaN-padded)."""
    ema = [float('nan')] * len(closes)
    if len(closes) < period:
        return ema
    # SMA seed
    ema[period - 1] = sum(closes[:period]) / period
    k = 2.0 / (period + 1)
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


def compute_atr(highs, lows, closes, period=14):
    """Compute ATR using Wilder's smoothing (RMA)."""
    n = len(closes)
    tr = [0.0] * n
    atr = [float('nan')] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    # Seed with SMA
    if n > period:
        atr[period] = sum(tr[1:period + 1]) / period
        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def compute_sma(values: List[float], period: int) -> List[float]:
    """Simple moving average."""
    sma = [float('nan')] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if any(math.isnan(v) for v in window):
            continue
        sma[i] = sum(window) / period
    return sma


def compute_adx(highs, lows, closes, period=14):
    """Compute ADX using Wilder's smoothing."""
    n = len(closes)
    adx = [float('nan')] * n
    if n < period * 3:
        return adx

    # +DM, -DM
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0

    # True Range
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))

    # Wilder smoothing (RMA)
    def rma(data, p, start=1):
        s = [float('nan')] * n
        if start + p > n:
            return s
        s[start + p - 1] = sum(data[start:start + p]) / p
        for i in range(start + p, n):
            s[i] = (s[i - 1] * (p - 1) + data[i]) / p
        return s

    sm_plus = rma(plus_dm, period)
    sm_minus = rma(minus_dm, period)
    sm_tr = rma(tr, period)

    # DI
    plus_di = [float('nan')] * n
    minus_di = [float('nan')] * n
    dx = [float('nan')] * n
    for i in range(n):
        if math.isnan(sm_tr[i]) or sm_tr[i] == 0:
            continue
        plus_di[i] = 100.0 * sm_plus[i] / sm_tr[i]
        minus_di[i] = 100.0 * sm_minus[i] / sm_tr[i]
        denom = plus_di[i] + minus_di[i]
        if denom > 0:
            dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denom

    # ADX = RMA of DX
    # Find first valid DX
    first_valid = None
    for i in range(n):
        if not math.isnan(dx[i]):
            first_valid = i
            break
    if first_valid is None:
        return adx

    # Seed ADX
    seed_end = first_valid + period
    if seed_end > n:
        return adx
    valid_dx = [dx[i] for i in range(first_valid, seed_end) if not math.isnan(dx[i])]
    if len(valid_dx) < period:
        return adx
    adx[seed_end - 1] = sum(valid_dx[:period]) / period
    for i in range(seed_end, n):
        if math.isnan(dx[i]):
            continue
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return adx


# =============================================================================
# POSITION STATE
# =============================================================================

@dataclass
class Position:
    side: str = "flat"         # "long", "short", "flat"
    entry_price: float = 0.0
    qty: float = 0.0           # in asset units
    equity_at_entry: float = 0.0
    entry_bar: int = 0         # bar index at entry
    highest_since_entry: float = 0.0
    # Pyramid
    pyr_count: int = 0
    initial_entry_price: float = 0.0
    entry_size_mult: float = 1.0
    pyr_qty: float = 0.0       # additional pyramid qty
    pyr_avg_price: float = 0.0  # blended avg


@dataclass
class AssetState:
    equity: float = INITIAL_CAPITAL
    peak_equity: float = INITIAL_CAPITAL
    position: Position = field(default_factory=Position)
    bar_count: int = 0
    last_processed_ts: int = 0
    trades: list = field(default_factory=list)


# =============================================================================
# TRADE LOGGING
# =============================================================================

def trade_csv_path(coin: str) -> str:
    return os.path.join(DATA_DIR, f"{coin}_trades.csv")


def equity_csv_path(coin: str) -> str:
    return os.path.join(DATA_DIR, f"{coin}_equity.csv")


def log_trade(coin: str, trade: dict):
    """Append a trade to the asset's CSV."""
    path = trade_csv_path(coin)
    exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "timestamp", "datetime", "side", "action", "price", "qty",
            "pnl", "pnl_pct", "equity_after", "exit_reason"
        ])
        if not exists:
            w.writeheader()
        w.writerow(trade)


def log_equity(coin: str, ts: int, equity: float, pos_side: str):
    """Append equity snapshot."""
    path = equity_csv_path(coin)
    exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "datetime", "equity", "position"])
        if not exists:
            w.writeheader()
        w.writerow({
            "timestamp": ts,
            "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "equity": f"{equity:.2f}",
            "position": pos_side,
        })


# =============================================================================
# STRATEGY ENGINE (per bar)
# =============================================================================

def process_bar(cfg: StrategyConfig, state: AssetState,
                candles: list, bar_idx: int, strat_name: str = None) -> AssetState:
    """
    Process a single bar for the strategy. Candles is the full history up to
    bar_idx (inclusive). Returns updated state.
    """
    label = strat_name or cfg.coin
    c = candles[bar_idx]
    ts = c["ts"]
    o, h, l, cl = c["o"], c["h"], c["l"], c["c"]
    dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    # Build indicator arrays up to current bar
    closes = [candles[i]["c"] for i in range(bar_idx + 1)]
    highs = [candles[i]["h"] for i in range(bar_idx + 1)]
    lows = [candles[i]["l"] for i in range(bar_idx + 1)]
    n = len(closes)

    ema_fast = compute_ema(closes, cfg.long_ema_fast)
    ema_slow = compute_ema(closes, cfg.long_ema_slow)
    short_ema_fast = compute_ema(closes, cfg.short_ema_fast)
    short_ema_slow = compute_ema(closes, cfg.short_ema_slow)
    atr = compute_atr(highs, lows, closes, 14)

    # Current values
    ef = ema_fast[-1] if not math.isnan(ema_fast[-1]) else None
    es = ema_slow[-1] if not math.isnan(ema_slow[-1]) else None
    sef = short_ema_fast[-1] if not math.isnan(short_ema_fast[-1]) else None
    ses = short_ema_slow[-1] if not math.isnan(short_ema_slow[-1]) else None
    atr_val = atr[-1] if not math.isnan(atr[-1]) else None

    if ef is None or es is None or atr_val is None:
        state.bar_count += 1
        return state

    # Previous values for crossover detection
    ef_prev = ema_fast[-2] if n >= 2 and not math.isnan(ema_fast[-2]) else ef
    es_prev = ema_slow[-2] if n >= 2 and not math.isnan(ema_slow[-2]) else es
    sef_prev = short_ema_fast[-2] if n >= 2 and not math.isnan(short_ema_fast[-2]) else sef
    ses_prev = short_ema_slow[-2] if n >= 2 and not math.isnan(short_ema_slow[-2]) else ses

    golden_cross = ef_prev <= es_prev and ef > es
    death_cross = sef_prev >= ses_prev and sef < ses if sef and ses else False
    golden_cross_s = sef_prev <= ses_prev and sef > ses if sef and ses else False
    in_downtrend = sef < ses if sef and ses else False
    short_pullback = in_downtrend and h > sef and cl < sef if sef else False

    # Binary ATR sizing
    size_mult = 1.0
    if cfg.has_bsiz:
        atr_sma = compute_sma([a if not math.isnan(a) else 0 for a in atr], 20)
        if len(atr_sma) > 0 and not math.isnan(atr_sma[-1]):
            if atr_val > atr_sma[-1]:
                size_mult = cfg.hi_vol_reduce

    # ADX regime
    active_lq = cfg.long_qty_pct
    active_stp = cfg.short_tp_pct
    active_st = cfg.short_max_bars
    if cfg.has_adx:
        adx_vals = compute_adx(highs, lows, closes, 14)
        adx_now = adx_vals[-1] if not math.isnan(adx_vals[-1]) else 50.0
        if adx_now < cfg.adx_thresh:
            active_lq = cfg.choppy_lq
            active_stp = cfg.choppy_stp
            active_st = cfg.choppy_st

    pos = state.position

    # --- POSITION MANAGEMENT ---

    if pos.side == "long":
        # Update highest
        pos.highest_since_entry = max(pos.highest_since_entry, h)
        chandelier_stop = pos.highest_since_entry - atr_val * cfg.chandelier_mult

        # Pyramid check
        if cfg.has_pyramid and pos.pyr_count < cfg.max_pyr and pos.initial_entry_price > 0:
            pyr_target = pos.initial_entry_price * (1 + cfg.pyr_threshold / 100 * pos.pyr_count)
            if h >= pyr_target:
                pyr_qty = state.equity * cfg.long_qty_pct / 100 * cfg.pyr_size_pct / 100 * pos.entry_size_mult / cl
                comm = pyr_qty * cl * COMM
                total_qty = pos.qty + pyr_qty
                pos.entry_price = (pos.entry_price * pos.qty + cl * pyr_qty) / total_qty
                pos.qty = total_qty
                pos.pyr_count += 1
                state.equity -= comm
                log.info(f"  {label} PYRAMID #{pos.pyr_count} @ {cl:.4f}, qty +{pyr_qty:.4f}")
                log_trade(label, {
                    "timestamp": ts, "datetime": dt_str,
                    "side": "long", "action": "pyramid", "price": f"{cl:.6f}",
                    "qty": f"{pyr_qty:.6f}", "pnl": f"{-comm:.2f}", "pnl_pct": "0.00",
                    "equity_after": f"{state.equity:.2f}", "exit_reason": "pyramid_add",
                })

        # Chandelier exit
        if cl < chandelier_stop:
            pnl = (cl - pos.entry_price) * pos.qty
            comm = pos.qty * cl * COMM
            pnl_net = pnl - comm
            pnl_pct = pnl_net / pos.equity_at_entry * 100 if pos.equity_at_entry > 0 else 0
            state.equity += pnl_net
            log.info(f"  {label} CLOSE LONG @ {cl:.4f} (chandelier), PnL ${pnl_net:+.2f} ({pnl_pct:+.1f}%)")
            log_trade(label, {
                "timestamp": ts, "datetime": dt_str,
                "side": "long", "action": "close", "price": f"{cl:.6f}",
                "qty": f"{pos.qty:.6f}", "pnl": f"{pnl_net:.2f}", "pnl_pct": f"{pnl_pct:.2f}",
                "equity_after": f"{state.equity:.2f}", "exit_reason": "chandelier",
            })
            state.position = Position()

    elif pos.side == "short":
        bars_held = state.bar_count - pos.entry_bar

        # TP/SL
        tp_price = pos.entry_price * (1 - active_stp / 100)
        sl_price = pos.entry_price * (1 + cfg.short_sl_pct / 100)
        exit_reason = None
        exit_price = cl

        if l <= tp_price:
            exit_price = tp_price
            exit_reason = "short_tp"
        elif h >= sl_price:
            exit_price = sl_price
            exit_reason = "short_sl"
        elif golden_cross_s:
            exit_reason = "short_golden_cross"
        elif bars_held >= active_st:
            exit_reason = "short_time_stop"

        if exit_reason:
            pnl = (pos.entry_price - exit_price) * pos.qty
            comm = pos.qty * exit_price * COMM
            pnl_net = pnl - comm
            pnl_pct = pnl_net / pos.equity_at_entry * 100 if pos.equity_at_entry > 0 else 0
            state.equity += pnl_net
            log.info(f"  {label} CLOSE SHORT @ {exit_price:.4f} ({exit_reason}), PnL ${pnl_net:+.2f} ({pnl_pct:+.1f}%)")
            log_trade(label, {
                "timestamp": ts, "datetime": dt_str,
                "side": "short", "action": "close", "price": f"{exit_price:.6f}",
                "qty": f"{pos.qty:.6f}", "pnl": f"{pnl_net:.2f}", "pnl_pct": f"{pnl_pct:.2f}",
                "equity_after": f"{state.equity:.2f}", "exit_reason": exit_reason,
            })
            state.position = Position()

    # --- NEW ENTRIES (only if flat) ---
    pos = state.position  # refresh after potential close
    if pos.side == "flat" and state.equity > 0:
        if golden_cross:
            # Long entry
            qty_pct = active_lq
            qty = state.equity * qty_pct / 100 * size_mult / cl
            comm = qty * cl * COMM
            state.equity -= comm
            state.position = Position(
                side="long", entry_price=cl, qty=qty,
                equity_at_entry=state.equity, entry_bar=state.bar_count,
                highest_since_entry=h,
                pyr_count=1 if cfg.has_pyramid else 0,
                initial_entry_price=cl if cfg.has_pyramid else 0,
                entry_size_mult=size_mult,
            )
            log.info(f"  {label} ENTER LONG @ {cl:.4f}, qty {qty:.4f}")
            log_trade(label, {
                "timestamp": ts, "datetime": dt_str,
                "side": "long", "action": "open", "price": f"{cl:.6f}",
                "qty": f"{qty:.6f}", "pnl": f"{-comm:.2f}", "pnl_pct": "0.00",
                "equity_after": f"{state.equity:.2f}", "exit_reason": "",
            })
        elif not cfg.long_only and (death_cross or short_pullback):
            # Short entry (skip if long_only)
            qty = state.equity * cfg.short_qty_pct / 100 * size_mult / cl
            comm = qty * cl * COMM
            state.equity -= comm
            reason = "death_cross" if death_cross else "pullback"
            state.position = Position(
                side="short", entry_price=cl, qty=qty,
                equity_at_entry=state.equity, entry_bar=state.bar_count,
            )
            log.info(f"  {label} ENTER SHORT @ {cl:.4f} ({reason}), qty {qty:.4f}")
            log_trade(label, {
                "timestamp": ts, "datetime": dt_str,
                "side": "short", "action": "open", "price": f"{cl:.6f}",
                "qty": f"{qty:.6f}", "pnl": f"{-comm:.2f}", "pnl_pct": "0.00",
                "equity_after": f"{state.equity:.2f}", "exit_reason": "",
            })

    # Track peak equity for DD
    state.peak_equity = max(state.peak_equity, state.equity)
    state.bar_count += 1
    state.last_processed_ts = ts

    # Log equity every bar
    log_equity(label, ts, state.equity, state.position.side)

    return state


# =============================================================================
# STATE PERSISTENCE
# =============================================================================

def save_state(states: Dict[str, AssetState]):
    """Save all asset states to JSON."""
    data = {}
    for coin, st in states.items():
        data[coin] = {
            "equity": st.equity,
            "peak_equity": st.peak_equity,
            "bar_count": st.bar_count,
            "last_processed_ts": st.last_processed_ts,
            "position": {
                "side": st.position.side,
                "entry_price": st.position.entry_price,
                "qty": st.position.qty,
                "equity_at_entry": st.position.equity_at_entry,
                "entry_bar": st.position.entry_bar,
                "highest_since_entry": st.position.highest_since_entry,
                "pyr_count": st.position.pyr_count,
                "initial_entry_price": st.position.initial_entry_price,
                "entry_size_mult": st.position.entry_size_mult,
            },
        }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_state() -> Dict[str, AssetState]:
    """Load asset states from JSON, or initialize fresh."""
    states = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        for coin, d in data.items():
            p = d.get("position", {})
            states[coin] = AssetState(
                equity=d["equity"],
                peak_equity=d["peak_equity"],
                bar_count=d["bar_count"],
                last_processed_ts=d["last_processed_ts"],
                position=Position(
                    side=p.get("side", "flat"),
                    entry_price=p.get("entry_price", 0),
                    qty=p.get("qty", 0),
                    equity_at_entry=p.get("equity_at_entry", 0),
                    entry_bar=p.get("entry_bar", 0),
                    highest_since_entry=p.get("highest_since_entry", 0),
                    pyr_count=p.get("pyr_count", 0),
                    initial_entry_price=p.get("initial_entry_price", 0),
                    entry_size_mult=p.get("entry_size_mult", 1.0),
                ),
            )
    # Initialize missing coins
    for coin in STRATEGIES:
        if coin not in states:
            states[coin] = AssetState()
    return states


# =============================================================================
# MAIN LOOP
# =============================================================================

def next_8h_close() -> float:
    """Return timestamp of next 8H candle close (00:00, 08:00, 16:00 UTC)."""
    now = time.time()
    # 8H boundaries: 0, 28800, 57600 seconds into the day
    day_start = int(now) - (int(now) % 86400)
    boundaries = [day_start + i * 28800 for i in range(4)]  # 0, 8h, 16h, 24h
    for b in boundaries:
        if b > now:
            return float(b)
    return float(day_start + 86400)  # next day 00:00


def run_once(states: Dict[str, AssetState]) -> Dict[str, AssetState]:
    """Process all new bars for all strategies. Called on each poll cycle."""
    # Cache candles per coin to avoid re-fetching
    candle_cache = {}

    for name, cfg in STRATEGIES.items():
        st = states[name]
        coin = cfg.coin

        # Fetch candles (cached per coin)
        if coin not in candle_cache:
            log.info(f"[{coin}] Fetching candles...")
            candles = fetch_candles_api(coin, "8h")
            if not candles:
                log.warning(f"[{coin}] No candles returned")
                candle_cache[coin] = None
            else:
                log.info(f"[{coin}] {len(candles)} candles, last ts {candles[-1]['ts']} "
                         f"({datetime.fromtimestamp(candles[-1]['ts'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')})")
                candle_cache[coin] = candles

        candles = candle_cache.get(coin)
        if not candles:
            continue

        # Find new bars to process
        if st.last_processed_ts == 0:
            start_idx = len(candles) - 1
            log.info(f"[{name}] First run, starting from bar {start_idx}")
        else:
            start_idx = None
            for i, c in enumerate(candles):
                if c["ts"] > st.last_processed_ts:
                    start_idx = i
                    break
            if start_idx is None:
                log.info(f"[{name}] No new bars since {st.last_processed_ts}")
                continue

        # Process each new bar (use name for logging/CSV)
        for i in range(start_idx, len(candles)):
            process_bar(cfg, st, candles, i, strat_name=name)

        log.info(f"[{name}] Equity: ${st.equity:.2f} | Pos: {st.position.side} | "
                 f"DD: {(1 - st.equity / st.peak_equity) * 100:.1f}%")

    save_state(states)
    return states


def run_continuous():
    """Main loop: poll every 8H candle close."""
    log.info("=" * 60)
    log.info("PAPER TRADER STARTING — 7 assets, $1000 each")
    log.info("=" * 60)

    states = load_state()

    # Initial run
    states = run_once(states)
    print_summary(states)

    while True:
        next_close = next_8h_close()
        wait_until = next_close + CLOSE_BUFFER
        now = time.time()
        sleep_secs = max(0, wait_until - now)

        next_dt = datetime.fromtimestamp(next_close, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log.info(f"Next 8H close: {next_dt} — sleeping {sleep_secs / 60:.0f} min")

        time.sleep(sleep_secs)

        log.info(f"--- Candle close at {next_dt} ---")
        states = run_once(states)
        print_summary(states)


def print_summary(states: Dict[str, AssetState]):
    """Print current portfolio status."""
    total_equity = 0
    total_initial = 0
    print("\n" + "=" * 80)
    print(f"{'Strategy':<16} {'Equity':>10} {'Ret%':>8} {'DD%':>6} {'Pos':>6} {'Trades':>7} {'Cat':>5}")
    print("-" * 80)
    last_cat = None
    for name in DISPLAY_ORDER:
        if name not in states:
            continue
        cfg = STRATEGIES[name]
        if cfg.category != last_cat:
            if last_cat is not None:
                print("-" * 80)
            cat_label = {"finalized": "FINALIZED", "challenger": "CHALLENGER",
                         "experimental": "EXPERIMENTAL"}.get(cfg.category, "")
            print(f"  --- {cat_label} ---")
            last_cat = cfg.category
        st = states[name]
        ret = (st.equity / INITIAL_CAPITAL - 1) * 100
        dd = (1 - st.equity / st.peak_equity) * 100 if st.peak_equity > 0 else 0
        path = trade_csv_path(name)
        n_trades = 0
        if os.path.exists(path):
            with open(path) as f:
                n_trades = sum(1 for row in csv.reader(f)) - 1
        total_equity += st.equity
        total_initial += INITIAL_CAPITAL
        cat_short = {"finalized": "FIN", "challenger": "CHL", "experimental": "EXP"}
        print(f"{name:<16} ${st.equity:>9.2f} {ret:>+7.1f}% {dd:>5.1f}% {st.position.side:>6} "
              f"{n_trades:>7} {cat_short.get(cfg.category, ''):>5}")
    print("-" * 80)
    total_ret = (total_equity / total_initial - 1) * 100 if total_initial > 0 else 0
    print(f"{'TOTAL':<16} ${total_equity:>9.2f} {total_ret:>+7.1f}%  ({len(STRATEGIES)} strategies)")
    print("=" * 80 + "\n")


# =============================================================================
# 30-DAY REPORT
# =============================================================================

def generate_report():
    """Generate 30-day performance report comparing paper vs TV backtest."""
    states = load_state()
    report_path = os.path.join(DATA_DIR, "30day_report.txt")

    lines = []
    lines.append("=" * 80)
    lines.append("30-DAY PAPER TRADING REPORT")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 80)
    lines.append("")

    # Calculate days running
    first_ts = None
    last_ts = None
    for coin, st in states.items():
        path = equity_csv_path(coin)
        if os.path.exists(path):
            with open(path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                if rows:
                    t0 = int(rows[0]["timestamp"])
                    t1 = int(rows[-1]["timestamp"])
                    if first_ts is None or t0 < first_ts:
                        first_ts = t0
                    if last_ts is None or t1 > last_ts:
                        last_ts = t1

    if first_ts and last_ts:
        days = (last_ts - first_ts) / 86400
        lines.append(f"Period: {days:.1f} days")
        lines.append(f"Start: {datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime('%Y-%m-%d')}")
        lines.append(f"End:   {datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime('%Y-%m-%d')}")
    else:
        days = 0
        lines.append("Period: No data yet")

    lines.append("")
    lines.append(f"{'Strategy':<16} {'Paper Ret':>10} {'Paper DD':>9} {'Paper R/D':>10} | "
                 f"{'TV Ret':>8} {'TV DD':>6} {'TV R/D':>7} | {'Tracking':>10} {'Cat':>5}")
    lines.append("-" * 100)

    total_paper = 0
    total_initial = 0
    verdicts = []

    for name in DISPLAY_ORDER:
        st = states.get(name)
        if not st:
            continue
        total_initial += INITIAL_CAPITAL

        paper_ret = (st.equity / INITIAL_CAPITAL - 1) * 100
        paper_dd = (1 - st.equity / st.peak_equity) * 100 if st.peak_equity > 0 else 0

        # Calculate max DD from equity curve
        path = equity_csv_path(name)
        max_dd = 0
        if os.path.exists(path):
            peak = 0
            with open(path) as f:
                for row in csv.DictReader(f):
                    eq = float(row["equity"])
                    peak = max(peak, eq)
                    dd = (1 - eq / peak) * 100 if peak > 0 else 0
                    max_dd = max(max_dd, dd)

        paper_rd = paper_ret / max_dd if max_dd > 0 else 0

        tv = TV_BENCHMARKS.get(name, {})
        tv_ret = tv.get("tv_ret") or 0
        tv_dd = tv.get("tv_dd") or 0
        tv_rd = tv.get("tv_rd") or 0
        cfg = STRATEGIES.get(name)

        # Annualize paper return for comparison
        # TV backtests cover ~14 months (Jan 2024 - Mar 2025)
        # Normalize paper return to same period length for fair comparison
        if days > 0:
            ann_paper_ret = paper_ret * (365 / days) if days < 365 else paper_ret
        else:
            ann_paper_ret = 0

        # Verdict: is paper tracking TV expectations?
        # Generous threshold: paper annualized should be within 50% of TV
        if days < 7:
            verdict = "TOO EARLY"
        elif max_dd > tv_dd * 1.5:
            verdict = "HIGH DD !!"
        elif ann_paper_ret < 0 and tv_ret > 0:
            verdict = "LOSING !!"
        elif days >= 30 and ann_paper_ret > 0 and tv_ret > 0:
            ratio = ann_paper_ret / tv_ret
            if ratio > 0.5:
                verdict = "ON TRACK"
            elif ratio > 0.25:
                verdict = "LAGGING"
            else:
                verdict = "UNDERPERF"
        else:
            verdict = "WATCHING"

        verdicts.append((name, verdict))
        total_paper += st.equity
        cat = cfg.category if cfg else "?"

        lines.append(f"{name:<16} {paper_ret:>+9.1f}% {max_dd:>8.1f}% {paper_rd:>9.2f} | "
                     f"{tv_ret:>+7.1f}% {tv_dd:>5.1f}% {tv_rd:>6.2f} | {verdict:>10} [{cat[:3].upper()}]")

    lines.append("-" * 90)
    total_ret = (total_paper / total_initial - 1) * 100 if total_initial > 0 else 0
    lines.append(f"{'TOTAL':<8} {total_ret:>+9.1f}%")
    lines.append("")

    # Verdicts summary
    lines.append("VERDICTS:")
    for coin, v in verdicts:
        emoji = {"ON TRACK": "OK", "LAGGING": "??", "UNDERPERF": "!!", "HIGH DD !!": "XX",
                 "LOSING !!": "XX", "WATCHING": "..", "TOO EARLY": "--"}.get(v, "??")
        lines.append(f"  [{emoji}] {coin}: {v}")

    lines.append("")
    lines.append("Legend: ON TRACK = annualized return > 50% of TV backtest")
    lines.append("        LAGGING  = 25-50% of TV    UNDERPERF = <25% of TV")
    lines.append("        HIGH DD  = paper DD > 1.5x TV DD")

    report = "\n".join(lines)
    with open(report_path, "w") as f:
        f.write(report)

    print(report)
    log.info(f"Report saved to {report_path}")
    return report_path


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    if "--report" in sys.argv:
        generate_report()
    elif "--once" in sys.argv:
        states = load_state()
        states = run_once(states)
        print_summary(states)
    elif "--status" in sys.argv:
        states = load_state()
        print_summary(states)
    else:
        run_continuous()
