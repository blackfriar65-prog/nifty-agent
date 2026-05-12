# Nifty AI Agent — Deployment Guide
# Paper trading | Monthly options | Fully autonomous

## What this agent does automatically (no manual clicks ever)
- 09:00 IST: Morning scan — Nifty spot, India VIX, crude, macro events
- 09:01 IST: VIX check — skips if VIX > 30
- 09:03 IST: Calculates expected range → selects 0.10–0.12 delta strikes
- 09:05 IST: Credit check — skips if combined credit < ₹8,000
- 10:00 IST: Entry window opens — fires all 4 paper legs automatically
- Every 30s: Monitors premiums, checks SL (2× premium) and profit target (50%)
- Auto-exits on: profit target hit / stop loss breach / VIX > 22 / EOD 15:15
- Repeats every trading day until May 27 expiry

---

## OPTION A — Render.com (recommended, no credit card needed)

1. Create free account at https://render.com
2. Upload this folder to a new GitHub repo (github.com → New repo → upload files)
3. On Render: New → Web Service → Connect your GitHub repo
4. Settings:
     Build Command : pip install -r requirements.txt
     Start Command : bash start.sh
     Instance Type : Free
5. Click "Create Web Service" — Render builds and deploys automatically
6. Your dashboard URL: https://nifty-ai-agent.onrender.com

⚠ FREE TIER CAVEAT: Render free services sleep after 15 min of no HTTP traffic.
   Fix: Use https://uptimerobot.com (free) — add a monitor pinging /api/state
   every 5 minutes → service stays awake 24/7.

---

## OPTION B — Railway.app (easiest, $5 credit on signup = ~30 days free)

1. Go to https://railway.app → Login with GitHub
2. New Project → Deploy from GitHub repo → select your repo
3. Railway auto-detects railway.toml and deploys
4. Public URL assigned automatically (e.g. nifty-agent.up.railway.app)
5. No sleep issues — Railway keeps services running on free credits

---

## OPTION C — Oracle Cloud Always Free (best for long-term, truly free forever)

### Step 1: Create Oracle Cloud account
- Go to https://cloud.oracle.com → Sign Up (free tier, credit card for verification only, NOT charged)
- Choose region: Mumbai (ap-mumbai-1) — closest to NSE

### Step 2: Create a VM instance
- Compute → Instances → Create Instance
- Shape: VM.Standard.A1.Flex (Always Free ARM) — 1 OCPU, 6GB RAM
- OS: Ubuntu 22.04
- Add your SSH public key (or generate one in the wizard)
- Click Create — wait ~2 min for it to boot

### Step 3: Open port 8080 in firewall
- Go to instance → Subnet → Security List → Add Ingress Rule
  - Source: 0.0.0.0/0
  - Port: 8080
  - Protocol: TCP

### Step 4: SSH into your VM and deploy
```bash
ssh ubuntu@<YOUR_VM_IP>

# On the VM:
sudo apt update && sudo apt install -y python3-pip git
git clone https://github.com/YOUR_USERNAME/nifty-agent.git
cd nifty-agent
pip3 install -r requirements.txt

# Install as a system service (auto-starts on reboot)
sudo cp nifty-agent.service /etc/systemd/system/
sudo sed -i 's|/home/ubuntu/nifty-agent|'"$(pwd)"'|g' /etc/systemd/system/nifty-agent.service
sudo systemctl daemon-reload
sudo systemctl enable nifty-agent
sudo systemctl start nifty-agent

# Check it's running
sudo systemctl status nifty-agent
```

### Step 5: Access your dashboard
Open browser: http://<YOUR_VM_IP>:8080

Optional — add a free domain via Cloudflare Tunnel so you get HTTPS:
```bash
# On the VM:
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared
chmod +x cloudflared
./cloudflared tunnel --url http://localhost:8080
# Cloudflare prints a random https://xxx.trycloudflare.com URL — use that
```

---

## Quickest path to Monday 11 May go-live

If today is Saturday evening (IST):

1. Create GitHub account + upload files (10 min)
2. Create Render.com account + deploy (5 min)
3. Create UptimeRobot monitor on /api/state (2 min)
4. Open dashboard URL and verify agent is in "scanning" phase
5. Monday 09:00 IST — agent auto-fires morning scan with zero input from you

Total setup time: ~20 minutes.

---

## Verify the agent is working

Check these endpoints after deploy:
- GET /api/state  → returns full JSON state (phase, pnl, strikes, logs)
- GET /api/events → last 100 agent log entries
- GET /          → live dashboard HTML

Expected sequence on Monday:
  09:00 — phase: "scanning"
  09:05 — phase: "entry_wait", verdict: "PROCEED"
  10:00–10:05 — phase: "active", position.active: true
  (intraday) — pnl.gross updates every 30s
  (on exit)  — phase: "exited"

---

## Files in this package

  agent/engine.py     — autonomous agent (market scan, entry, monitor, exit)
  server.py           — FastAPI server (dashboard + SSE stream + JSON API)
  static/dashboard.html — live dashboard (no framework, vanilla JS + Chart.js)
  requirements.txt    — Python deps (fastapi, uvicorn only)
  start.sh            — unified start script
  Procfile            — Render/Heroku process file
  render.yaml         — Render deploy config
  railway.toml        — Railway deploy config
  nifty-agent.service — systemd unit for Oracle/VPS
  DEPLOY.md           — this file
