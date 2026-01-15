#!/usr/bin/env python3
"""
DG-Lab Coyote 3.0 WebSocket server (single client) + FastAPI API
Endpoint:
  POST /shock  -> real pulse burst (Socket Control), queued/serialized

Run:
  cd "C:\Users\tiago\Documents\GitHub\Telegram-Dg-Lab"
python -m uvicorn "telegram dglab.coyote_ws_server_api:app" --host 0.0.0.0 --port 8000

Design goals:
- Keep the code small, explicit, and easy to read.
- Queue concurrent shocks so device commands never collide.
- Enforce owner amplitude cap and protocol-safe frequency limits.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, validator

# ─────────────────────────────────────────────────────────────────────────────
# Dependencies (protocol + optional QR)
# ─────────────────────────────────────────────────────────────────────────────
try:
    from pydglab_ws import (
        DGLabWSServer,
        Channel,
        InvalidPulseOperation,
        PulseDataTooLong,
    )
except ImportError as e:
    raise SystemExit(
        "Missing dependency: pydglab-ws. Install:\n  python -m pip install pydglab-ws"
    ) from e

try:
    import qrcode  # optional (for QR image)
    HAVE_QR = True
except ImportError:
    HAVE_QR = False


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    WS_HOST: str = "0.0.0.0"   # DG-Lab app binds here (WebSocket)
    WS_PORT: int = 4567
    HEARTBEAT: int = 60

    DEF_AMP: int = 20
    DEF_FREQ: int = 20
    DEF_COPIES: int = 5

    FREQ_MAX: int = 200        # protocol/safety cap
    COPIES_MIN: int = 1
    COPIES_MAX: int = 100

    # Cosmetic owner amplitude cap (applied to requested amp)
    OWNER_MAX_POWER: int = int(os.getenv("OWNER_MAX_POWER", "50"))


CFG = Config()
API_DESCRIPTION = "DG-Lab Coyote WS + FastAPI; endpoint: POST /shock (queued)"


# ─────────────────────────────────────────────────────────────────────────────
# Paths / QR output (robust for normal runs + PyInstaller onefile)
# ─────────────────────────────────────────────────────────────────────────────
def get_base_dir() -> Path:
    """
    Returns a stable directory where we can write output files.
    - Normal Python run: directory of this file.
    - PyInstaller frozen app: directory of the executable.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
QR_DIR = BASE_DIR / "qrcodes"
QR_DIR.mkdir(parents=True, exist_ok=True)
QR_PATH = QR_DIR / "server_qr.png"


# ─────────────────────────────────────────────────────────────────────────────
# Small utils
# ─────────────────────────────────────────────────────────────────────────────
def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


def guess_lan_ip() -> str:
    """Determine LAN IP (used to print ws:// URL for the app QR)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass
    return ip


# ─────────────────────────────────────────────────────────────────────────────
# Controller: thin, explicit wrapper around pydglab_ws client
# ─────────────────────────────────────────────────────────────────────────────
class CoyoteController:
    def __init__(self, client):
        self.client = client

    async def clear_pulses(self, channel: str) -> None:
        ch = Channel.A if channel.upper() == "A" else Channel.B
        await self.client.clear_pulses(ch)

    def _build_pulse_frames(self, amp: int, freq: int, copies: int):
        """
        Create N frames. Each frame (100ms) has:
          ((f,f,f,f), (s1,s2,s3,s4))
        We raise strength for one slot to make a crisp jolt.
        """
        amp = clamp_int(amp, 0, 100)
        freq = clamp_int(freq, 0, CFG.FREQ_MAX)
        copies = clamp_int(copies, CFG.COPIES_MIN, CFG.COPIES_MAX)

        freq_op = (freq, freq, freq, freq)
        strength_spike = (0, amp, 0, 0)   # single spike per frame
        frame = (freq_op, strength_spike)
        return [frame] * copies

    async def shock(self, channel: str, amp: int, freq: int, copies: int) -> None:
        ch = Channel.A if channel.upper() == "A" else Channel.B
        frames = self._build_pulse_frames(amp, freq, copies)
        await self.client.clear_pulses(ch)          # ensure immediate start
        await self.client.add_pulses(ch, *frames)   # enqueue frames


# ─────────────────────────────────────────────────────────────────────────────
# Job + Server with single worker (serialization)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ShockJob:
    channel: str
    amp_requested: int
    freq: int
    copies: int
    done: asyncio.Future  # completes with Dict response (or raises HTTPException)


class CoyoteServer:
    """
    Owns:
      - The DG-Lab WebSocket server the app binds to.
      - A CoyoteController for commands.
      - A single worker consuming an async queue (no command collisions).
    """
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.controller: Optional[CoyoteController] = None
        self.bound_event = asyncio.Event()
        self._ws_task: Optional[asyncio.Task] = None
        self._worker_task: Optional[asyncio.Task] = None
        self.queue: asyncio.Queue[ShockJob] = asyncio.Queue()

    def start(self) -> None:
        if not self._ws_task or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._run_ws())
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        for t in (self._worker_task, self._ws_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def _run_ws(self) -> None:
        ip = guess_lan_ip()
        public_uri = f"ws://{ip}:{self.cfg.WS_PORT}"
        print("====================================================")
        print("DG-Lab Coyote 3.0 WebSocket Server (single client)")
        print(f"Scan this in the DG-Lab app:\n  {public_uri}")
        print("====================================================")

        async with DGLabWSServer(self.cfg.WS_HOST, self.cfg.WS_PORT, self.cfg.HEARTBEAT) as server:
            client = server.new_local_client()
            self.controller = CoyoteController(client)

            # Optional QR image (handy in practice)
            qr_payload = client.get_qrcode(public_uri)
            if qr_payload and HAVE_QR:
                try:
                    qrcode.make(qr_payload).save(QR_PATH)
                    print(f"[QR] Saved QR to: {QR_PATH.resolve()}")
                except Exception as e:
                    print(f"[QR] Save skipped (error): {e}")
            elif qr_payload and not HAVE_QR:
                print("[QR] qrcode module not installed, skipping PNG output.")
                print("     Install with: python -m pip install qrcode[pil]")

            print("Waiting for the app to bind…")
            await client.bind()   # block until phone app connects
            self.bound_event.set()
            print(f"Bound to app. target_id={client.target_id}")

            # Log app telemetry (handy for debugging)
            async def telemetry():
                async for data in client.data_generator():
                    print(f"[APP] {data}")

            t_task = asyncio.create_task(telemetry())
            try:
                await asyncio.Future()  # keep context alive
            finally:
                t_task.cancel()

    async def _run_worker(self) -> None:
        """Single-consumer worker: serialize shock execution."""
        while True:
            job = await self.queue.get()
            try:
                await self.bound_event.wait()  # guarantee device is ready
                amp_eff = min(job.amp_requested, self.cfg.OWNER_MAX_POWER)

                # controller is set once WS server starts
                await self.controller.shock(job.channel, amp_eff, job.freq, job.copies)  # type: ignore[union-attr]

                resp = {
                    "ok": True,
                    "mode": "pulse",
                    "channel": job.channel,
                    "amp_requested": job.amp_requested,
                    "amp_effective": amp_eff,
                    "owner_max": self.cfg.OWNER_MAX_POWER,
                    "freq": clamp_int(job.freq, 0, self.cfg.FREQ_MAX),
                    "copies": job.copies,
                    "approx_duration_ms": job.copies * 100,
                }
                if not job.done.done():
                    job.done.set_result(resp)

            except (InvalidPulseOperation, PulseDataTooLong) as e:
                if not job.done.done():
                    job.done.set_exception(HTTPException(status_code=400, detail=f"Invalid pulse: {e}"))
            except Exception as e:
                if not job.done.done():
                    job.done.set_exception(HTTPException(status_code=500, detail=f"Shock failed: {e}"))
            finally:
                self.queue.task_done()


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI layer
# ─────────────────────────────────────────────────────────────────────────────
class ShockRequest(BaseModel):
    channel: str = Field(..., description="A or B")
    amp: int = Field(CFG.DEF_AMP, ge=0, le=100, description="0..100 %")
    freq: int = Field(CFG.DEF_FREQ, ge=0, le=CFG.FREQ_MAX, description=f"Hz (0..{CFG.FREQ_MAX})")
    copies: int = Field(CFG.DEF_COPIES, ge=CFG.COPIES_MIN, le=CFG.COPIES_MAX, description="~100 ms per copy")

    @validator("channel")
    def _channel(cls, v: str) -> str:
        v = v.upper()
        if v not in ("A", "B"):
            raise ValueError("channel must be 'A' or 'B'")
        return v


app = FastAPI(title="DG-Lab Coyote API", description=API_DESCRIPTION, version="1.7.0")
server = CoyoteServer(CFG)


@app.on_event("startup")
async def on_startup() -> None:
    server.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await server.stop()


@app.post("/shock")
async def shock(req: ShockRequest):
    # Fast fail if device/app isn’t bound yet (clear error for callers)
    if not server.bound_event.is_set():
        raise HTTPException(status_code=503, detail="Device/app not bound yet (scan QR in DG-Lab app)")
    if not server.controller:
        raise HTTPException(status_code=503, detail="Controller not ready")

    done: asyncio.Future = asyncio.get_running_loop().create_future()
    job = ShockJob(channel=req.channel, amp_requested=req.amp, freq=req.freq, copies=req.copies, done=done)
    await server.queue.put(job)

    try:
        resp: Dict[str, Any] = await done  # either returns dict or raises HTTPException
        return resp
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Shock failed: {e}")
