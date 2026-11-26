import base64
import configparser
import contextlib
import io
import json
import os
import plistlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None

import claude_usage_web as app

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_PROVIDER_KEYS = {
    "id",
    "name",
    "ok",
    "error",
    "code",
    "arg",
    "source",
    "plan",
    "windows",
}


def jwt_with_payload(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def make_args(**overrides):
    defaults = {
        "port": 8787,
        "host": "127.0.0.1",
        "interval": 120,
        "refresh_cooldown": app.REFRESH_COOLDOWN_SEC,
        "only": "both",
        "claude_token": None,
        "codex_token": None,
        "demo": False,
        "theme": "midnight",
        "print_systemd": False,
        "print_launchd": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeClock:
    def __init__(self, start=1000.0):
        self._now = float(start)
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._now

    def advance(self, seconds):
        with self._lock:
            self._now += float(seconds)
            return self._now


class UtilityAndSchemaTests(unittest.TestCase):
    def test_fmt_window(self):
        self.assertEqual(app.fmt_window(18_000), "5h")
        self.assertEqual(app.fmt_window(604_800), "7d")
        self.assertEqual(app.fmt_window(5_400), "2h")
        self.assertIsNone(app.fmt_window(None))
        self.assertIsNone(app.fmt_window(0))

    def test_to_epoch_accepts_seconds_milliseconds_and_iso(self):
        self.assertEqual(app.to_epoch(1_700_000_000), 1_700_000_000)
        self.assertEqual(app.to_epoch(1_700_000_000_000), 1_700_000_000)
        self.assertEqual(app.to_epoch("1700000000"), 1_700_000_000)
        self.assertEqual(app.to_epoch("1970-01-01T00:00:01Z"), 1)
        self.assertIsNone(app.to_epoch(""))
        self.assertIsNone(app.to_epoch(None))
        self.assertIsNone(app.to_epoch("not-a-date"))

    def test_decode_jwt_claims(self):
        token = jwt_with_payload({app.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": "acct_123"}})
        claims = app.decode_jwt_claims(token)
        self.assertEqual(claims[app.OPENAI_AUTH_CLAIM]["chatgpt_account_id"], "acct_123")
        self.assertEqual(app.decode_jwt_claims("invalid"), {})

    def test_normalize_source_and_plan(self):
        self.assertEqual(app.normalize_source("  ~/.codex/auth.json  "), "~/.codex/auth.json")
        self.assertIsNone(app.normalize_source("   "))
        self.assertIsNone(app.normalize_source(None))
        self.assertEqual(app.normalize_plan("  Plus "), "plus")
        self.assertIsNone(app.normalize_plan(""))
        self.assertIsNone(app.normalize_plan(None))

    def test_codex_window_clamps_percentage_and_derives_name(self):
        window = app._codex_window(
            {"used_percent": 120, "limit_window_seconds": 18_000, "reset_at": 100},
            "Session",
            derive=True,
        )
        self.assertEqual(window["label"], "Session (5h)")
        self.assertEqual(window["kind"], "session")
        self.assertEqual(window["percent"], 100.0)

        weekly = app._codex_window(
            {"usedPercent": -5, "windowSeconds": 604_800, "reset_after_seconds": 60},
            "Session",
            derive=True,
        )
        self.assertEqual(weekly["label"], "Weekly (7d)")
        self.assertEqual(weekly["kind"], "weekly")
        self.assertEqual(weekly["percent"], 0.0)
        self.assertIsInstance(weekly["reset_epoch"], int)

    def test_demo_and_error_payloads_share_normalized_schema(self):
        providers = app.demo_providers()
        self.assertEqual([p["id"] for p in providers], ["claude", "codex"])
        for provider in providers:
            self.assertEqual(set(provider.keys()), EXPECTED_PROVIDER_KEYS)
            self.assertTrue(provider["ok"])
            self.assertEqual(provider["source"], "demo")
            self.assertIsNone(provider["plan"])

        err = app.provider_error(
            "codex", "Codex", "錯誤", "http", 502, source="  --codex-token ", plan=" PRO "
        )
        self.assertEqual(set(err.keys()), EXPECTED_PROVIDER_KEYS)
        self.assertFalse(err["ok"])
        self.assertEqual(err["source"], "--codex-token")
        self.assertEqual(err["plan"], "pro")


class FetchClaudeTests(unittest.TestCase):
    def test_missing_credentials_returns_normalized_error(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(app, "CLAUDE_CREDENTIALS", Path("/nonexistent/claude-creds.json")), \
             mock.patch.object(app.sys, "platform", "linux"):
            res = app.fetch_claude()
        self.assertEqual(set(res.keys()), EXPECTED_PROVIDER_KEYS)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "no_cred_claude")
        self.assertIsNone(res["source"])
        self.assertIsNone(res["plan"])

    def test_claude_token_reads_env_then_credentials_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cred_file = Path(tmp) / ".credentials.json"
            cred_file.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "file-token"}}),
                encoding="utf-8",
            )
            with mock.patch.object(app, "CLAUDE_CREDENTIALS", cred_file):
                with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": " env-token "}, clear=True):
                    self.assertEqual(app.claude_token(), ("env-token", "CLAUDE_CODE_OAUTH_TOKEN"))
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(app.claude_token(), ("file-token", "~/.claude/.credentials.json"))

    def test_fetch_claude_parses_headers_in_canonical_order_and_normalizes_plan(self):
        headers = {
            "Anthropic-Ratelimit-Unified-7d-Opus-Utilization": "0.425",
            "Anthropic-Ratelimit-Unified-7d-Opus-Reset": "1700003600",
            "Anthropic-Ratelimit-Unified-5h-Utilization": "0.15",
            "Anthropic-Ratelimit-Unified-5h-Reset": "1700001800",
            "Anthropic-Ratelimit-Unified-Tier": " Max ",
            "Anthropic-Ratelimit-Unified-Custom-Utilization": "0.05",
        }
        with mock.patch.object(app, "http_json", return_value=(200, headers, b"{}")) as mocked_http:
            res = app.fetch_claude(token_override="tok_abc")

        mocked_http.assert_called_once()
        self.assertEqual(set(res.keys()), EXPECTED_PROVIDER_KEYS)
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "--claude-token")
        self.assertEqual(res["plan"], "max")
        self.assertEqual([w["label"] for w in res["windows"]], ["Session (5h)", "Opus (7d)", "CUSTOM"])
        self.assertEqual([w["percent"] for w in res["windows"]], [15.0, 42.5, 5.0])

    def test_fetch_claude_handles_401_missing_headers_and_connection_errors(self):
        with mock.patch.object(app, "http_json", return_value=(401, {}, b"")):
            expired = app.fetch_claude(token_override="bad_tok")
        self.assertFalse(expired["ok"])
        self.assertEqual(expired["code"], "expired_claude")
        self.assertEqual(expired["source"], "--claude-token")

        with mock.patch.object(app, "http_json", return_value=(200, {"content-type": "application/json"}, b"{}")):
            no_hdr = app.fetch_claude(token_override="tok")
        self.assertFalse(no_hdr["ok"])
        self.assertEqual(no_hdr["code"], "no_headers")
        self.assertEqual(no_hdr["arg"], 200)

        with mock.patch.object(app, "http_json", side_effect=OSError("network unreachable")):
            conn_err = app.fetch_claude(token_override="tok")
        self.assertFalse(conn_err["ok"])
        self.assertEqual(conn_err["code"], "connect")
        self.assertIn("network unreachable", conn_err["arg"])
        self.assertEqual(conn_err["source"], "--claude-token")


class FetchCodexTests(unittest.TestCase):
    def test_missing_credentials_returns_normalized_error(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(app, "CODEX_AUTH_FILE", Path("/nonexistent/codex-auth.json")):
            res = app.fetch_codex()
        self.assertEqual(set(res.keys()), EXPECTED_PROVIDER_KEYS)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "no_cred_codex")
        self.assertIsNone(res["source"])
        self.assertIsNone(res["plan"])

    def test_codex_auth_reads_file_and_jwt_account_id(self):
        tok = jwt_with_payload({app.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": "acct_jwt"}})
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            auth_file.write_text(json.dumps({"tokens": {"access_token": tok}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(app, "CODEX_AUTH_FILE", auth_file):
                self.assertEqual(app.codex_auth(), (tok, "acct_jwt", "~/.codex/auth.json"))

    def test_fetch_codex_parses_windows_plan_and_account_header(self):
        tok = jwt_with_payload({app.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": "acct_999"}})
        payload = {
            "plan_type": "Plus",
            "rate_limit": {
                "primary_window": {"used_percent": 31.44, "limit_window_seconds": 18_000, "reset_at": 1_700_005_000},
                "secondary_window": {"used_percent": 62.0, "limit_window_seconds": 604_800, "reset_at": 1_700_500_000},
            },
            "additional_rate_limits": [
                {
                    "limit_name": "o3_pro",
                    "rate_limit": {
                        "primary_window": {"used_percent": 10, "limit_window_seconds": 86_400, "reset_at": 1_700_090_000}
                    },
                }
            ],
        }
        with mock.patch.object(
            app, "http_json", return_value=(200, {}, json.dumps(payload).encode())
        ) as mocked_http:
            res = app.fetch_codex(token_override=tok)

        sent_headers = mocked_http.call_args.kwargs["headers"]
        self.assertEqual(sent_headers["chatgpt-account-id"], "acct_999")
        self.assertEqual(set(res.keys()), EXPECTED_PROVIDER_KEYS)
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "--codex-token")
        self.assertEqual(res["plan"], "plus")
        self.assertEqual(len(res["windows"]), 3)
        self.assertEqual(res["windows"][0]["percent"], 31.4)

    def test_fetch_codex_handles_401_500_bad_json_empty_usage_and_connect_error(self):
        with mock.patch.object(app, "http_json", return_value=(401, {}, b"")):
            self.assertEqual(app.fetch_codex(token_override="tok")["code"], "expired_codex")

        with mock.patch.object(app, "http_json", return_value=(503, {}, b"")):
            http_err = app.fetch_codex(token_override="tok")
        self.assertEqual(http_err["code"], "http")
        self.assertEqual(http_err["arg"], 503)

        with mock.patch.object(app, "http_json", return_value=(200, {}, b"not-json")):
            self.assertEqual(app.fetch_codex(token_override="tok")["code"], "bad_json")

        with mock.patch.object(
            app, "http_json", return_value=(200, {}, json.dumps({"plan_type": "Pro", "rate_limit": {}}).encode())
        ):
            empty = app.fetch_codex(token_override="tok")
        self.assertEqual(empty["code"], "no_usage")
        self.assertEqual(empty["plan"], "pro")
        self.assertEqual(empty["source"], "--codex-token")

        with mock.patch.object(app, "http_json", side_effect=TimeoutError("timed out")):
            conn = app.fetch_codex(token_override="tok")
        self.assertEqual(conn["code"], "connect")
        self.assertEqual(conn["source"], "--codex-token")


class PollerSyncAndCooldownTests(unittest.TestCase):
    def test_slow_upstream_refresh_over_1_5s_returns_updated_snapshot(self):
        """驗證慢速 (>1.5s) 上游回應時,Condition 同步仍能拿到新一輪資料而非舊快照。"""
        call_count = 0
        delay_sec = 0.0

        def fake_fetch_claude(_token):
            nonlocal call_count
            call_count += 1
            current = call_count
            if delay_sec > 0:
                time.sleep(delay_sec)
            return app.provider_ok(
                "claude",
                "Claude",
                [{"label": "Session (5h)", "kind": "session", "win": "5h",
                  "percent": float(current * 25), "reset_epoch": 1_800_000_000}],
                source="mock",
            )

        args = make_args(only="claude", interval=300)
        poller = app.Poller(args, cooldown_sec=30, refresh_timeout=10.0)
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(app, "fetch_claude", side_effect=fake_fetch_claude):
            worker = threading.Thread(target=poller.run_forever, daemon=True)
            worker.start()
            try:
                self.assertTrue(poller.wait_for_generation(1, timeout=3.0))
                initial = poller.snapshot()
                self.assertEqual(initial["providers"][0]["windows"][0]["percent"], 25.0)
                self.assertEqual(initial["refresh_cooldown_remaining"], 0)

                delay_sec = 1.7
                t0 = time.monotonic()
                refreshed = poller.refresh()
                elapsed = time.monotonic() - t0

                self.assertGreaterEqual(elapsed, 1.65)
                self.assertEqual(call_count, 2)
                self.assertEqual(refreshed["providers"][0]["windows"][0]["percent"], 50.0)
                self.assertGreater(refreshed["refresh_cooldown_remaining"], 0)
            finally:
                poller.stop()
                worker.join(timeout=3.0)

    def test_30s_refresh_cooldown_rate_limits_duplicate_refreshes(self):
        """驗證 30 秒冷卻時間內重複觸發 refresh 只會打一次上游,過期後才會重抓。"""
        clock = FakeClock(start=500.0)
        upstream_calls = 0

        def fake_fetch_claude(_token):
            nonlocal upstream_calls
            upstream_calls += 1
            return app.provider_ok(
                "claude",
                "Claude",
                [{"label": "Session (5h)", "kind": "session", "win": "5h",
                  "percent": float(upstream_calls * 10), "reset_epoch": 1_800_000_000}],
                source="mock",
            )

        args = make_args(only="claude", interval=600)
        poller = app.Poller(args, clock=clock)
        self.assertEqual(poller.cooldown_sec, 30.0)

        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(app, "fetch_claude", side_effect=fake_fetch_claude):
            worker = threading.Thread(target=poller.run_forever, daemon=True)
            worker.start()
            try:
                self.assertTrue(poller.wait_for_generation(1, timeout=3.0))
                self.assertEqual(upstream_calls, 1)
                self.assertEqual(poller.snapshot()["refresh_cooldown_remaining"], 0)

                # 第一次手動重新整理:觸發第 2 次上游呼叫並啟動 30s 冷卻
                snap1 = poller.refresh()
                self.assertEqual(upstream_calls, 2)
                self.assertEqual(snap1["providers"][0]["windows"][0]["percent"], 20.0)
                self.assertEqual(snap1["refresh_cooldown_remaining"], 30)

                # 12 秒後仍在冷卻期內:重複 refresh 不會再打上游
                clock.advance(12.0)
                snap2 = poller.refresh()
                self.assertEqual(upstream_calls, 2)
                self.assertEqual(snap2["providers"][0]["windows"][0]["percent"], 20.0)
                self.assertEqual(snap2["refresh_cooldown_remaining"], 18)

                # 推進超過 30 秒冷卻期:歸零並允許下一輪上游重抓
                clock.advance(18.1)
                self.assertEqual(poller.snapshot()["refresh_cooldown_remaining"], 0)
                snap3 = poller.refresh()
                self.assertEqual(upstream_calls, 3)
                self.assertEqual(snap3["providers"][0]["windows"][0]["percent"], 30.0)
                self.assertEqual(snap3["refresh_cooldown_remaining"], 30)
            finally:
                poller.stop()
                worker.join(timeout=3.0)

    def test_concurrent_in_flight_refreshes_coalesce_into_single_upstream_call(self):
        upstream_calls = 0
        entered = threading.Event()
        release = threading.Event()

        def fake_fetch_claude(_token):
            nonlocal upstream_calls
            upstream_calls += 1
            if upstream_calls == 2:
                entered.set()
                release.wait(timeout=3.0)
            return app.provider_ok(
                "claude",
                "Claude",
                [{"label": "Session (5h)", "kind": "session", "win": "5h",
                  "percent": float(upstream_calls * 15), "reset_epoch": 1_800_000_000}],
                source="mock",
            )

        args = make_args(only="claude", interval=600)
        poller = app.Poller(args, cooldown_sec=30, refresh_timeout=5.0)
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(app, "fetch_claude", side_effect=fake_fetch_claude):
            worker = threading.Thread(target=poller.run_forever, daemon=True)
            worker.start()
            try:
                self.assertTrue(poller.wait_for_generation(1, timeout=3.0))
                results = []
                callers = [
                    threading.Thread(target=lambda: results.append(poller.refresh()))
                    for _ in range(2)
                ]
                callers[0].start()
                self.assertTrue(entered.wait(timeout=2.0))
                callers[1].start()
                time.sleep(0.05)
                release.set()
                for c in callers:
                    c.join(timeout=3.0)

                self.assertEqual(upstream_calls, 2)
                self.assertEqual([r["providers"][0]["windows"][0]["percent"] for r in results], [30.0, 30.0])
            finally:
                release.set()
                poller.stop()
                worker.join(timeout=3.0)

    def test_refresh_respects_bounded_timeout_when_upstream_stalls(self):
        release = threading.Event()
        calls = 0

        def fake_fetch_claude(_token):
            nonlocal calls
            calls += 1
            if calls >= 2:
                release.wait(timeout=3.0)
            return app.provider_ok(
                "claude",
                "Claude",
                [{"label": "Session (5h)", "kind": "session", "win": "5h",
                  "percent": float(calls * 10), "reset_epoch": 1_800_000_000}],
                source="mock",
            )

        args = make_args(only="claude", interval=600)
        poller = app.Poller(args, cooldown_sec=30, refresh_timeout=0.12)
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(app, "fetch_claude", side_effect=fake_fetch_claude):
            worker = threading.Thread(target=poller.run_forever, daemon=True)
            worker.start()
            try:
                self.assertTrue(poller.wait_for_generation(1, timeout=3.0))
                t0 = time.monotonic()
                snap = poller.refresh()
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 0.8)
                self.assertEqual(snap["providers"][0]["windows"][0]["percent"], 10.0)
            finally:
                release.set()
                poller.stop()
                worker.join(timeout=3.0)


class HttpHandlerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(start=2000.0)
        self.upstream_calls = 0
        self._stdout_patch = contextlib.ExitStack()
        self._stdout_patch.enter_context(contextlib.redirect_stdout(io.StringIO()))

        def fake_fetch_claude(_token):
            self.upstream_calls += 1
            return app.provider_ok(
                "claude",
                "Claude",
                [{"label": "Session (5h)", "kind": "session", "win": "5h",
                  "percent": float(self.upstream_calls * 11), "reset_epoch": 1_800_000_000}],
                source="mock",
            )

        self._stdout_patch.enter_context(
            mock.patch.object(app, "fetch_claude", side_effect=fake_fetch_claude)
        )
        args = make_args(only="claude", interval=600, theme="amber")
        self.poller = app.Poller(args, cooldown_sec=30, clock=self.clock)
        self.worker = threading.Thread(target=self.poller.run_forever, daemon=True)
        self.worker.start()
        self.assertTrue(self.poller.wait_for_generation(1, timeout=3.0))

        self.prev_poller = app.Handler.poller
        self.prev_page = app.Handler.page
        app.Handler.poller = self.poller
        app.Handler.page = app.PAGE.replace("__DEFAULT_THEME__", args.theme).encode()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3.0)
        self.poller.stop()
        self.worker.join(timeout=3.0)
        app.Handler.poller = self.prev_poller
        app.Handler.page = self.prev_page
        self._stdout_patch.close()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_root_route_serves_embedded_html_page(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        html = body.decode("utf-8")
        self.assertIn('data-theme", t || "amber"', html)
        self.assertIn("refresh_cooldown_remaining", html)

    def test_api_usage_snapshot_and_refresh_with_cooldown(self):
        # O(1) 快照查詢不會觸發額外上游呼叫
        status, _, raw = self._get("/api/usage")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(self.upstream_calls, 1)
        self.assertEqual(data["providers"][0]["windows"][0]["percent"], 11.0)
        self.assertEqual(data["refresh_cooldown_remaining"], 0)

        # ?refresh=1 觸發重抓並進入 30 秒冷卻
        status, _, raw = self._get("/api/usage?refresh=1")
        self.assertEqual(status, 200)
        refreshed = json.loads(raw)
        self.assertEqual(self.upstream_calls, 2)
        self.assertEqual(refreshed["providers"][0]["windows"][0]["percent"], 22.0)
        self.assertEqual(refreshed["refresh_cooldown_remaining"], 30)

        # 冷卻期間再次呼叫 ?refresh=1 不會打上游
        self.clock.advance(5.0)
        status, _, raw = self._get("/api/usage?refresh=1")
        self.assertEqual(status, 200)
        cached = json.loads(raw)
        self.assertEqual(self.upstream_calls, 2)
        self.assertEqual(cached["providers"][0]["windows"][0]["percent"], 22.0)
        self.assertEqual(cached["refresh_cooldown_remaining"], 25)

    def test_unknown_routes_return_404(self):
        status, _, _ = self._get("/not-found")
        self.assertEqual(status, 404)
        status_extra, _, _ = self._get("/api/usage_extra")
        self.assertEqual(status_extra, 404)


class CliAndServicePackagingTests(unittest.TestCase):
    def test_print_systemd_flag_renders_unit_without_leaking_tokens(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = app.main([
                "--print-systemd",
                "--host", "0.0.0.0",
                "--port", "9000",
                "--interval", "180",
                "--refresh-cooldown", "45",
                "--only", "claude",
                "--theme", "amber",
                "--claude-token", "sk-ant-secret-do-not-leak",
            ])
        self.assertEqual(rc, 0)
        unit_text = buf.getvalue()
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(unit_text)
        self.assertIn("Unit", parser)
        self.assertIn("Service", parser)
        self.assertIn("Install", parser)
        exec_start = parser["Service"]["ExecStart"]
        self.assertIn("--host 0.0.0.0", exec_start)
        self.assertIn("--port 9000", exec_start)
        self.assertIn("--interval 180", exec_start)
        self.assertIn("--refresh-cooldown 45", exec_start)
        self.assertIn("--only claude", exec_start)
        self.assertIn("--theme amber", exec_start)
        self.assertNotIn("sk-ant-secret-do-not-leak", unit_text)

    def test_print_launchd_flag_outputs_valid_apple_plist(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = app.main(["--print-launchd", "--port", "9100", "--demo"])
        self.assertEqual(rc, 0)
        doc = plistlib.loads(buf.getvalue().encode("utf-8"))
        self.assertEqual(doc["Label"], "com.ai-usage-web")
        self.assertTrue(doc["RunAtLoad"])
        self.assertTrue(doc["KeepAlive"])
        self.assertIn("9100", doc["ProgramArguments"])
        self.assertIn("--demo", doc["ProgramArguments"])

    def test_systemd_and_launchd_flags_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                app.main(["--print-systemd", "--print-launchd"])
        self.assertEqual(ctx.exception.code, 2)

    def test_contrib_templates_and_pyproject_metadata_are_valid(self):
        service_path = REPO_ROOT / "contrib" / "ai-usage-web.service"
        plist_path = REPO_ROOT / "contrib" / "com.ai-usage-web.plist"
        pyproject_path = REPO_ROOT / "pyproject.toml"

        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(service_path.read_text(encoding="utf-8"))
        self.assertIn("ExecStart", parser["Service"])

        plist_doc = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(plist_doc["Label"], "com.ai-usage-web")
        self.assertIsInstance(plist_doc["ProgramArguments"], list)

        if tomllib is not None:
            meta = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["project"]["dependencies"], [])
            self.assertEqual(meta["project"]["requires-python"], ">=3.10")
            self.assertEqual(meta["project"]["scripts"]["ai-usage-web"], "claude_usage_web:main")
            self.assertEqual(meta["tool"]["setuptools"]["py-modules"], ["claude_usage_web"])


if __name__ == "__main__":
    unittest.main()
