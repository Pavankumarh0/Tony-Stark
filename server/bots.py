"""Telegram and WhatsApp bots: the kiosk's answers, in the farmer's chat app.

A farmer types a question or sends a voice note in Kannada, Hindi or English.
The reply comes back as text in the same language, and a voice note also gets
a spoken reply. Each chat is its own session, so follow-up questions work.

Telegram  long polling (getUpdates): no public URL needed, it works behind
          any home or campus network. Set TELEGRAM_BOT_TOKEN (from @BotFather).
WhatsApp  Meta WhatsApp Cloud API webhook at /api/krishi/whatsapp/webhook.
          Meta must reach it over HTTPS (a tunnel such as cloudflared or ngrok
          when the server runs on a PC). Set WHATSAPP_TOKEN,
          WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN and
          WHATSAPP_APP_SECRET; every POST must carry Meta's signature.

app.py hands the pipeline in with setup():
    ask(text, session, heard_lang)  -> the localized reply dict (as /chat)
    transcribe(audio, filename)     -> (text, language code)
    speak(text, lang)               -> MP3 bytes
"""
import asyncio, hashlib, hmac, json, logging, os, re
from collections import OrderedDict

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

log = logging.getLogger("krishi.bots")
router = APIRouter()

WELCOME = ("Namaskara! I am Smart Krishi, your farming assistant. Ask me about crops, soil, fertilizer, "
           "pests, weather or mandi prices. Type your question or send a voice note in Kannada, Hindi or English.")
UNHEARD = "Sorry, I couldn't hear that clearly. Please send the voice note again, or type your question."
FAILED = "Sorry, something went wrong while answering. Please try again in a little while."
MAX_TEXT = 4000                   # both apps cap one message at 4096 characters
MAX_AUDIO = 16 * 1024 * 1024      # longer voice notes are refused, not transcribed

_ask = _transcribe = _speak = None
_tasks: set = set()               # running Telegram handlers (kept so they are not garbage collected)
_seen: "OrderedDict[str, None]" = OrderedDict()   # WhatsApp message ids already handled (Meta redelivers)


def setup(ask, transcribe, speak):
    global _ask, _transcribe, _speak
    _ask, _transcribe, _speak = ask, transcribe, speak


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _allowed(var: str, who: str) -> bool:
    """An empty allow-list lets everyone in."""
    allow = {x.strip() for x in _env(var).split(",") if x.strip()}
    return not allow or who in allow


def _audio_name(kind: str) -> str:
    """A filename whose extension tells speech-to-text the format ("audio/ogg; codecs=opus", "x.oga")."""
    kind = kind.lower()
    for key, ext in (("ogg", "ogg"), ("oga", "ogg"), ("opus", "ogg"), ("mpeg", "mp3"), ("mp3", "mp3"),
                     ("mp4", "m4a"), ("m4a", "m4a"), ("aac", "m4a"), ("wav", "wav"), ("webm", "webm")):
        if key in kind:
            return f"voice.{ext}"
    return "voice.ogg"


async def answer(session: str, text: str = "", audio: bytes | None = None,
                 filename: str = "voice.ogg") -> tuple[str, bytes | None]:
    """(reply text, MP3 of the reply or None). Only a voice note gets the MP3."""
    heard = None
    if audio is not None:
        try:
            text, heard = await _transcribe(audio, filename)
        except Exception as e:
            log.warning("bot speech-to-text failed: %s", e)
            return FAILED, None
        if not text:
            return UNHEARD, None
        log.info("%s heard (%s): %r", session, heard, text)
    try:
        r = await _ask(text, session, heard)
    except Exception:
        log.exception("bot answer failed")
        return FAILED, None
    if audio is None:
        return r["answer"][:MAX_TEXT], None
    reply = f"🎤 {text}\n\n{r['answer']}"[:MAX_TEXT]      # show what was heard, so a mishearing is visible
    try:
        return reply, await _speak(r["answer"], r.get("language", "en"))
    except Exception as e:
        log.warning("bot TTS failed: %s", e)
        return reply, None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def telegram_on() -> bool:
    return bool(_env("TELEGRAM_BOT_TOKEN"))


class _HideBotToken(logging.Filter):
    """httpx logs every request URL, and a Telegram URL contains the bot token."""
    def filter(self, rec):
        if rec.args and isinstance(rec.args, tuple):
            rec.args = tuple(re.sub(r"/bot[^/]+/", "/bot<token>/", str(a)) if "api.telegram.org" in str(a) else a
                             for a in rec.args)
        return True


logging.getLogger("httpx").addFilter(_HideBotToken())


async def telegram_loop():
    base = f"https://api.telegram.org/bot{_env('TELEGRAM_BOT_TOKEN')}"
    offset = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, read=45)) as c:
        try:
            await c.post(f"{base}/deleteWebhook")        # getUpdates is refused while a webhook is set
        except httpx.HTTPError:
            pass
        log.info("Telegram bot: polling for messages")
        while True:
            try:
                r = await c.get(f"{base}/getUpdates", params={"timeout": 30, "allowed_updates": '["message"]',
                                                                 **({"offset": offset} if offset else {})})
                if r.status_code == 401:
                    log.error("Telegram bot: TELEGRAM_BOT_TOKEN was rejected; the bot is off")
                    return
                if r.status_code == 409:
                    log.warning("Telegram bot: another copy of this bot is polling; retrying in 10 s")
                    await asyncio.sleep(10)
                    continue
                for u in r.json().get("result", []):
                    offset = u["update_id"] + 1
                    if u.get("message"):
                        t = asyncio.create_task(_telegram_message(c, base, u["message"]))
                        _tasks.add(t)
                        t.add_done_callback(_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Telegram polling error: %s", e)
                await asyncio.sleep(5)


async def _telegram_message(c: httpx.AsyncClient, base: str, m: dict):
    chat = m["chat"]["id"]
    if not _allowed("TELEGRAM_ALLOWED_CHATS", str(chat)):
        return
    text = (m.get("text") or "").strip()
    voice = m.get("voice") or m.get("audio")
    send = lambda t: c.post(f"{base}/sendMessage", json={"chat_id": chat, "text": t,
                                                          "reply_parameters": {"message_id": m["message_id"]}})
    try:
        if text.startswith(("/start", "/help")) or (not text and not voice):
            await send(WELCOME)
            return
        if voice and voice.get("file_size", 0) > MAX_AUDIO:
            await send("That voice note is too long. Please send a shorter one, under a minute.")
            return
        await c.post(f"{base}/sendChatAction", json={"chat_id": chat, "action": "typing"})
        audio, name = None, "voice.ogg"
        if voice:
            f = (await c.get(f"{base}/getFile", params={"file_id": voice["file_id"]})).json()["result"]
            audio = (await c.get(f"{base.replace('/bot', '/file/bot', 1)}/{f['file_path']}")).content
            name = _audio_name(voice.get("mime_type") or f["file_path"])
        reply, mp3 = await answer(f"tg:{chat}", text, audio, name)
        await send(reply)
        if mp3:
            await c.post(f"{base}/sendVoice", data={"chat_id": str(chat)},
                         files={"voice": ("answer.mp3", mp3, "audio/mpeg")})
    except Exception as e:
        log.warning("Telegram reply to chat %s failed: %s", chat, e)


# ---------------------------------------------------------------------------
# WhatsApp (Meta Cloud API)
# ---------------------------------------------------------------------------

def whatsapp_on() -> bool:
    return all(_env(k) for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET"))


def _graph() -> str:
    return f"https://graph.facebook.com/{_env('WHATSAPP_API_VERSION') or 'v21.0'}"


@router.get("/whatsapp/webhook")
async def whatsapp_verify(request: Request):
    """Meta's one-time check when the webhook URL is saved in the app dashboard."""
    p, expected = request.query_params, _env("WHATSAPP_VERIFY_TOKEN")
    if p.get("hub.mode") == "subscribe" and expected and hmac.compare_digest(p.get("hub.verify_token", ""), expected):
        return Response(p.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verify token does not match WHATSAPP_VERIFY_TOKEN.")


@router.post("/whatsapp/webhook")
async def whatsapp_receive(request: Request, tasks: BackgroundTasks):
    """Incoming messages. Answered in the background: Meta retries a webhook that is slow to return 200."""
    if not whatsapp_on():
        raise HTTPException(status_code=503, detail="WhatsApp bot is off: set WHATSAPP_TOKEN, "
                                                    "WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_APP_SECRET in .env.")
    body = await request.body()
    good = "sha256=" + hmac.new(_env("WHATSAPP_APP_SECRET").encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), good):
        raise HTTPException(status_code=401, detail="Bad signature.")
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON.")
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages") or []:     # status updates carry no messages
                mid = m.get("id", "")
                if mid in _seen:
                    continue
                _seen[mid] = None
                while len(_seen) > 500:
                    _seen.popitem(last=False)
                tasks.add_task(_whatsapp_message, m)
    return {"ok": True}


async def _whatsapp_send(c: httpx.AsyncClient, to: str, payload: dict):
    r = await c.post(f"{_graph()}/{_env('WHATSAPP_PHONE_NUMBER_ID')}/messages",
                     json={"messaging_product": "whatsapp", "to": to, **payload})
    if r.status_code >= 400:
        log.warning("WhatsApp send to %s failed: HTTP %d %s", to, r.status_code, r.text[:300])


async def _whatsapp_message(m: dict):
    who, kind = m.get("from", ""), m.get("type")
    if not _allowed("WHATSAPP_ALLOWED_NUMBERS", who):
        return
    async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {_env('WHATSAPP_TOKEN')}"}) as c:
        try:
            text, audio, name = "", None, "voice.ogg"
            if kind == "text":
                text = (m.get("text", {}).get("body") or "").strip()
            elif kind == "audio":
                media = (await c.get(f"{_graph()}/{m['audio']['id']}")).json()
                if media.get("file_size", 0) > MAX_AUDIO:
                    await _whatsapp_send(c, who, {"type": "text", "text": {"body": "That voice note is too long. "
                                                                           "Please send a shorter one."}})
                    return
                audio = (await c.get(media["url"])).content
                name = _audio_name(media.get("mime_type") or m["audio"].get("mime_type", ""))
            if not text and audio is None:
                await _whatsapp_send(c, who, {"type": "text", "text": {"body": WELCOME}})
                return
            reply, mp3 = await answer(f"wa:{who}", text, audio, name)
            await _whatsapp_send(c, who, {"type": "text", "text": {"body": reply}, "context": {"message_id": m["id"]}})
            if mp3:
                up = await c.post(f"{_graph()}/{_env('WHATSAPP_PHONE_NUMBER_ID')}/media",
                                  data={"messaging_product": "whatsapp", "type": "audio/mpeg"},
                                  files={"file": ("answer.mp3", mp3, "audio/mpeg")})
                if up.status_code < 400:
                    await _whatsapp_send(c, who, {"type": "audio", "audio": {"id": up.json()["id"]}})
                else:
                    log.warning("WhatsApp audio upload failed: HTTP %d %s", up.status_code, up.text[:300])
        except Exception as e:
            log.warning("WhatsApp reply to %s failed: %s", who, e)
