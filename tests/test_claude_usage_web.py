import base64
import json
import unittest

import claude_usage_web as app


def jwt_with_payload(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class UtilityTests(unittest.TestCase):
    def test_fmt_window(self):
        self.assertEqual(app.fmt_window(18_000), "5h")
        self.assertEqual(app.fmt_window(604_800), "7d")
        self.assertIsNone(app.fmt_window(None))

    def test_to_epoch_accepts_seconds_milliseconds_and_iso(self):
        self.assertEqual(app.to_epoch(1_700_000_000), 1_700_000_000)
        self.assertEqual(app.to_epoch(1_700_000_000_000), 1_700_000_000)
        self.assertEqual(app.to_epoch("1970-01-01T00:00:01Z"), 1)
        self.assertIsNone(app.to_epoch("not-a-date"))

    def test_decode_jwt_claims(self):
        token = jwt_with_payload({app.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": "acct_123"}})
        claims = app.decode_jwt_claims(token)
        self.assertEqual(claims[app.OPENAI_AUTH_CLAIM]["chatgpt_account_id"], "acct_123")
        self.assertEqual(app.decode_jwt_claims("invalid"), {})

    def test_codex_window_clamps_percentage(self):
        window = app._codex_window(
            {"used_percent": 120, "limit_window_seconds": 18_000, "reset_at": 100},
            "Session",
            derive=True,
        )
        self.assertEqual(window["label"], "Session (5h)")
        self.assertEqual(window["percent"], 100.0)

    def test_demo_payload_has_both_providers(self):
        providers = app.demo_providers()
        self.assertEqual([provider["id"] for provider in providers], ["claude", "codex"])
        self.assertTrue(all(provider["ok"] for provider in providers))


if __name__ == "__main__":
    unittest.main()
