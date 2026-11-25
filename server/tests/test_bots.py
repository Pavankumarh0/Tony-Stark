"""bots.py: Telegram and WhatsApp handlers. The pipeline and the network are mocked."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import bots  # noqa: E402

SECRET = "app-secret"
WA_ENV = {"WHATSAPP_TOKEN": "wa-token", "WHATSAPP_PHONE_NUMBER_ID": "123", "WHATSAPP_APP_SECRET": SECRET,
          "WHATSAPP_VERIFY_TOKEN": "verify-me", "WHATSAPP_ALLOWED_NUMBERS": "", "TELEGRAM_ALLOWED_CHATS": ""}


class FakeResponse:
    def __init__(self, data=None, content=b"", status_code=200):
        self._data, self.content, self.status_code, self.text = data or {}, content, status_code, json.dumps(data)

    def json(self):
        return self._data


class FakeClient:
    """Records calls; get() answers by URL substring."""
    def __init__(self, routes=None, **_):
        self.routes, self.posts, self.gets = routes or {}, [], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self.gets.append(url)
        return next((r for k, r in self.routes.items() if k in url), FakeResponse())

    async def post(self, url, **kw):
        self.posts.append((url, kw))
        return FakeResponse({"id": "media-1"})


def signed(body: bytes) -> dict:
    return {"x-hub-signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
            "content-type": "application/json"}


def wa_payload(msg: dict) -> bytes:
    return json.dumps({"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}).encode()


class BotTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, WA_ENV)
        self.env.start()
        self.ask = mock.AsyncMock(return_value={"answer": "ಟೊಮೆಟೊಗೆ ನೀರು ಬೇಕು.", "language": "kn"})
        self.stt = mock.AsyncMock(return_value=("tomato ge neeru beku?", "kn-IN"))
        self.tts = mock.AsyncMock(return_value=b"MP3")
        bots.setup(self.ask, self.stt, self.tts)
        bots._seen.clear()
        app = FastAPI()
        app.include_router(bots.router, prefix="/api/krishi")
        self.c = TestClient(app)

    def tearDown(self):
        self.env.stop()

    # --- shared answer() ---

    def test_text_gets_text_only(self):
        reply, mp3 = asyncio.run(bots.answer("tg:1", "how much water for tomato?"))
        self.assertEqual(reply, "ಟೊಮೆಟೊಗೆ ನೀರು ಬೇಕು.")
        self.assertIsNone(mp3)
        self.ask.assert_awaited_once_with("how much water for tomato?", "tg:1", None)

    def test_voice_note_shows_what_was_heard_and_is_spoken(self):
        reply, mp3 = asyncio.run(bots.answer("tg:1", audio=b"OGG"))
        self.assertTrue(reply.startswith("🎤 tomato ge neeru beku?"))
        self.assertEqual(mp3, b"MP3")
        self.ask.assert_awaited_once_with("tomato ge neeru beku?", "tg:1", "kn-IN")
        self.tts.assert_awaited_once_with("ಟೊಮೆಟೊಗೆ ನೀರು ಬೇಕು.", "kn")

    def test_silent_voice_note_asks_again(self):
        self.stt.return_value = ("", "kn-IN")
        self.assertEqual(asyncio.run(bots.answer("tg:1", audio=b"OGG")), (bots.UNHEARD, None))
        self.ask.assert_not_awaited()

    def test_pipeline_error_is_a_polite_reply(self):
        self.ask.side_effect = RuntimeError("gemma down")
        self.assertEqual(asyncio.run(bots.answer("tg:1", "hi")), (bots.FAILED, None))

    def test_audio_names(self):
        self.assertEqual(bots._audio_name("audio/ogg; codecs=opus"), "voice.ogg")
        self.assertEqual(bots._audio_name("voice/file_3.oga"), "voice.ogg")
        self.assertEqual(bots._audio_name("audio/mpeg"), "voice.mp3")

    # --- WhatsApp webhook ---

    def test_webhook_verification(self):
        q = {"hub.mode": "subscribe", "hub.challenge": "42"}
        self.assertEqual(self.c.get("/api/krishi/whatsapp/webhook", params={**q, "hub.verify_token": "verify-me"}).text,
                         "42")
        self.assertEqual(self.c.get("/api/krishi/whatsapp/webhook", params={**q, "hub.verify_token": "nope"})
                         .status_code, 403)

    def test_unsigned_or_forged_message_is_rejected(self):
        body = wa_payload({"id": "m1", "from": "919900000000", "type": "text", "text": {"body": "hi"}})
        with mock.patch.object(bots, "_whatsapp_message", mock.AsyncMock()) as handle:
            self.assertEqual(self.c.post("/api/krishi/whatsapp/webhook", content=body).status_code, 401)
            bad = {"x-hub-signature-256": "sha256=" + "0" * 64}
            self.assertEqual(self.c.post("/api/krishi/whatsapp/webhook", content=body, headers=bad).status_code, 401)
            handle.assert_not_awaited()

    def test_webhook_is_off_without_credentials(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_APP_SECRET": ""}):
            self.assertEqual(self.c.post("/api/krishi/whatsapp/webhook", content=b"{}").status_code, 503)

    def test_signed_message_is_handled_once(self):
        msg = {"id": "m1", "from": "919900000000", "type": "text", "text": {"body": "hi"}}
        body = wa_payload(msg)
        with mock.patch.object(bots, "_whatsapp_message", mock.AsyncMock()) as handle:
            for _ in range(2):                                   # Meta redelivers
                self.assertEqual(self.c.post("/api/krishi/whatsapp/webhook", content=body,
                                             headers=signed(body)).status_code, 200)
            handle.assert_awaited_once_with(msg)

    def test_whatsapp_text_reply(self):
        fake = FakeClient()
        with mock.patch.object(bots.httpx, "AsyncClient", lambda **kw: fake):
            asyncio.run(bots._whatsapp_message({"id": "m1", "from": "919900000000", "type": "text",
                                                "text": {"body": "water for tomato?"}}))
        url, kw = fake.posts[0]
        self.assertTrue(url.endswith("/123/messages"))
        self.assertEqual(kw["json"]["to"], "919900000000")
        self.assertEqual(kw["json"]["text"]["body"], "ಟೊಮೆಟೊಗೆ ನೀರು ಬೇಕು.")
        self.assertEqual(len(fake.posts), 1)

    def test_whatsapp_voice_note_gets_text_and_audio(self):
        fake = FakeClient({"/media-9": FakeResponse({"url": "https://lookaside.example/f", "mime_type": "audio/ogg"}),
                           "lookaside": FakeResponse(content=b"OGG")})
        with mock.patch.object(bots.httpx, "AsyncClient", lambda **kw: fake):
            asyncio.run(bots._whatsapp_message({"id": "m2", "from": "919900000000", "type": "audio",
                                                "audio": {"id": "media-9"}}))
        self.stt.assert_awaited_once_with(b"OGG", "voice.ogg")
        kinds = [kw.get("json", {}).get("type") for _, kw in fake.posts]
        self.assertEqual(kinds, ["text", None, "audio"])         # reply, media upload, voice reply

    def test_whatsapp_allow_list(self):
        fake = FakeClient()
        with mock.patch.dict(os.environ, {"WHATSAPP_ALLOWED_NUMBERS": "911111111111"}), \
             mock.patch.object(bots.httpx, "AsyncClient", lambda **kw: fake):
            asyncio.run(bots._whatsapp_message({"id": "m3", "from": "919900000000", "type": "text",
                                                "text": {"body": "hi"}}))
        self.assertEqual(fake.posts, [])
        self.ask.assert_not_awaited()

    # --- Telegram ---

    def test_telegram_start_gets_welcome(self):
        fake = FakeClient()
        asyncio.run(bots._telegram_message(fake, "https://api.telegram.org/botT", {
            "message_id": 5, "chat": {"id": 77}, "text": "/start"}))
        self.assertEqual(fake.posts[0][1]["json"]["text"], bots.WELCOME)
        self.ask.assert_not_awaited()

    def test_telegram_voice_note(self):
        fake = FakeClient({"/getFile": FakeResponse({"result": {"file_path": "voice/file_1.oga"}}),
                           "/file/botT/": FakeResponse(content=b"OGG")})
        asyncio.run(bots._telegram_message(fake, "https://api.telegram.org/botT", {
            "message_id": 6, "chat": {"id": 77}, "voice": {"file_id": "F1", "file_size": 9000}}))
        self.stt.assert_awaited_once_with(b"OGG", "voice.ogg")
        urls = [u.rsplit("/", 1)[-1] for u, _ in fake.posts]
        self.assertEqual(urls, ["sendChatAction", "sendMessage", "sendVoice"])
        self.ask.assert_awaited_once_with("tomato ge neeru beku?", "tg:77", "kn-IN")

    def test_bot_token_is_hidden_in_logs(self):
        import logging
        rec = logging.LogRecord("httpx", logging.INFO, "", 0, "HTTP Request: %s %s", (
            "GET", "https://api.telegram.org/bot123:SECRET/getUpdates"), None)
        bots._HideBotToken().filter(rec)
        self.assertNotIn("SECRET", rec.getMessage())


if __name__ == "__main__":
    unittest.main()
