"""
Nifty Agent — lightweight FastAPI server
Exposes /api/state (JSON) and /api/events (SSE stream) for the dashboard.
Handles HEAD requests on all routes so Render health checks pass (HTTP 200).
"""

import json
import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

STATE_FILE       = Path("logs/state.json")
EVENTS_FILE      = Path("logs/events.jsonl")
RECAL_FLAG_FILE  = Path("logs/cmd_recalibrate.flag")  # engine polls this

app = FastAPI(title="Nifty AI Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ── helpers ───────────────────────────────────────────────────────────────────

def read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"status": "initialising", "phase": "pre_market"}

def read_last_events(n: int = 100) -> list:
    if not EVENTS_FILE.exists():
        return []
    try:
        lines = EVENTS_FILE.read_text().strip().split("\n")
        rows  = []
        for line in reversed(lines[-n:]):
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        return rows
    except Exception:
        return []

# ── health check — Render pings HEAD / and HEAD /api/state ───────────────────

@app.head("/")
@app.head("/health")
@app.head("/api/state")
async def health_head():
    """Render.com health checker sends HEAD — must return 200."""
    return Response(status_code=200)

@app.get("/health")
async def health_get():
    return JSONResponse({"ok": True, "phase": read_state().get("phase", "unknown")})

# ── data endpoints ────────────────────────────────────────────────────────────

@app.get("/api/state")
def get_state():
    return JSONResponse(read_state())

@app.get("/api/events")
def get_events():
    return JSONResponse(read_last_events(100))

@app.get("/api/stream")
async def sse_stream():
    """Server-Sent Events — dashboard receives live state pushes every 2s."""
    async def generator():
        last_mtime = 0
        # Send an immediate heartbeat so the browser connection opens cleanly
        yield ": heartbeat\n\n"
        while True:
            try:
                mtime = STATE_FILE.stat().st_mtime if STATE_FILE.exists() else 0
                if mtime != last_mtime:
                    last_mtime = mtime
                    payload = json.dumps(read_state())
                    yield f"data: {payload}\n\n"
                else:
                    # Keep-alive comment every 20s to prevent Render from closing idle SSE
                    yield ": keep-alive\n\n"
            except Exception as e:
                yield f"data: {{\"error\": \"{e}\"}}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # tells Render/nginx not to buffer SSE
        }
    )

# ── recalibrate command ───────────────────────────────────────────────────────

@app.head("/api/recalibrate")
async def recal_head():
    return Response(status_code=200)

@app.post("/api/recalibrate")
async def recalibrate(request: Request):
    """
    Manual recalibration trigger.
    Writes a flag file that the engine picks up on its next 30s tick.
    The engine will:
      1. Re-run the full morning scan (VIX + macro + range + strikes)
      2. Immediately re-run the credit check on current live premiums
      3. If credit passes, open the entry window right away (no waiting for 10:00)
    Safe to call at any time after 09:15 and before 11:30.
    Will NOT fire if a position is already active or the day's trade is already exited.
    """
    s = read_state()

    # Guard: don't allow recal if already in a live position
    if s.get("position", {}).get("active", False):
        return JSONResponse(
            {"ok": False, "reason": "Position already active — recalibrate not allowed"},
            status_code=409
        )
    if s.get("phase") == "exited":
        return JSONResponse(
            {"ok": False, "reason": "Trade already exited today — recalibrate not allowed"},
            status_code=409
        )

    # Write the flag file — engine detects and acts within 30s
    RECAL_FLAG_FILE.write_text(
        json.dumps({"requested_at": __import__("datetime").datetime.utcnow().isoformat(), "source": "manual_dashboard"})
    )
    return JSONResponse({"ok": True, "msg": "Recalibration command sent — agent will re-scan within 30s"})


@app.get("/api/recalibrate/status")
def recal_status():
    """Returns whether a recalibration flag is currently pending."""
    pending = RECAL_FLAG_FILE.exists()
    return JSONResponse({"pending": pending})


# ── dashboard ─────────────────────────────────────────────────────────────────

@app.head("/")
async def dashboard_head():
    return Response(status_code=200)

@app.get("/", response_class=HTMLResponse)
def dashboard():
    html_path = Path("static/dashboard.html")
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse(
        "<h1 style='font-family:monospace;padding:2rem'>Dashboard loading — "
        "agent initialising, refresh in 30s</h1>",
        status_code=200
    )

# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
