"""vision.py: the crop photo report. The vision model and Sarvam are mocked."""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vision  # noqa: E402

DIAG = {"is_crop": True, "crop": "tomato", "health": "moderate", "problem": "early blight",
        "confidence": "high", "what_happened": "Brown rings on the lower leaves.",
        "why": "A fungus that spreads in warm, wet weather.", "treatment": ["Remove the spotted leaves."],
        "improve": ["Water the soil, not the leaves.", "Rotate crops."], "when_to_get_help": "If it spreads."}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class VisionTests(unittest.TestCase):
    def test_parse_fenced_json(self):
        d = vision._parse("```json\n" + json.dumps({**DIAG, "health": "Moderate problem"}) + "\n```")
        self.assertEqual(d["health"], "moderate")
        self.assertEqual(d["treatment"], ["Remove the spotted leaves."])

    def test_three_reports_in_order_with_translations(self):
        async def fake(text, lang, style):
            self.assertEqual(style, "native")
            return f"<{lang}>{text}", ""
        with mock.patch("language.from_english", fake):
            r = run(vision.reports(DIAG))
        self.assertEqual(list(r), ["en", "kn", "hi"])
        self.assertIn("early blight", r["en"])
        self.assertIn("*Treatment now:*\n1. Remove the spotted leaves.", r["en"])
        self.assertIn("ಬೆಳೆ ಫೋಟೋ ವರದಿ", r["kn"])
        self.assertIn("<kn>early blight", r["kn"])
        self.assertIn("ಮಧ್ಯಮ ಸಮಸ್ಯೆ", r["kn"])
        self.assertIn("<hi>Rotate crops.", r["hi"])
        self.assertIn("फसल फोटो रिपोर्ट", r["hi"])

    def test_not_a_crop(self):
        r = run(vision.reports({"is_crop": False}))
        self.assertIn("could not see a crop", r["en"])
        self.assertEqual(set(r), {"en", "kn", "hi"})

    def test_falls_back_to_the_next_model(self):
        calls = []

        async def gem(c, model, prompt, image, mime):
            calls.append(model)
            if model == "m1":
                raise vision.VisionError("HTTP 429")
            return json.dumps(DIAG)
        env = {"GEMINI_API_KEY": "k", "VISION_MODEL": "m1", "VISION_FALLBACK_MODEL": "m2", "VISION_OLLAMA_MODEL": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(vision, "_gemini", gem):
            d = run(vision.diagnose(b"img", "image/jpeg", "leaves yellow", "soil 20%"))
        self.assertEqual(calls, ["m1", "m2"])
        self.assertEqual(d["model"], "m2")

    def test_all_models_fail_gives_the_sorry_message(self):
        async def bad(*a):
            raise vision.VisionError("down")
        env = {"GEMINI_API_KEY": "k", "VISION_OLLAMA_MODEL": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(vision, "_gemini", bad):
            texts, summary = run(vision.photo_reports(b"img"))
        self.assertEqual(summary, "")
        self.assertEqual(texts, vision.FAILED)

    def test_summary_for_follow_up_questions(self):
        s = vision.english_summary(DIAG)
        self.assertIn("tomato", s)
        self.assertIn("early blight", s)


if __name__ == "__main__":
    unittest.main()
