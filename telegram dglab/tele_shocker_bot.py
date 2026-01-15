#!/usr/bin/env python3
"""
Snow’s DG-Lab Telegram Bot
- Calls your FastAPI /shock endpoint
- Single command: /shock <A|B> <amp> [freq 0..200] [copies 1..100]
- Appends "ouch ⚡❄️" to every reply
- Reports every successful shock to ADMIN_ID with profile photo + deep-link buttons

Dependencies:
  pip install python-telegram-bot httpx
"""

from __future__ import annotations

import os
import asyncio
import logging
from typing import Tuple, Dict, Any

import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
# Your bot token (you asked to keep it set directly)
TELEGRAM_BOT_TOKEN = "bot_token_here"

# FastAPI base URL where /shock lives
API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")
API_SHOCK_URL = f"{API_BASE.rstrip('/')}/shock"

# Admin who receives reports (defaults to your id)
ADMIN_ID = int(os.getenv("ADMIN_ID", "telegram_admin_id_here"))  # e.g., 123456789

# Optional: restrict usage to a single chat (comment out to allow anyone)
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID")  # e.g., "123456789"

# Logging
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("snow-dglab-bot")

# One global async HTTP client (reused for all requests)
_http: httpx.AsyncClient | None = None


# ─────────────────────────────────────────────────────────────────────────────
# UI text helpers
# ─────────────────────────────────────────────────────────────────────────────
def start_text() -> str:
    """Pretty, personal /start message (HTML)."""
    return (
        "❄️✨❄️ <b>Welcome to Snow’s DG-Lab Bot</b> ❄️✨❄️\n"
        "Hey, this is your private control panel for the Coyote 3.0.\n"
        "\n"
        "⭐ Everything runs through <b>Snow’s DG-Lab</b>\n"
        "⭐ Keep it safe and playful — always start low ⚡\n"
        "\n"
        "❄️ <b>Quick Example</b>\n"
        "<pre>/shock A 20 20 5</pre>\n"
        "• Channel <b>A</b>, 20% amplitude, 20 Hz, 5×100ms (~0.5s)\n"
        "\n"
        "❄️ <b>Format</b>\n"
        "<code>/shock &lt;A|B&gt; &lt;amp 0..100&gt; [freq 0..200] [copies 1..100]</code>\n"
        "\n"
        "✨ Each copy ≈ 100 ms\n"
        "✨ Recommended start: 10–20%\n"
    )

def usage() -> str:
    """Concise usage text for /help and validation errors."""
    return (
        "Usage:\n"
        "  /shock <channel> <amp> [freq 0..200] [copies 1..100]\n"
        "Examples:\n"
        "  /shock A 25 20 5       → pulse A, 25% @20Hz, 5×100ms (~0.5s)\n"
        "  /shock B 30            → default freq=20, copies=5\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing & validation
# ─────────────────────────────────────────────────────────────────────────────
def parse_shock_args(args: list[str]) -> Tuple[Dict[str, Any] | None, str | None, dict]:
    """
    Parse: /shock <channel> <amp> [freq] [copies]
    Returns (payload, error_msg, notes)
      - payload: dict for API
      - error_msg: None if ok, otherwise user-friendly string
      - notes: optional info (e.g., {'clamped_freq_from': 300}) to inform user
    """
    notes: dict = {}

    if len(args) < 2:
        return None, "Missing arguments.", notes

    channel = args[0].upper()
    if channel not in ("A", "B"):
        return None, "Channel must be A or B.", notes

    # amp (required)
    try:
        amp = int(args[1])
    except ValueError:
        return None, "amp must be an integer 0..100.", notes
    if not (0 <= amp <= 100):
        return None, "amp must be 0..100.", notes

    # defaults
    freq, copies = 20, 5

    # freq (optional)
    if len(args) >= 3:
        try:
            freq = int(args[2])
        except ValueError:
            return None, "freq must be an integer 0..200.", notes
        if freq < 0:
            return None, "freq must be ≥ 0.", notes
        if freq > 200:
            notes["clamped_freq_from"] = freq
            freq = 200  # clamp to API cap

    # copies (optional)
    if len(args) >= 4:
        try:
            copies = int(args[3])
        except ValueError:
            return None, "copies must be an integer 1..100.", notes
        if not (1 <= copies <= 100):
            return None, "copies must be 1..100.", notes

    payload = {"channel": channel, "amp": amp, "freq": freq, "copies": copies}
    return payload, None, notes


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────
async def http_client() -> httpx.AsyncClient:
    """Get (or build) a shared AsyncClient (keeps connections alive)."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=10.0)
    return _http

async def post_shock(payload: Dict[str, Any]) -> tuple[bool, str, Dict[str, Any]]:
    """
    Call the FastAPI /shock endpoint.
    Returns (ok, message_for_user, raw_json)
    """
    client = await http_client()
    try:
        resp = await client.post(API_SHOCK_URL, json=payload)
        data = resp.json()
    except Exception as e:
        return False, f"Network error: {e}", {}

    if resp.status_code != 200:
        detail = data.get("detail") if isinstance(data, dict) else str(data)
        return False, f"API error ({resp.status_code}): {detail}", data

    # Shape a concise message. The API reports an owner cap and duration estimate.
    ch = data.get("channel")
    msg = (
        f"Pulse {ch}: amp={data.get('amp_effective')} "
        f"(req {data.get('amp_requested')}, owner max {data.get('owner_max')}) "
        f"freq={data.get('freq')} copies={data.get('copies')} "
        f"(~{data.get('approx_duration_ms')}ms)"
    )
    return True, msg, data


# ─────────────────────────────────────────────────────────────────────────────
# Admin reporting
# ─────────────────────────────────────────────────────────────────────────────
async def report_to_admin(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    payload: Dict[str, Any],
    api_data: Dict[str, Any],
) -> None:
    """
    Notify ADMIN_ID of a successful shock with:
      - user id, name, username
      - profile photo (if available)
      - inline buttons with tg deep links
    """
    user = update.effective_user
    chat = update.effective_chat
    uid = user.id if user else None
    username = user.username if (user and user.username) else None
    uname = f"@{username}" if username else "(no username)"
    display = user.full_name if user else "(unknown)"

    ch = payload.get("channel")
    amp = payload.get("amp")
    freq = payload.get("freq")
    copies = payload.get("copies")
    eff = api_data.get("amp_effective")
    owner_max = api_data.get("owner_max")

    # Build deep-link buttons
    buttons = [[InlineKeyboardButton("Open TG Profile (ID)", url=f"tg://user?id={uid}")]]
    if username:
        buttons.append([InlineKeyboardButton("Open TG Profile (Resolve)", url=f"tg://resolve?domain={username}")])
    markup = InlineKeyboardMarkup(buttons)

    caption = (
        "❄️ <b>Shock Report</b>\n"
        f"• User: <b>{display}</b> ({uname})\n"
        f"• User ID: <code>{uid}</code>\n"
        f"• Chat ID: <code>{chat.id if chat else 'n/a'}</code>\n"
        f"• Command: <code>/shock {ch} {amp} {freq} {copies}</code>\n"
        f"• Effective amp: <code>{eff}</code> (owner max {owner_max})\n"
        "ouch ⚡❄️"
    )

    # Try to attach profile photo; otherwise send text only
    try:
        photos = await context.bot.get_user_profile_photos(uid, limit=1)  # type: ignore[arg-type]
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id  # best size
            await context.bot.send_photo(
                chat_id=ADMIN_ID, photo=file_id, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=markup
            )
            return
    except Exception as e:
        log.warning("Profile photo fetch failed for %s: %s", uid, e)

    await context.bot.send_message(
        chat_id=ADMIN_ID, text=caption, parse_mode=ParseMode.HTML,
        reply_markup=markup, disable_web_page_preview=True
    )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram command handlers
# ─────────────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Personal, pretty intro."""
    if ALLOWED_CHAT_ID and str(update.effective_chat.id) != ALLOWED_CHAT_ID:
        return
    await update.message.reply_text(
        start_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage quick reference."""
    if ALLOWED_CHAT_ID and str(update.effective_chat.id) != ALLOWED_CHAT_ID:
        return
    await update.message.reply_text(usage())

async def shock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Parse args → validate/clamp → call API → reply to user.
    Always append "ouch ⚡❄️" for flavor. Report successes to ADMIN_ID.
    """
    if ALLOWED_CHAT_ID and str(update.effective_chat.id) != ALLOWED_CHAT_ID:
        return

    args = context.args or []
    payload, err, notes = parse_shock_args(args)
    if err:
        await update.message.reply_text(f"❌ {err}\n\n" + usage())
        return

    # Optional informative warning if we clamped freq
    if "clamped_freq_from" in notes:
        await update.message.reply_text(
            f"⚠️ Requested freq {notes['clamped_freq_from']} > 200, clamped to 200."
        )

    ok, msg, data = await post_shock(payload)
    if ok:
        await update.message.reply_text(f"✅ {msg}\n\nouch ⚡❄️")
        try:
            await report_to_admin(context, update, payload, data)
        except Exception as e:
            log.warning("Admin report failed: %s", e)
    else:
        await update.message.reply_text(f"❌ {msg}\n\nouch ⚡❄️")


# ─────────────────────────────────────────────────────────────────────────────
# Main entrypoint
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN.")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register commands (v20+ handlers are async by default)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("shock", shock_cmd))

    log.info("Starting Snow’s DG-Lab Bot | API: %s", API_BASE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    finally:
        # Ensure HTTP client is closed when the bot stops
        if _http is not None:
            asyncio.run(_http.aclose())
