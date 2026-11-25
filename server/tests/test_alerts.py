"""alerts.py: the flood alert call. Vobiz and edge-tts are mocked."""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import alerts  # noqa: E402

ENV = {"ALERT_CALL_ENABLED": "1", "ALERT_WATER_PERCENT": "70", "ALERT_COOLDOWN_MIN": "30",
       "VOBIZ_AUTH_ID": "MA_TEST", "VOBIZ_AUTH_TOKEN": "tok", "VOBIZ_FROM": "+12402128725",
       "ALERT_CALL_TO": "+918904030441", "ALERT_PUBLIC_URL": "https://example.trycloudflare.com/"}


def reading(pct, valid=True):
    return {"water_level_percent": pct, "water_level_valid": valid}


class FloodAlertTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        alerts._state.update(above=False, last_call=0.0)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.call = mock.patch.object(alerts, "place_call", mock.AsyncMock(return_value=True))
        self.placed = self.call.start()

    def tearDown(self):
        self.call.stop()
        self.env.stop()
        self.loop.run_until_complete(asyncio.sleep(0))
        self.loop.close()

    def check(self, pct, t, valid=True):
        return alerts.check(reading(pct, valid), now=t)

    def test_calls_when_the_level_rises_above_70(self):
        self.assertFalse(self.check(65, 1000))
        self.assertTrue(self.check(75, 1005))

    def test_no_repeat_while_high_until_the_cooldown(self):
        self.assertTrue(self.check(90, 1000))
        self.assertFalse(self.check(92, 1005))             # still high: no second call
        self.assertFalse(self.check(92, 1000 + 29 * 60))
        self.assertTrue(self.check(92, 1000 + 31 * 60))    # 30 minutes later, still flooding: call again

    def test_falling_and_rising_again_calls_again(self):
        self.assertTrue(self.check(90, 1000))
        self.assertFalse(self.check(40, 1100))
        self.assertTrue(self.check(85, 1200))

    def test_exactly_70_or_invalid_does_not_call(self):
        self.assertFalse(self.check(70, 1000))
        self.assertFalse(self.check(95, 1000, valid=False))

    def test_switched_off_does_not_call(self):
        with mock.patch.dict(os.environ, {"ALERT_CALL_ENABLED": "0"}):
            self.assertFalse(self.check(95, 1000))

    def test_vobiz_request(self):
        self.call.stop()
        sent = {}

        class Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None, json=None):
                sent.update(url=url, headers=headers, json=json)
                return mock.Mock(status_code=200, json=lambda: {"request_uuid": "u1"}, text="")

        with mock.patch.object(alerts.httpx, "AsyncClient", Client), \
             mock.patch.object(alerts, "_make_audio", mock.AsyncMock()):
            self.assertTrue(self.loop.run_until_complete(alerts.place_call()))
        self.call.start()
        self.assertEqual(sent["url"], "https://api.vobiz.ai/api/v1/Account/MA_TEST/Call/")
        self.assertEqual(sent["headers"], {"X-Auth-ID": "MA_TEST", "X-Auth-Token": "tok"})
        self.assertEqual(sent["json"]["to"], "918904030441")
        self.assertEqual(sent["json"]["from"], "12402128725")
        self.assertEqual(sent["json"]["answer_url"], "https://example.trycloudflare.com/api/krishi/alert/answer")

    def test_answer_plays_kannada_then_english(self):
        alerts._audio.update(kn=b"KN", en=b"EN")
        r = self.loop.run_until_complete(alerts.alert_answer())
        xml = r.body.decode()
        kn = xml.index("/api/krishi/alert/audio/kn.mp3")
        en = xml.index("/api/krishi/alert/audio/en.mp3")
        self.assertLess(kn, en)
        self.assertTrue(xml.startswith('<?xml') and "<Response>" in xml and "<Hangup/>" in xml)
        self.assertIn("ರಾಮಣ್ಣ", alerts.MESSAGES["kn"])


if __name__ == "__main__":
    unittest.main()
