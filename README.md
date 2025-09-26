# Snow’s DG‑Lab Controller (Coyote 3.0) — API + Telegram Bot

Control a DG‑Lab **Coyote 3.0** via a local **WebSocket** server and a small **FastAPI** service, with a **Telegram bot** frontend for quick commands.

> **Credits**: Built for and with 💙 by **SnowDutchie**.  
> Uses the excellent `pydglab-ws` protocol library.

---

##  Features

- **Single‑client DG‑Lab WebSocket server** (the official app binds via QR).
- **FastAPI** with one endpoint: `POST /shock` — fires a pulse in *Socket Control* mode.
- **FIFO queue**: multiple concurrent requests are serialized safely.
- **Owner amplitude cap** (`OWNER_MAX_POWER`) applied to every request.
- **Frequency cap** `0..200 Hz`; copies `1..100` (each copy ≈ 100 ms).
- **Telegram bot** (`/shock`) with pretty `/start`, **admin reports**, and playful _“ouch ⚡❄️”_.
- Works on **Windows 11** and **Linux (Ubuntu 22.04/24.04)**.

Repo layout:
```
coyote_ws_server_api.py   # FastAPI + DG‑Lab WS + queue
tele_shocker_bot.py       # Telegram bot calling /shock
server_qr.png             # (optional) QR generated on server start
```

---

##  Requirements

- Python **3.10+**
- Packages: `pydglab-ws`, `fastapi`, `uvicorn`, `qrcode[pil]`, `python-telegram-bot`, `httpx`

Create a virtualenv and install:
```bash
python -m venv .venv
# Windows
.\.venv\Scripts\Activate.ps1
# Linux/macOS
source .venv/bin/activate

python -m pip install --upgrade pip
pip install pydglab-ws fastapi "uvicorn[standard]" qrcode[pil] python-telegram-bot httpx
```

---

##  Quick Start (Local)

### 1) Start the API server
```bash
uvicorn coyote_ws_server_api:app --host 0.0.0.0 --port 8000
```
- Console prints `ws://<your-ip>:4567`.  
- Scan the **QR** (or enter the URL) in the DG‑Lab app to bind.

### 2) Start the Telegram bot
```bash
python tele_shocker_bot.py
```
- If your API runs elsewhere: `set API_BASE=http://SERVER:8000` (Windows) or `export API_BASE=http://SERVER:8000` (Linux) before running.

### 3) Use it
**Telegram**:
```
/shock A 20 20 5
```
**API (curl)**:
```bash
curl -X POST http://SERVER:8000/shock \
  -H "Content-Type: application/json" \
  -d '{"channel":"A","amp":20,"freq":20,"copies":5}'
```

---

## ⚙️ Configuration (env vars)

| Variable           | Where           | Meaning                                               | Default |
|--------------------|-----------------|-------------------------------------------------------|---------|
| `OWNER_MAX_POWER`  | API             | Cosmetic cap on amplitude (`0..100`)                  | `50`    |
| `API_BASE`         | Bot             | Base URL of API (e.g., `http://127.0.0.1:8000`)       | local   |
| `ADMIN_ID`         | Bot             | Telegram user id to receive reports                   | set in code |
| `ALLOWED_CHAT_ID`  | Bot (optional)  | Restrict bot to one chat id                           | unset   |
| `TG_BOT_TOKEN`     | SSE viewer opt. | Token for live update viewer example (not required)   | unset   |

> **Note**: Your current bot file contains the token inline by design. For production security, prefer environment variables.

---

##  API Reference

### `POST /shock`
Fire a pulse (Socket Control).

**Request JSON**
```json
{
  "channel": "A",          // "A" or "B"
  "amp": 20,               // 0..100 (owner cap may reduce this)
  "freq": 20,              // 0..200 Hz
  "copies": 5              // 1..100 (each ≈ 100 ms)
}
```

**Response JSON**
```json
{
  "ok": true,
  "mode": "pulse",
  "channel": "A",
  "amp_requested": 20,
  "amp_effective": 20,
  "owner_max": 50,
  "freq": 20,
  "copies": 5,
  "approx_duration_ms": 500
}
```

**Errors**
- `503` — device/app not bound yet (scan QR in DG‑Lab app).
- `400` — invalid pulse parameters (protocol constraints).
- `500` — unexpected failure.

---

## 💬 Telegram Bot

- **Commands**:  
  `/shock <A|B> <amp 0..100> [freq 0..200] [copies 1..100]`  
  Examples: `/shock A 25 20 5`, `/shock B 30`

- **Behavior**:  
  - Replies with details and *“ouch ⚡❄️”*.  
  - Sends an **admin report** (user id, profile photo, deep links).  
  - Clamps `freq` to `≤ 200` and `copies` to `1..100`.

---

##  Windows: build portable `.exe` (optional)

Install **PyInstaller**:
```powershell
pip install pyinstaller
```

**API server**: create a tiny launcher `server_launcher.py`:
```python
import uvicorn
if __name__ == "__main__":
    uvicorn.run("coyote_ws_server_api:app", host="0.0.0.0", port=8000)
```
Build:
```powershell
pyinstaller --clean --noconfirm --onefile --name CoyoteServer `
  --collect-all fastapi --collect-all starlette --collect-all pydantic `
  --collect-all pydglab_ws server_launcher.py
```
**Bot**:
```powershell
pyinstaller --clean --noconfirm --onefile --name SnowDGLabBot `
  --collect-all httpx --collect-all certifi --collect-all telegram `
  tele_shocker_bot.py
```

Outputs: `dist\CoyoteServer.exe`, `dist\SnowDGLabBot.exe`.

---

##  VPS / Cloud (recommended)

Use a small VPS (Hetzner/DigitalOcean/Vultr). Open ports **8000** (API) and **4567** (DG‑Lab WS).  
Create **systemd** services to auto‑start on boot. Consider **Nginx + TLS** if you want HTTPS/WSS.

---

##  Safety & Security

- Start with low amplitude. Respect device and channel limits.
- Never commit secrets (bot token) to a public repo.
- Optionally restrict the bot with `ALLOWED_CHAT_ID` and run behind a firewall.

---

##  Troubleshooting

- **`503 Device/app not bound yet`**: Open the DG‑Lab app and scan the QR (`ws://<server-ip>:4567`).  
- **No pulses**: Ensure you’re in **Socket Control** mode in the app; frequency `≤ 200`.  
- **Windows firewall**: Allow when prompted (or open ports manually).  
- **Many users**: The queue is FIFO; requests wait and run one by one.

---

##  Credits

- **SnowDutchie** — project owner, direction, and testing.
- `pydglab-ws` — DG‑Lab protocol library used under the hood.
- DG‑Lab community for reverse‑engineering notes and examples.

---

##  License

MIT © 2025 SnowDutchie — see [LICENSE](./LICENSE) for details.
