"""Esse — Claude'da (ESSAY_PROVIDER), yozma javoblar — Gemini'da (AI_PROVIDER). Tarmoqqa chiqmaydi: mijoz soxta."""
import json
import types
import unittest

import _env  # noqa: F401

import anthropic

import essay_ai

ANSWER = {
    "criteria": [{"key": key, "score": 2 if i % 3 else 1, "comment": "Izoh."} for i, key in enumerate(essay_ai.RUBRIC_KEYS)],
    "has_outline_or_epigraph": False,
    "overall_comment": "Yaxshi esse.",
}


class FakeClient:
    """anthropic.AsyncAnthropic o'rnida: so'rovni yozib oladi, tayyor javob qaytaradi."""
    calls = []

    def __init__(self, api_key=None):
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self.create))

    async def create(self, **params):
        FakeClient.calls.append(params)
        return types.SimpleNamespace(
            stop_reason="end_turn",
            content=[types.SimpleNamespace(type="thinking", thinking=""),
                     types.SimpleNamespace(type="text", text=json.dumps(ANSWER))],
            usage=types.SimpleNamespace(input_tokens=2000, output_tokens=3000,
                                        cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

    async def close(self):
        pass


class EssayOnClaude(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = (essay_ai.AI_PROVIDER, essay_ai.ESSAY_PROVIDER, essay_ai.ANTHROPIC_API_KEY,
                      essay_ai.CLAUDE_MODEL, anthropic.AsyncAnthropic)
        essay_ai.AI_PROVIDER, essay_ai.ESSAY_PROVIDER = "gemini", "claude"
        essay_ai.ANTHROPIC_API_KEY, essay_ai.CLAUDE_MODEL = "test-key", "claude-sonnet-5-5"
        anthropic.AsyncAnthropic = FakeClient
        FakeClient.calls = []

    def tearDown(self):
        (essay_ai.AI_PROVIDER, essay_ai.ESSAY_PROVIDER, essay_ai.ANTHROPIC_API_KEY,
         essay_ai.CLAUDE_MODEL, anthropic.AsyncAnthropic) = self.saved

    def test_providers_are_separate(self):
        self.assertTrue(essay_ai.enabled() and essay_ai.essay_enabled())
        self.assertEqual((essay_ai.provider_name(), essay_ai.essay_provider_name()), ("Gemini", "Claude"))

    async def test_text_essay_graded_by_claude_sonnet(self):
        essay = "Ijtimoiy tarmoqlar haqida esse. " * 20
        result = await essay_ai.grade("Mavzu", essay)
        params = FakeClient.calls[0]
        self.assertEqual(params["model"], "claude-sonnet-5-5")
        self.assertEqual(params["output_config"]["effort"], "high")
        schema = params["output_config"]["format"]["schema"]
        self.assertNotIn("transcription", schema["properties"])  # matnni qayta ko'chirmaydi
        self.assertEqual(params["fallbacks"], "default")
        self.assertNotIn("thinking", params)  # Sonnet 5.5: adaptive (standart)
        self.assertEqual(result["score"], sum(c["score"] for c in ANSWER["criteria"]))
        self.assertEqual(result["provider"], "Claude")
        self.assertEqual(result["transcription"], essay.strip())
        self.assertAlmostEqual(result["cost"], 2000 * 2 / 1e6 + 3000 * 10 / 1e6, places=5)  # Sonnet narxi

    async def test_photo_essay_keeps_transcription_field(self):
        await essay_ai.grade("Mavzu", None, [(b"\xff\xd8 rasm", "image/jpeg")])
        schema = FakeClient.calls[0]["output_config"]["format"]["schema"]
        self.assertIn("transcription", schema["properties"])

    async def test_refusal_is_reported(self):
        async def refuse(**params):
            return types.SimpleNamespace(stop_reason="refusal", content=[], usage=None)

        class Refusing(FakeClient):
            def __init__(self, api_key=None):
                self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=refuse))

        anthropic.AsyncAnthropic = Refusing
        with self.assertRaises(essay_ai.AIError):
            await essay_ai.grade("Mavzu", "Matn " * 50)


if __name__ == "__main__":
    unittest.main()
