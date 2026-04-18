"""
=============================================================================
PAPER TRADING DASHBOARD
=============================================================================
Lean web dashboard that reads only from data/paper_trading/state.json.
NO exchange connectivity, NO keys needed. Safe by design.
Run: python paper_web.py  (then open http://localhost:8000)
=============================================================================
"""

import os
import sys
import csv
import json
import logging
from typing import Dict, List
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("paper_web")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "data", "paper_trading", "state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "data", "paper_trading", "paper_trader.log")
TEMPLATES_DIR = os.path.join(SCRIPT_DIR, "templates")

app = FastAPI(title="Paper Trading Dashboard")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def load_paper_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Failed to load state: {e}")
        return {}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("paper.html", {"request": request})


@app.get("/api/status")
async def api_status():
    state = load_paper_state()
    total_equity = sum(s.get("equity", 0) for s in state.values() if isinstance(s, dict))
    n_strategies = len(state)
    n_open = sum(1 for s in state.values()
                 if isinstance(s, dict)
                 and s.get("position", {}).get("side") in ("long", "short"))
    mtime = os.path.getmtime(STATE_FILE) if os.path.exists(STATE_FILE) else 0
    return {
        "status": "paper",
        "strategies": n_strategies,
        "open_positions": n_open,
        "total_equity": round(total_equity, 2),
        "last_update": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if mtime else "never",
    }


@app.get("/api/strategies")
async def api_strategies():
    state = load_paper_state()
    rows = []
    for name, s in state.items():
        if not isinstance(s, dict):
            continue
        pos = s.get("position", {})
        equity = s.get("equity", 0)
        peak = s.get("peak_equity", 0)
        dd = (1 - equity / peak) * 100 if peak > 0 else 0
        rows.append({
            "name": name,
            "equity": round(equity, 2),
            "ret_pct": round((equity - 1000) / 10, 2),
            "drawdown": round(dd, 2),
            "side": pos.get("side", "flat"),
            "entry_price": pos.get("entry_price", 0),
            "qty": pos.get("qty", 0),
            "pyramids": pos.get("pyr_count", 0),
        })
    rows.sort(key=lambda r: r["ret_pct"], reverse=True)
    return {"strategies": rows}


@app.get("/api/positions")
async def api_positions():
    state = load_paper_state()
    positions = []
    for name, s in state.items():
        if not isinstance(s, dict):
            continue
        pos = s.get("position", {})
        if pos.get("side") in ("long", "short"):
            positions.append({
                "strategy": name,
                "side": pos["side"],
                "entry": pos.get("entry_price", 0),
                "qty": pos.get("qty", 0),
                "equity_at_entry": pos.get("equity_at_entry", 0),
            })
    return {"positions": positions}


@app.get("/api/logs")
async def api_logs(lines: int = 50):
    if not os.path.exists(LOG_FILE):
        return {"lines": []}
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(max(0, os.path.getsize(LOG_FILE) - 50_000))
            tail = f.read().decode("utf-8", errors="ignore")
        all_lines = tail.split("\n")
        return {"lines": all_lines[-lines:]}
    except Exception as e:
        return {"lines": [f"error: {e}"]}


if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("  PAPER TRADING DASHBOARD")
    print("  Open: http://localhost:8000")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
