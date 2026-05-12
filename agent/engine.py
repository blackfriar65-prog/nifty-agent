"""
Nifty Iron Condor AI Agent — Autonomous Engine
Paper trading mode. Runs every trading day 09:00–15:30 IST.
No manual intervention required.
"""

import json
import math
import time
import random
import threading
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/agent.log"),
    ],
)
log = logging.getLogger("agent")

STATE_FILE      = Path("logs/state.json")
LOG_JSONL       = Path("logs/events.jsonl")
RECAL_FLAG_FILE = Path("logs/cmd_recalibrate.flag")  # written by server on manual trigger

IST_OFFSET = 5.5 * 3600  # seconds ahead of UTC

# ── helpers ──────────────────────────────────────────────────────────────────

def now_ist() -> datetime:
    return datetime.utcnow() + timedelta(seconds=IST_OFFSET)

def ist_hm() -> tuple:
    t = now_ist()
    return t.hour, t.minute

def is_market_day() -> bool:
    """Mon–Fri only. No holiday list for demo."""
    return now_ist().weekday() < 5

def emit(event: str, data: dict):
    row = {"ts": now_ist().isoformat(), "event": event, **data}
    with open(LOG_JSONL, "a") as f:
        f.write(json.dumps(row) + "\n")
    log.info(f"[{event}] {data}")

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2))

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return default_state()

def default_state() -> dict:
    return {
        "mode": "paper",
        "phase": "pre_market",  # pre_market | scanning | credit_check_wait | entry_wait | active | exited
        "expiry": "27-May-2026",
        "dte": 16,
        "spot": 24280.0,
        "gift_nifty": 24280.0,
        "vix": 16.84,
        "crude_brent": 110.0,
        "verdict": "PENDING",          # PROCEED | SKIP | STOP
        "skip_reason": "",
        "strikes": {
            "put_short": 23000, "put_long": 22700,
            "call_short": 25500, "call_long": 25800
        },
        "credits": {
            "put_leg": 0.0, "call_leg": 0.0, "total": 0.0,
            "target_min": 5000
        },
        "position": {
            "active": False,
            "entry_time": "",
            "entry_spot": 0.0,
            "lot_size": 50,
            "put_premium_entry": 0.0,
            "call_premium_entry": 0.0,
            "put_premium_current": 0.0,
            "call_premium_current": 0.0,
        },
        "pnl": {
            "gross": 0.0,
            "net": 0.0,
            "pct_of_credit": 0.0,
            "theta_today": 0.0,
            "peak": 0.0,
        },
        "rules": {
            "profit_target_pct": 50,
            "sl_multiple": 2,
            "entry_window_start": "10:00",
            "entry_window_end": "11:30",
            "vix_skip_above": 30,
            "vix_event_stop": 22,
            "min_credit": 5000,
            "min_dte": 15
        },
        "alerts": [],
        "log": [],
        "last_updated": "",
        "scan_count": 0,
        "trade_date": "",
    }

# ── simulated market data (paper mode) ───────────────────────────────────────

class PaperMarket:
    """Simulates realistic Nifty + VIX movement for paper trading."""

    def __init__(self, state: dict):
        self.spot     = state["spot"]
        self.vix      = state["vix"]
        self.crude    = state["crude_brent"]
        self._trend   = 0.0
        self._vol_regime = "normal"

    def tick(self) -> dict:
        """Return one tick of simulated market data."""
        h, m = ist_hm()

        # Intraday vol profile — higher at open/close
        if h < 10:
            intra_vol = 1.6
        elif h >= 15:
            intra_vol = 1.4
        elif 12 <= h < 13:
            intra_vol = 0.7
        else:
            intra_vol = 1.0

        spot_sigma = 40 * intra_vol
        vix_sigma  = 0.08
        crude_sigma = 0.8

        # Random walk with slight mean reversion
        self._trend += random.gauss(0, 0.3)
        self._trend  = max(-2, min(2, self._trend))

        self.spot  += random.gauss(self._trend * 5, spot_sigma)
        self.spot   = max(21000, min(28000, self.spot))
        self.vix   += random.gauss(0, vix_sigma)
        self.vix    = max(8, min(40, self.vix))
        self.crude += random.gauss(0, crude_sigma)
        self.crude  = max(70, min(150, self.crude))

        return {
            "spot":  round(self.spot, 2),
            "vix":   round(self.vix, 2),
            "crude": round(self.crude, 2),
        }

    def option_price(self, strike: int, opt_type: str, spot: float,
                     vix: float, dte_days: float) -> float:
        """Simplified Black-Scholes approximation for paper mode."""
        if dte_days <= 0:
            itm = (spot - strike) if opt_type == "CE" else (strike - spot)
            return max(0.0, itm)
        T   = dte_days / 365
        vol = vix / 100
        d   = (math.log(spot / strike) + 0.5 * vol**2 * T) / (vol * math.sqrt(T))
        nd  = 0.5 * (1 + math.erf(d / math.sqrt(2)))
        if opt_type == "CE":
            nd_neg = 0.5 * (1 + math.erf(-d / math.sqrt(2)))
            price  = spot * nd - strike * nd_neg
        else:
            nd_neg = 0.5 * (1 + math.erf(d / math.sqrt(2)))
            price  = strike * (1 - nd_neg) - spot * (1 - nd)
        return max(0.0, round(price, 2))

# ── agent logic ───────────────────────────────────────────────────────────────

class NiftyAgent:

    def __init__(self):
        self.state  = load_state()
        self.market = PaperMarket(self.state)
        self._lock  = threading.Lock()

    def add_log(self, msg: str, level: str = "info"):
        entry = {"t": now_ist().strftime("%H:%M:%S"), "msg": msg, "level": level}
        with self._lock:
            self.state["log"].insert(0, entry)
            self.state["log"] = self.state["log"][:120]
        emit("log", {"level": level, "msg": msg})

    # ── 09:00 morning scan — VIX, macro, range, strikes only ─────────────────
    # Credit is NOT checked here. Premiums at open are noisy and wide.
    # The binding credit check happens at 09:59 on settled mid-market prices.

    def morning_scan(self):
        self.add_log("[SCAN] 09:00 morning scan started", "info")
        s = self.state

        tick = self.market.tick()
        s["spot"]        = tick["spot"]
        s["vix"]         = tick["vix"]
        s["crude_brent"] = tick["crude"]
        s["scan_count"] += 1
        s["trade_date"]  = now_ist().strftime("%d-%b-%Y")

        # Reset daily verdict so stale state never carries over
        s["verdict"]     = "PENDING"
        s["skip_reason"] = ""
        s["phase"]       = "scanning"

        # ── 1. DTE check — skip if too close to expiry (< 15 days) ─────────
        min_dte = s["rules"].get("min_dte", 15)
        if s["dte"] < min_dte:
            s["verdict"]     = "SKIP"
            s["skip_reason"] = (
                f"DTE {s['dte']} < minimum {min_dte} — too close to expiry, "
                f"no new position today. Wait for next monthly series."
            )
            s["phase"] = "pre_market"
            self.add_log(f"[DTE SKIP] {s['skip_reason']}", "warn")
            save_state(s)
            return

        self.add_log(
            f"[DTE OK] {s['dte']} DTE >= {min_dte} minimum — expiry check clear", "ok"
        )

        # ── 2. VIX check — only hard skip at this stage ───────────────────────
        if s["vix"] >= s["rules"]["vix_skip_above"]:
            s["verdict"]     = "SKIP"
            s["skip_reason"] = (
                f"VIX {s['vix']:.2f} >= {s['rules']['vix_skip_above']} — no trade today"
            )
            s["phase"] = "pre_market"
            self.add_log(f"[VIX SKIP] {s['skip_reason']}", "warn")
            save_state(s)
            return

        self.add_log(
            f"[VIX OK] {s['vix']:.2f} < {s['rules']['vix_skip_above']} — VIX clear", "ok"
        )

        # ── 2. Macro / event check ────────────────────────────────────────────
        self.add_log("[EVENT] No scheduled macro events detected — clear", "ok")

        # ── 3. Expected range using opening spot ──────────────────────────────
        dte   = s["dte"]
        sigma = s["spot"] * (s["vix"] / 100) * math.sqrt(dte / 365)
        lower = round(s["spot"] - sigma)
        upper = round(s["spot"] + sigma)
        self.add_log(
            f"[RANGE] 1SD ±{round(sigma)} pts → [{lower:,} – {upper:,}]", "info"
        )

        # ── 4. Provisional strike selection (0.10–0.12 delta zone) ───────────
        put_short  = round((lower - 250) / 100) * 100
        put_long   = put_short - 300
        call_short = round((upper + 250) / 100) * 100
        call_long  = call_short + 300

        s["strikes"] = {
            "put_short": put_short, "put_long": put_long,
            "call_short": call_short, "call_long": call_long
        }
        self.add_log(
            f"[STRIKES] Provisional: {put_long}PE / {put_short}PE | "
            f"{call_short}CE / {call_long}CE", "info"
        )

        # Geo-risk flag (informational only at this point)
        if s["crude_brent"] > 105:
            self.add_log(
                f"[GEO] Crude ${s['crude_brent']:.1f} > $105 — "
                f"position will be sized at 75% if entry proceeds", "warn"
            )

        # ── 5. Verdict stays PENDING until 09:59 credit check ────────────────
        s["verdict"] = "PENDING"
        s["phase"]   = "credit_check_wait"
        self.add_log(
            "[SCAN] Strikes set. Waiting for 09:59 credit check on settled premiums.",
            "info"
        )
        save_state(s)

    # ── 09:59 pre-entry credit check — binding go / no-go decision ───────────
    # Re-prices all four strikes using the current (settled) spot and VIX.
    # Only if combined credit >= minimum does the agent move to entry_wait.

    def pre_entry_credit_check(self):
        s = self.state

        # Guard: only run once, only when scan has cleared
        if s["phase"] != "credit_check_wait":
            return
        if s["verdict"] == "SKIP":
            return

        self.add_log(
            "[CREDIT CHECK] 09:59 — re-pricing strikes on settled premiums", "info"
        )

        # Refresh spot and VIX with the latest tick (market has been open 44 min)
        tick = self.market.tick()
        s["spot"] = tick["spot"]
        s["vix"]  = tick["vix"]

        sk  = s["strikes"]
        dte = s["dte"]

        # Re-run range and strikes on settled spot in case of a large gap move
        sigma      = s["spot"] * (s["vix"] / 100) * math.sqrt(dte / 365)
        lower      = round(s["spot"] - sigma)
        upper      = round(s["spot"] + sigma)
        put_short  = round((lower - 250) / 100) * 100
        put_long   = put_short - 300
        call_short = round((upper + 250) / 100) * 100
        call_long  = call_short + 300

        # Log if strikes shifted materially from 09:00
        if put_short != sk["put_short"] or call_short != sk["call_short"]:
            self.add_log(
                f"[CREDIT CHECK] Spot moved — strikes revised: "
                f"{put_long}PE/{put_short}PE | {call_short}CE/{call_long}CE", "warn"
            )
        else:
            self.add_log(
                f"[CREDIT CHECK] Strikes unchanged: "
                f"{put_long}PE/{put_short}PE | {call_short}CE/{call_long}CE", "info"
            )

        s["strikes"] = {
            "put_short": put_short, "put_long": put_long,
            "call_short": call_short, "call_long": call_long
        }

        # Price all four legs
        put_cr  = self.market.option_price(put_short,  "PE", s["spot"], s["vix"], dte)
        put_lp  = self.market.option_price(put_long,   "PE", s["spot"], s["vix"], dte)
        call_cr = self.market.option_price(call_short, "CE", s["spot"], s["vix"], dte)
        call_lp = self.market.option_price(call_long,  "CE", s["spot"], s["vix"], dte)

        put_net  = round((put_cr  - put_lp)  * 50, 2)
        call_net = round((call_cr - call_lp) * 50, 2)
        total    = put_net + call_net
        minimum  = s["rules"]["min_credit"]

        s["credits"] = {
            "put_leg":    put_net,
            "call_leg":   call_net,
            "total":      total,
            "target_min": minimum,
            "checked_at": now_ist().strftime("%H:%M:%S"),
            "spot_at_check": round(s["spot"], 2),
            "vix_at_check":  round(s["vix"], 2),
        }

        self.add_log(
            f"[CREDIT CHECK] Put spread: ₹{put_net:,.0f} | "
            f"Call spread: ₹{call_net:,.0f} | "
            f"Total: ₹{total:,.0f} | Min: ₹{minimum:,}", "info"
        )

        # ── binding decision ──────────────────────────────────────────────────
        # Re-confirm DTE hasn't slipped below minimum since morning scan
        min_dte = s["rules"].get("min_dte", 15)
        if s["dte"] < min_dte:
            s["verdict"]     = "SKIP"
            s["skip_reason"] = (
                f"DTE {s['dte']} < minimum {min_dte} at 09:59 re-check — skipping"
            )
            s["phase"] = "pre_market"
            self.add_log(f"[DTE SKIP] {s['skip_reason']}", "warn")
            save_state(s)
            return

        if total < minimum:
            s["verdict"]     = "SKIP"
            s["skip_reason"] = (
                f"Credit ₹{total:,.0f} < minimum ₹{minimum:,} at 09:59 — "
                f"premium insufficient, no trade today"
            )
            s["phase"] = "pre_market"
            self.add_log(f"[CREDIT SKIP] {s['skip_reason']}", "warn")
            save_state(s)
            return

        self.add_log(
            f"[CREDIT OK] ₹{total:,.0f} >= ₹{minimum:,} minimum — "
            f"GO for entry at 10:00", "ok"
        )
        s["verdict"] = "PROCEED"
        s["phase"]   = "entry_wait"
        self.add_log(
            f"[GO] Credit check passed at 09:59. "
            f"Entry window opens at 10:00.", "exec"
        )
        save_state(s)

    # ── entry execution ────────────────────────────────────────────────────────

    def execute_entry(self):
        s = self.state
        if s["phase"] != "entry_wait" or s["verdict"] != "PROCEED":
            return
        if s["position"]["active"]:
            return

        tick = self.market.tick()
        s["spot"] = tick["spot"]

        # Stability check: spot within 150 pts of scan price (simplified)
        self.add_log("[ENTRY] Stability check passed — firing all 4 legs", "exec")

        sk   = s["strikes"]
        dte  = s["dte"]
        vix  = s["vix"]
        spot = s["spot"]

        pp_entry = self.market.option_price(sk["put_short"],  "PE", spot, vix, dte)
        lp_entry = self.market.option_price(sk["put_long"],   "PE", spot, vix, dte)
        cp_entry = self.market.option_price(sk["call_short"], "CE", spot, vix, dte)
        lc_entry = self.market.option_price(sk["call_long"],  "CE", spot, vix, dte)

        put_spread_credit  = round((pp_entry - lp_entry) * 50, 2)
        call_spread_credit = round((cp_entry - lc_entry) * 50, 2)
        total_credit       = put_spread_credit + call_spread_credit

        s["position"].update({
            "active": True,
            "entry_time": now_ist().strftime("%H:%M:%S"),
            "entry_spot": spot,
            "put_premium_entry":  pp_entry,
            "call_premium_entry": cp_entry,
            "put_premium_current":  pp_entry,
            "call_premium_current": cp_entry,
        })
        s["credits"]["total"]    = total_credit
        s["credits"]["put_leg"]  = put_spread_credit
        s["credits"]["call_leg"] = call_spread_credit
        s["phase"]               = "active"

        self.add_log(
            f"[PAPER FILL] SELL {sk['put_short']}PE @ {pp_entry:.1f} | "
            f"BUY {sk['put_long']}PE @ {lp_entry:.1f}", "exec"
        )
        self.add_log(
            f"[PAPER FILL] SELL {sk['call_short']}CE @ {cp_entry:.1f} | "
            f"BUY {sk['call_long']}CE @ {lc_entry:.1f}", "exec"
        )
        self.add_log(
            f"[IC ACTIVE] Net credit = ₹{total_credit:,.0f}. "
            f"Target: ₹{total_credit*0.5:,.0f}  SL: ₹{total_credit*2:,.0f}", "exec"
        )

        save_state(s)

    # ── intraday monitoring ────────────────────────────────────────────────────

    def monitor_position(self):
        s = self.state
        if not s["position"]["active"] or s["phase"] != "active":
            return

        tick = self.market.tick()
        s["spot"] = tick["spot"]
        s["vix"]  = tick["vix"]

        sk  = s["strikes"]
        dte = max(0.01, s["dte"] - (1 - (15.5 - ist_hm()[0]) / 6.5))
        vix = s["vix"]
        spot = s["spot"]

        put_now  = self.market.option_price(sk["put_short"],  "PE", spot, vix, dte)
        call_now = self.market.option_price(sk["call_short"], "CE", spot, vix, dte)

        s["position"]["put_premium_current"]  = put_now
        s["position"]["call_premium_current"] = call_now

        pe = s["position"]["put_premium_entry"]
        ce = s["position"]["call_premium_entry"]
        credit = s["credits"]["total"]

        put_pnl  = (pe - put_now)  * 50
        call_pnl = (ce - call_now) * 50
        gross    = round(put_pnl + call_pnl, 2)

        s["pnl"]["gross"]         = gross
        s["pnl"]["net"]           = gross
        s["pnl"]["pct_of_credit"] = round(gross / credit * 100, 1) if credit else 0
        s["pnl"]["theta_today"]   = round(credit / (s["dte"] * 1.2), 2)
        s["pnl"]["peak"]          = max(s["pnl"].get("peak", 0), gross)

        s["last_updated"] = now_ist().isoformat()

        # ── exit checks ──
        # 1. Profit target
        if gross >= credit * 0.50:
            self.add_log(
                f"[TARGET HIT] 50% profit: ₹{gross:,.0f}. Auto-closing all legs.", "exec"
            )
            self._close_position("PROFIT_TARGET")
            return

        # 2. Stop loss — either short leg > 2× entry
        put_sl  = pe * s["rules"]["sl_multiple"]
        call_sl = ce * s["rules"]["sl_multiple"]
        if put_now > put_sl:
            self.add_log(
                f"[SL] PUT leg breached 2× ({put_now:.1f} > {put_sl:.1f}). "
                f"Closing spread.", "warn"
            )
            self._close_position("PUT_SL")
            return
        if call_now > call_sl:
            self.add_log(
                f"[SL] CALL leg breached 2× ({call_now:.1f} > {call_sl:.1f}). "
                f"Closing spread.", "warn"
            )
            self._close_position("CALL_SL")
            return

        # 3. VIX event stop
        if vix >= s["rules"]["vix_event_stop"]:
            self.add_log(
                f"[EVENT STOP] VIX {vix:.2f} >= {s['rules']['vix_event_stop']}. "
                f"Closing all.", "warn"
            )
            self._close_position("VIX_STOP")
            return

        save_state(s)

    def _close_position(self, reason: str):
        s = self.state
        s["position"]["active"] = False
        s["phase"]              = "exited"
        s["alerts"].insert(0, {
            "t": now_ist().strftime("%H:%M:%S"),
            "msg": f"Position closed: {reason} | P&L: ₹{s['pnl']['gross']:,.0f}"
        })
        self.add_log(f"[CLOSED] Reason={reason} PnL=₹{s['pnl']['gross']:,.0f}", "exec")
        save_state(s)

    # ── manual recalibration (triggered via dashboard button) ───────────────
    # Called when the flag file logs/cmd_recalibrate.flag is detected.
    # Re-runs full scan + immediate credit check + opens entry window.
    # Safe guards: won't fire if position is already active or exited.

    def manual_recalibrate(self, triggered_at: str):
        s = self.state

        # ── safety guards ─────────────────────────────────────────────────────
        if s["position"]["active"]:
            self.add_log(
                "[RECAL] ⚠ Ignored — position already active", "warn"
            )
            return

        if s["phase"] == "exited":
            self.add_log(
                "[RECAL] ⚠ Ignored — trade already exited today", "warn"
            )
            return

        h, _ = ist_hm()
        if h < 9:
            self.add_log(
                "[RECAL] ⚠ Ignored — market not yet open (before 09:15)", "warn"
            )
            return
        if h >= 14:
            self.add_log(
                "[RECAL] ⚠ Ignored — too late in the day (after 14:00)", "warn"
            )
            return

        self.add_log(
            f"[RECAL] ═══ MANUAL RECALIBRATION TRIGGERED at {triggered_at} ═══", "exec"
        )
        self.add_log(
            "[RECAL] Step 1/3 — Running full morning scan on current market data", "info"
        )

        # Step 1: Full morning scan (VIX, macro, range, strikes)
        # Reset phase so morning_scan() runs cleanly
        s["phase"]   = "pre_market"
        s["verdict"] = "PENDING"
        self.morning_scan()

        # If scan returned a SKIP (VIX too high etc), stop here
        if s["verdict"] == "SKIP":
            self.add_log(
                f"[RECAL] ✗ Scan returned SKIP — {s['skip_reason']}", "warn"
            )
            return

        self.add_log(
            "[RECAL] Step 2/3 — Running immediate credit check on live premiums", "info"
        )

        # Step 2: Immediate credit check (same logic as 09:59, run right now)
        self.pre_entry_credit_check()

        if s["verdict"] == "SKIP":
            self.add_log(
                f"[RECAL] ✗ Credit check failed — {s['skip_reason']}", "warn"
            )
            self.add_log(
                "[RECAL] No trade. Try recalibrating again later if premiums improve.", "warn"
            )
            return

        self.add_log(
            "[RECAL] Step 3/3 — Credit passed. Opening entry window immediately.", "exec"
        )
        self.add_log(
            "[RECAL] ✓ Recalibration complete — entry will fire on next loop tick (≤30s)", "exec"
        )
        # phase is now "entry_wait" — the main loop will call execute_entry() within 30s

    # ── end of day ────────────────────────────────────────────────────────────

    def end_of_day(self):
        s = self.state
        if s["position"]["active"]:
            self.add_log("[EOD] Closing open position at 15:15 EOD cutoff", "warn")
            self._close_position("EOD_CLOSE")
        s["dte"]   = max(0, s["dte"] - 1)
        s["phase"] = "pre_market"
        save_state(s)
        self.add_log(f"[EOD] Day complete. DTE now {s['dte']}", "info")

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self):
        log.info("=== Nifty AI Agent started (paper mode) ===")

        # Daily flags — reset at midnight each trading day
        _morning_done       = False
        _credit_check_done  = False   # NEW: 09:59 credit check flag
        _entry_done         = False
        _eod_done           = False

        while True:
            try:
                if not is_market_day():
                    time.sleep(60)
                    continue

                h, m = ist_hm()

                # ── Reset all daily flags at midnight ─────────────────────────
                if h == 0 and m == 0:
                    _morning_done      = False
                    _credit_check_done = False
                    _entry_done        = False
                    _eod_done          = False

                # ── Manual recalibration flag check (every 30s during market hours) ──
                # Server writes logs/cmd_recalibrate.flag when user clicks the button.
                # We consume and delete the flag immediately to prevent double-fire.
                if (9 <= h < 14) and RECAL_FLAG_FILE.exists():
                    try:
                        flag_data = json.loads(RECAL_FLAG_FILE.read_text())
                        triggered_at = flag_data.get("requested_at", "unknown")
                        RECAL_FLAG_FILE.unlink()          # consume the flag
                        # Reset daily flags so recal can re-run scan + credit check
                        _morning_done      = True         # skip auto 09:00 re-run
                        _credit_check_done = True         # skip auto 09:59 re-run
                        _entry_done        = False        # allow fresh entry
                        self.manual_recalibrate(triggered_at)
                    except Exception as recal_err:
                        log.exception(f"Recalibration error: {recal_err}")
                        try:
                            RECAL_FLAG_FILE.unlink()      # clean up even on error
                        except Exception:
                            pass

                # ── 09:00 — Morning scan (VIX + macro + range + strikes) ──────
                # Credit is NOT assessed here. Premiums at open are unreliable.
                if h == 9 and m == 0 and not _morning_done:
                    self.morning_scan()
                    _morning_done = True

                # ── 09:59 — Binding credit check on settled premiums ──────────
                # Agent re-prices all four strikes and decides GO / NO-GO.
                # Entry window at 10:00 only opens if this check passes.
                if h == 9 and m == 59 and not _credit_check_done:
                    self.pre_entry_credit_check()
                    _credit_check_done = True

                # ── 10:00–11:30 — Entry window ────────────────────────────────
                # execute_entry() only fires if verdict == PROCEED (set at 09:59)
                if (h == 10 or (h == 11 and m <= 30)):
                    if not _entry_done and self.state["phase"] == "entry_wait":
                        self.execute_entry()
                        _entry_done = True

                # ── 09:15–15:15 — Intraday monitoring every 30s ───────────────
                if (h == 9 and m >= 15) or (10 <= h < 15) or (h == 15 and m <= 15):
                    self.monitor_position()

                # ── 15:15 — End of day close ──────────────────────────────────
                if h == 15 and m == 15 and not _eod_done:
                    self.end_of_day()
                    _eod_done = True

                # ── Continuous spot/VIX update during market hours ────────────
                if 9 <= h < 16:
                    tick = self.market.tick()
                    with self._lock:
                        self.state["spot"]         = tick["spot"]
                        self.state["vix"]          = tick["vix"]
                        self.state["last_updated"] = now_ist().isoformat()
                    save_state(self.state)

            except Exception as e:
                log.exception(f"Agent loop error: {e}")

            time.sleep(30)   # tick every 30 seconds


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    agent = NiftyAgent()
    agent.run()
