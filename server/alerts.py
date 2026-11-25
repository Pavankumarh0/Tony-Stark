"""Flood alert: a phone call when the water level sensor goes above the limit.

Each reading from the UNO Q passes through check(). When the water level rises
above ALERT_WATER_PERCENT (default 70), a call is placed through Vobiz to
ALERT_CALL_TO. Vobiz then fetches /api/krishi/alert/answer, which plays the
Kannada message and then the English one. Vobiz's own text-to-speech has no
Kannada voice, so both messages are MP3s made with the kiosk's edge-tts voices.

While the level stays high, the call repeats at most once per
ALERT_COOLDOWN_MIN minutes (default 30); after the level falls back below the
limit, the next rise calls straight away.

Settings (.env): ALERT_CALL_ENABLED=1, VOBIZ_AUTH_ID, VOBIZ_AUTH_TOKEN,
VOBIZ_FROM (the Vobiz number), ALERT_CALL_TO, ALERT_PUBLIC_URL (the public
https address of this server, which Vobiz must reach: the tunnel).
"""
import asyncio, logging, os, time

import httpx
from fastapi import APIRouter, Response

log = logging.getLogger("krishi.alerts")
router = APIRouter()

MESSAGES = {
    "kn": ("ರಾಮಣ್ಣ, ನಿಮ್ಮ ತೋಟದಲ್ಲಿ ನೀರಿನ ಮಟ್ಟ ಹೆಚ್ಚಾಗಿದ್ದು, ಪ್ರವಾಹ ಉಂಟಾಗುವ ಸಾಧ್ಯತೆ ಇದೆ. ದಯವಿಟ್ಟು ತಕ್ಷಣ ಎಚ್ಚರಿಕೆ "
           "ವಹಿಸಿ ಮತ್ತು ಅಗತ್ಯ ಮುನ್ನೆಚ್ಚರಿಕೆ ಕ್ರಮಗಳನ್ನು ಕೈಗೊಳ್ಳಿ."),
    "en": ("Ramanna, the water level in your farm has increased, and there is a possibility of flooding. "
           "Please stay alert and take the necessary precautionary measures immediately."),
}
VOICES = {"kn": ("TTS_VOICE_KN", "kn-IN-SapnaNeural"), "en": ("TTS_VOICE", "en-IN-NeerjaNeural")}

_audio: dict[str, bytes] = {}          # language -> MP3, made once
_state = {"above": False, "last_call": 0.0}
_tasks: set = set()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def enabled() -> bool:
    return _env("ALERT_CALL_ENABLED") == "1" and all(
        _env(k) for k in ("VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN", "VOBIZ_FROM", "ALERT_CALL_TO", "ALERT_PUBLIC_URL"))


def _digits(number: str) -> str:
    """E.164 without the +, as the Vobiz docs show it (14155551234)."""
    return "".join(ch for ch in number if ch.isdigit())


def check(reading: dict, now: float | None = None) -> bool:
    """Called with every reading. Returns True when it started a call."""
    pct = reading.get("water_level_percent") if reading.get("water_level_valid") else None
    if pct is None:
        return False
    now = now or time.time()
    limit = float(_env("ALERT_WATER_PERCENT", "70"))
    if pct <= limit:
        _state["above"] = False
        return False
    rising = not _state["above"]
    _state["above"] = True
    cooldown = float(_env("ALERT_COOLDOWN_MIN", "30")) * 60
    if not enabled() or (not rising and now - _state["last_call"] < cooldown) or now - _state["last_call"] < 60:
        return False
    _state["last_call"] = now
    log.warning("water level %.0f%% is above %.0f%%: calling %s", pct, limit, _env("ALERT_CALL_TO"))
    task = asyncio.get_event_loop().create_task(place_call())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def place_call() -> bool:
    """Ask Vobiz to ring ALERT_CALL_TO; the call then fetches the answer XML below."""
    auth_id = _env("VOBIZ_AUTH_ID")
    await _make_audio()                         # ready before Vobiz asks for it
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/",
                             headers={"X-Auth-ID": auth_id, "X-Auth-Token": _env("VOBIZ_AUTH_TOKEN")},
                             json={"from": _digits(_env("VOBIZ_FROM")), "to": _digits(_env("ALERT_CALL_TO")),
                                   "answer_url": _env("ALERT_PUBLIC_URL").rstrip("/") + "/api/krishi/alert/answer",
                                   "answer_method": "POST"})
    except httpx.HTTPError as e:
        log.error("flood alert call failed: %s", e)
        return False
    if r.status_code >= 300:
        log.error("flood alert call refused: HTTP %d %s", r.status_code, r.text[:300])
        return False
    log.info("flood alert call placed: %s", r.json().get("request_uuid"))
    return True


async def _make_audio():
    import speech
    for lang, text in MESSAGES.items():
        if lang not in _audio:
            env, default = VOICES[lang]
            try:
                _audio[lang] = await speech.synth_mp3(text, _env(env, default))
            except Exception as e:
                log.error("could not make the %s alert audio: %s", lang, e)


@router.api_route("/alert/answer", methods=["GET", "POST"])
async def alert_answer():
    """What the call plays: the Kannada alert, then the English one."""
    await _make_audio()
    base = _env("ALERT_PUBLIC_URL").rstrip("/") + "/api/krishi/alert/audio"
    plays = "".join(f"<Play>{base}/{lang}.mp3</Play>" for lang in MESSAGES if lang in _audio)
    if not plays:                       # no audio could be made: at least say it in English
        plays = f'<Speak voice="WOMAN" language="en-IN">{MESSAGES["en"]}</Speak>'
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{plays}<Hangup/></Response>'
    return Response(xml, media_type="application/xml")


@router.get("/alert/audio/{lang}.mp3")
async def alert_audio(lang: str):
    await _make_audio()
    if lang not in _audio:
        return Response(status_code=404)
    return Response(_audio[lang], media_type="audio/mpeg")
