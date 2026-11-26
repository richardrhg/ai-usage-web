#!/usr/bin/env python3
"""
AI Usage Web — 本機版「Claude + Codex 用量儀表板」
 
在自己的電腦上跑,分別去問 Anthropic 和 OpenAI 目前的用量,
把 5 小時 / 每週的額度畫成復古像素風網頁。
 
用法:
    python claude_usage_web.py                  # 兩邊都抓,開 http://127.0.0.1:8787
    python claude_usage_web.py --demo           # 用假資料看畫面,不打任何 API
    python claude_usage_web.py --only claude    # 只顯示 Claude
    python claude_usage_web.py --only codex     # 只顯示 Codex
    python claude_usage_web.py --port 9000 --interval 60
    python claude_usage_web.py --theme amber      # 預設配色

配色:網頁右下角六顆色票可以直接點,或按 T 循環切換,選過的記在瀏覽器裡。
      midnight / phosphor / amber / synthwave / ice / paper
 
憑證(兩邊都是自動偵測,通常什麼都不用設):
    Claude — 裝好 Claude Code 並跑過 `claude` 登入即可
             CLAUDE_CODE_OAUTH_TOKEN 環境變數 / ~/.claude/.credentials.json / macOS Keychain
    Codex  — 裝好 Codex CLI 並跑過 `codex` 登入即可
             CODEX_ACCESS_TOKEN 環境變數 / ~/.codex/auth.json
 
只用 Python 標準函式庫,不需要 pip install 任何東西。
"""
 
import argparse
import base64
import json
import math
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
 
# ── Anthropic ────────────────────────────────────────────────
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"
ANTHROPIC_PROBE_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
 
# ── OpenAI / Codex ───────────────────────────────────────────
# 這個端點是 Codex CLI 自己在用的,OpenAI 沒有正式公開文件,有可能會變。
CODEX_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
 
CLAUDE_WINDOW_LABELS = {
    "5h": "Session (5h)",
    "7d": "Weekly (7d)",
    "7d-oauth": "Weekly (7d)",
    "7d-opus": "Opus (7d)",
    "7d-fable": "Fable (7d)",
}
# label 是給 --once 的文字輸出和舊前端當後備用的;網頁真正拿去翻譯的是這裡的
# (kind, win)。kind 對到 I18N 的字典鍵,win 是「5h」這種長度標記,兩種語言共用。
CLAUDE_WINDOW_META = {
    "5h": ("session", "5h"),
    "7d": ("weekly", "7d"),
    "7d-oauth": ("weekly", "7d"),
    "7d-opus": ("opus", "7d"),
    "7d-fable": ("fable", "7d"),
}
CODEX_WINDOW_KINDS = {"session", "weekly", "monthly"}
CLAUDE_WINDOW_ORDER = ["5h", "7d", "7d-oauth", "7d-opus", "7d-fable"]
UTIL_RE = re.compile(r"^anthropic-ratelimit-unified-(.+)-utilization$", re.I)

# 網頁配色。要加新主題:PAGE 的 CSS 補一段 [data-theme="xxx"]、
# PAGE 的 JS THEMES 陣列補一行,然後把 id 加進這裡。
THEME_IDS = ["midnight", "phosphor", "amber", "synthwave", "ice", "paper"]

# 手動 ?refresh=1 的預設冷卻秒數與最長等待秒數。
REFRESH_COOLDOWN_SEC = 30
REFRESH_TIMEOUT_SEC = 10.0
SYSTEMD_UNIT_NAME = "ai-usage-web.service"
LAUNCHD_LABEL = "com.ai-usage-web"
 
 
# ═════════════════════════════════════════════ 共用工具
 
def http_json(url, *, method="GET", headers=None, body=None, timeout=20):
    """回傳 (狀態碼, 回應 headers dict, 回應 body bytes)。4xx/5xx 不會丟例外。"""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
 
 
def fmt_window(seconds):
    """把視窗長度秒數變成人看得懂的短標籤:18000 -> '5h',604800 -> '7d'。"""
    if not seconds:
        return None
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{round(seconds / 3600)}h"
 
 
def to_epoch(raw):
    """reset 欄位可能是 unix 秒、毫秒,或 ISO8601 字串。三種都吃。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return int(raw / 1000) if raw > 10_000_000_000 else int(raw)
    raw = str(raw).strip()
    if raw.isdigit():
        return to_epoch(int(raw))
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None
 
 
def decode_jwt_claims(token):
    """不驗簽章,只把 JWT 的 payload 挖出來拿 account id。失敗就回空 dict。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def normalize_source(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def normalize_plan(raw):
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def provider_ok(pid, name, windows, *, source=None, plan=None):
    return {
        "id": pid,
        "name": name,
        "ok": True,
        "error": None,
        "code": None,
        "arg": None,
        "source": normalize_source(source),
        "plan": normalize_plan(plan),
        "windows": windows,
    }


# ═════════════════════════════════════════════ Claude
 
def claude_token():
    """回傳 (token, 來源說明)。每次輪詢都重讀,Claude Code 換發後會自動跟上。"""
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env and env.strip():
        return env.strip(), "CLAUDE_CODE_OAUTH_TOKEN"
 
    if CLAUDE_CREDENTIALS.exists():
        try:
            data = json.loads(CLAUDE_CREDENTIALS.read_text(encoding="utf-8"))
            tok = (data.get("claudeAiOauth") or {}).get("accessToken")
            if tok:
                return tok, "~/.claude/.credentials.json"
        except Exception as exc:
            print(f"[claude] 讀憑證檔失敗: {exc}", file=sys.stderr)
 
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                tok = (json.loads(out.stdout.strip()).get("claudeAiOauth") or {}).get("accessToken")
                if tok:
                    return tok, "macOS Keychain"
        except Exception:
            pass
 
    return None, None
 
 
def fetch_claude(token_override=None):
    token, source = (token_override, "--claude-token") if token_override else claude_token()
    if not token:
        return provider_error("claude", "Claude",
                              "找不到憑證。裝好 Claude Code 後跑一次 `claude` 登入",
                              "no_cred_claude", source=source)
 
    body = json.dumps({
        "model": ANTHROPIC_PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()
 
    try:
        status, headers, _ = http_json(
            ANTHROPIC_ENDPOINT, method="POST", body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-version": ANTHROPIC_VERSION,
                "anthropic-beta": ANTHROPIC_OAUTH_BETA,
                "content-type": "application/json",
                "User-Agent": "claude-code/2.1.5",
            },
        )
    except Exception as exc:
        return provider_error("claude", "Claude", f"連線失敗: {exc}", "connect", str(exc),
                              source=source)
 
    lower = {k.lower(): v for k, v in headers.items()}
    plan = lower.get("anthropic-ratelimit-unified-tier")
    windows = {}
    for key, value in lower.items():
        m = UTIL_RE.match(key)
        if not m:
            continue
        slug = m.group(1).lower()
        try:
            pct = float(value) * 100.0
        except (TypeError, ValueError):
            continue
        kind, win = CLAUDE_WINDOW_META.get(slug, (None, None))
        windows[slug] = {
            "label": CLAUDE_WINDOW_LABELS.get(slug, slug.upper()),
            "kind": kind, "win": win,
            "percent": round(pct, 1),
            "reset_epoch": to_epoch(lower.get(f"anthropic-ratelimit-unified-{slug}-reset")),
        }
 
    if not windows:
        if status == 401:
            return provider_error("claude", "Claude",
                                  "憑證無效或已過期,重跑一次 `claude` 登入", "expired_claude",
                                  source=source, plan=plan)
        return provider_error("claude", "Claude",
                              f"回應沒有用量 header (HTTP {status})", "no_headers", status,
                              source=source, plan=plan)
 
    ordered = [windows.pop(k) for k in CLAUDE_WINDOW_ORDER if k in windows]
    ordered += [windows[k] for k in sorted(windows)]
    return provider_ok("claude", "Claude", ordered, source=source, plan=plan)
 
 
# ═════════════════════════════════════════════ Codex
 
def codex_auth():
    """回傳 (access_token, account_id, 來源說明)。"""
    env = os.environ.get("CODEX_ACCESS_TOKEN")
    if env and env.strip():
        token = env.strip()
        claims = decode_jwt_claims(token).get(OPENAI_AUTH_CLAIM) or {}
        return token, claims.get("chatgpt_account_id"), "CODEX_ACCESS_TOKEN"
 
    if CODEX_AUTH_FILE.exists():
        try:
            data = json.loads(CODEX_AUTH_FILE.read_text(encoding="utf-8"))
            tokens = data.get("tokens") or {}
            token = tokens.get("access_token")
            if token:
                claims = decode_jwt_claims(token).get(OPENAI_AUTH_CLAIM) or {}
                account = tokens.get("account_id") or claims.get("chatgpt_account_id")
                return token, account, "~/.codex/auth.json"
        except Exception as exc:
            print(f"[codex] 讀 auth.json 失敗: {exc}", file=sys.stderr)
 
    return None, None, None
 
 
def codex_window_name(seconds, fallback):
    """用視窗長度決定名稱,不要照 primary/secondary 的順序猜。

    實測 Plus 方案回來的 primary_window 就是 604800 秒(7 天)、secondary_window 是
    null,照順序貼標籤會變成「Session (7d)」這種自相矛盾的東西。
    """
    if not seconds:
        return fallback
    if seconds < 86400:
        return "Session"
    if seconds <= 7 * 86400:
        return "Weekly"
    if seconds <= 31 * 86400:
        return "Monthly"
    return fallback


def _codex_window(raw, name, derive=False):
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percent", raw.get("usedPercent"))
    if not isinstance(pct, (int, float)):
        return None
    seconds = (raw.get("limit_window_seconds") or raw.get("windowSeconds")
               or (raw.get("window_minutes") or 0) * 60)
    if derive:
        name = codex_window_name(seconds, name)
    win = fmt_window(seconds)
    reset = to_epoch(raw.get("reset_at") or raw.get("resetAt"))
    if reset is None and isinstance(raw.get("reset_after_seconds"), (int, float)):
        reset = int(time.time() + raw["reset_after_seconds"])
    kind = name.lower() if name.lower() in CODEX_WINDOW_KINDS else None
    return {
        "label": f"{name} ({win})" if win else name,
        "kind": kind, "win": win,
        "percent": round(max(0.0, min(100.0, float(pct))), 1),
        "reset_epoch": reset,
    }
 
 
def fetch_codex(token_override=None):
    if token_override:
        token = token_override
        account = (decode_jwt_claims(token).get(OPENAI_AUTH_CLAIM) or {}).get("chatgpt_account_id")
        source = "--codex-token"
    else:
        token, account, source = codex_auth()
 
    if not token:
        return provider_error("codex", "Codex",
                              "找不到憑證。裝好 Codex CLI 後跑一次 `codex` 登入",
                              "no_cred_codex", source=source)
 
    headers = {"authorization": f"Bearer {token}", "accept": "application/json",
               "user-agent": "ai-usage-web/1.0"}
    if account:
        headers["chatgpt-account-id"] = account
 
    try:
        status, _, raw = http_json(CODEX_ENDPOINT, headers=headers)
    except Exception as exc:
        return provider_error("codex", "Codex", f"連線失敗: {exc}", "connect", str(exc),
                              source=source)
 
    if status == 401:
        return provider_error("codex", "Codex",
                              "憑證已過期,開一次 Codex CLI 讓它更新", "expired_codex",
                              source=source)
    if status >= 400:
        return provider_error("codex", "Codex", f"端點回應 HTTP {status}", "http", status,
                              source=source)
 
    try:
        data = json.loads(raw)
    except Exception:
        return provider_error("codex", "Codex", "回應不是合法 JSON", "bad_json", source=source)
 
    plan = (data.get("credits") or {}).get("plan") or data.get("plan_type")
    rl = data.get("rate_limit") or {}
    windows = [w for w in (_codex_window(rl.get("primary_window"), "Session", derive=True),
                           _codex_window(rl.get("secondary_window"), "Weekly", derive=True)) if w]
 
    for item in data.get("additional_rate_limits") or []:
        name = item.get("limit_name") or item.get("metered_feature") or "Extra"
        sub = item.get("rate_limit") or {}
        w = _codex_window(sub.get("secondary_window") or sub.get("primary_window"), str(name).title())
        if w:
            windows.append(w)
 
    if not windows:
        return provider_error("codex", "Codex", "回應裡沒有用量資料", "no_usage",
                              source=source, plan=plan)
 
    return provider_ok("codex", "Codex", windows, source=source, plan=plan)
 
 
# ═════════════════════════════════════════════ 輪詢
 
def provider_error(pid, name, message, code=None, arg=None, *, source=None, plan=None):
    """message 是中文原句,網頁沒有對應翻譯時就直接顯示它。

    code / arg 是給前端 i18n 用的:code 對到 I18N 的 err 字典,arg 是要塞進去的
    變數(例外訊息、HTTP 狀態碼)。source 與 plan 一律正規化為一致的欄位結構。
    """
    return {
        "id": pid,
        "name": name,
        "ok": False,
        "error": message,
        "code": code,
        "arg": arg,
        "source": normalize_source(source),
        "plan": normalize_plan(plan),
        "windows": [],
    }
 
 
def demo_providers():
    now = int(time.time())
    return [
        provider_ok("claude", "Claude", [
            {"label": "Session (5h)", "kind": "session", "win": "5h",
             "percent": 12.0, "reset_epoch": now + 4 * 3600 + 30 * 60},
            {"label": "Weekly (7d)", "kind": "weekly", "win": "7d",
             "percent": 23.0, "reset_epoch": now + 5 * 86400},
            {"label": "Fable (7d)", "kind": "fable", "win": "7d",
             "percent": 32.0, "reset_epoch": now + 5 * 86400},
        ], source="demo", plan=None),
        provider_ok("codex", "Codex", [
            {"label": "Session (5h)", "kind": "session", "win": "5h",
             "percent": 74.0, "reset_epoch": now + 2 * 3600 + 10 * 60},
            {"label": "Weekly (7d)", "kind": "weekly", "win": "7d",
             "percent": 91.0, "reset_epoch": now + 3 * 86400},
        ], source="demo", plan=None),
    ]
 
 
class Poller:
    def __init__(self, args, *, cooldown_sec=None, refresh_timeout=REFRESH_TIMEOUT_SEC,
                 clock=time.monotonic):
        self.args = args
        if cooldown_sec is None:
            cooldown_sec = getattr(args, "refresh_cooldown", REFRESH_COOLDOWN_SEC)
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self.refresh_timeout = max(0.0, float(refresh_timeout))
        self._clock = clock
        self.lock = threading.Lock()
        self._cond = threading.Condition(self.lock)
        self.state = {"providers": [], "fetched_at": None}
        self._gen = 0
        self._polling = False
        self._wake_requested = False
        self._pending_refresh_gen = None
        self._cooldown_until = None
        self._stopped = False

    def _cooldown_remaining_unlocked(self):
        if self._cooldown_until is None:
            return 0
        rem = self._cooldown_until - self._clock()
        return math.ceil(rem) if rem > 0 else 0

    def cooldown_remaining(self):
        with self._cond:
            return self._cooldown_remaining_unlocked()

    @property
    def generation(self):
        with self._cond:
            return self._gen

    def snapshot(self):
        with self._cond:
            snap = dict(self.state)
            snap["refresh_cooldown_remaining"] = self._cooldown_remaining_unlocked()
            return snap

    def refresh_now(self):
        with self._cond:
            self._wake_requested = True
            self._cond.notify_all()

    def refresh(self, timeout=None):
        """觸發新一輪輪詢並透過 Condition 等待完成;冷卻時間內直接複用快取快照。"""
        limit = self.refresh_timeout if timeout is None else max(0.0, float(timeout))
        with self._cond:
            if self._pending_refresh_gen is not None and self._gen < self._pending_refresh_gen:
                target_gen = self._pending_refresh_gen
            elif self._cooldown_remaining_unlocked() > 0:
                snap = dict(self.state)
                snap["refresh_cooldown_remaining"] = self._cooldown_remaining_unlocked()
                return snap
            else:
                target_gen = self._gen + (2 if self._polling else 1)
                self._pending_refresh_gen = target_gen
                if self.cooldown_sec > 0:
                    self._cooldown_until = self._clock() + self.cooldown_sec
                self._wake_requested = True
                self._cond.notify_all()

            deadline = self._clock() + limit
            while not self._stopped and self._gen < target_gen:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)

            snap = dict(self.state)
            snap["refresh_cooldown_remaining"] = self._cooldown_remaining_unlocked()
            return snap

    def wait_for_generation(self, target_gen, timeout=None):
        limit = self.refresh_timeout if timeout is None else max(0.0, float(timeout))
        with self._cond:
            deadline = self._clock() + limit
            while not self._stopped and self._gen < target_gen:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            return self._gen >= target_gen

    def stop(self):
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def run_forever(self):
        while True:
            with self._cond:
                if self._stopped:
                    return
                self._wake_requested = False
                self._polling = True
            try:
                self._tick()
            finally:
                with self._cond:
                    if self._polling:
                        self._polling = False
                        self._cond.notify_all()
                    if self._stopped:
                        return
                    if not self._wake_requested:
                        self._cond.wait(timeout=self.args.interval)
                    if self._stopped:
                        return

    def _tick(self):
        if self.args.demo:
            providers = demo_providers()
        else:
            wanted = ["claude", "codex"] if self.args.only == "both" else [self.args.only]
            results = {}
            threads = []
 
            def run(pid):
                try:
                    results[pid] = (fetch_claude(self.args.claude_token) if pid == "claude"
                                    else fetch_codex(self.args.codex_token))
                except Exception as exc:
                    results[pid] = provider_error(pid, pid.title(), f"未預期錯誤: {exc}",
                                                  "unexpected", str(exc))
 
            for pid in wanted:
                t = threading.Thread(target=run, args=(pid,))
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
 
            providers = [results[p] for p in wanted if p in results]
 
        with self._cond:
            self._polling = False
            self.state = {"providers": providers, "fetched_at": int(time.time())}
            self._gen += 1
            if self._pending_refresh_gen is not None and self._gen >= self._pending_refresh_gen:
                self._pending_refresh_gen = None
                if self.cooldown_sec > 0:
                    self._cooldown_until = self._clock() + self.cooldown_sec
            self._cond.notify_all()
 
        for p in providers:
            if p["ok"]:
                bits = "  ".join(f"{w['label']}={w['percent']}%" for w in p["windows"])
                print(f"[{p['id']}] {bits}")
            else:
                print(f"[{p['id']}] {p['error']}")
 
 
# ═════════════════════════════════════════════ 網頁
 
PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI Usage</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
<script>
  // 在畫面畫出來之前先套主題和語言,避免載入時閃一下預設值。
  (function(){
    var d = document.documentElement, t = null, l = null;
    try { t = localStorage.getItem("ai-usage-theme"); } catch(e){}
    try { l = localStorage.getItem("ai-usage-lang"); } catch(e){}
    // 沒選過就看瀏覽器語言,中文環境直接給中文。
    if (!l) l = /^zh\b/i.test(navigator.language || "") ? "zh" : "en";
    d.setAttribute("data-theme", t || "__DEFAULT_THEME__");
    d.setAttribute("data-lang", l);
    d.setAttribute("lang", l === "zh" ? "zh-Hant" : "en");
  })();
</script>
<style>
  /* ── 主題調色盤 ─────────────────────────────────────────────
     底下所有樣式一律吃變數,一個主題只覆寫這一組色。
     --p1 是 Claude 面板、--p2 是 Codex 面板的強調色,每個主題都給兩種
     互相分得出來的色相,兩個面板才不會混在一起。
     --p*-glow 存 r,g,b 三元組給 rgba() 用;--glow-a 是發光總開關
     (亮色主題調低,不然白底上會糊掉)。 */
  :root, [data-theme="midnight"]{
    --bg:#0b1b3a; --bg2:#0d2350; --scan:rgba(255,255,255,.022);
    --panel-a:rgba(20,45,95,.75); --panel-b:rgba(8,20,45,.9);
    --frame:#1e3f7d; --halo:#06122b; --line:#1b366b; --edge:#2a4f96;
    --ink:#dbe7ff; --dim:#7f9ad0; --label:#b9cdf5;
    --warn:#ffcc44; --warn-hi:#ffe08a; --crit:#ff5c5c; --crit-hi:#ff9c9c;
    --ok:#3ddc84; --err:#ffb3b3; --glow-a:1; --drop:rgba(0,0,0,.6);
    --p1:#3f7bff; --p1-hi:#7fb0ff; --p1-text:#8fb8ff; --p1-shadow:#143163;
    --p1-glow:80,140,255; --p1-edge:#2a4f96; --p1-track:#16305f;
    --p2:#1fbf95; --p2-hi:#8ff0d4; --p2-text:#7fe3c4; --p2-shadow:#0f4a3c;
    --p2-glow:60,220,175; --p2-edge:#1f6b58; --p2-track:#0e3630;
  }
  [data-theme="phosphor"]{
    --bg:#03120a; --bg2:#062a16; --scan:rgba(140,255,190,.035);
    --panel-a:rgba(10,52,28,.72); --panel-b:rgba(2,14,8,.92);
    --frame:#186039; --halo:#020b06; --line:#14532f; --edge:#1e7a48;
    --ink:#c9ffdb; --dim:#4e9c6d; --label:#9dedbb;
    --warn:#e8dc4a; --warn-hi:#f6f099; --crit:#ff6b5c; --crit-hi:#ffa79c;
    --ok:#3ddc84; --err:#ffb3ad; --glow-a:1; --drop:rgba(0,0,0,.65);
    --p1:#23e07a; --p1-hi:#8effc0; --p1-text:#5cff9d; --p1-shadow:#0a3d21;
    --p1-glow:60,255,150; --p1-edge:#1e7a48; --p1-track:#0a2d19;
    --p2:#a8e33c; --p2-hi:#dcff8a; --p2-text:#cdf95e; --p2-shadow:#2e4208;
    --p2-glow:175,235,70; --p2-edge:#5a7a1e; --p2-track:#1e2d09;
  }
  [data-theme="amber"]{
    --bg:#140a02; --bg2:#2b1405; --scan:rgba(255,190,110,.03);
    --panel-a:rgba(74,40,8,.72); --panel-b:rgba(18,9,2,.92);
    --frame:#7c4a12; --halo:#0c0601; --line:#6a3f0f; --edge:#93591a;
    --ink:#ffe2b6; --dim:#b07c3c; --label:#ffd49a;
    --warn:#ffd24a; --warn-hi:#ffeaa0; --crit:#ff5f3a; --crit-hi:#ff9f88;
    --ok:#b8d94a; --err:#ffb59f; --glow-a:1; --drop:rgba(0,0,0,.65);
    --p1:#ff9c1a; --p1-hi:#ffd089; --p1-text:#ffb648; --p1-shadow:#5a3208;
    --p1-glow:255,160,50; --p1-edge:#93591a; --p1-track:#3a1e06;
    --p2:#ffe14a; --p2-hi:#fff3a8; --p2-text:#ffe873; --p2-shadow:#5a4a08;
    --p2-glow:255,225,80; --p2-edge:#8a7418; --p2-track:#3a2f06;
  }
  [data-theme="synthwave"]{
    --bg:#11052a; --bg2:#2a0a4d; --scan:rgba(255,160,255,.03);
    --panel-a:rgba(58,14,96,.72); --panel-b:rgba(10,2,24,.92);
    --frame:#5e2490; --halo:#0a0218; --line:#4a1c78; --edge:#7a30b4;
    --ink:#f4dcff; --dim:#a97ad6; --label:#dcb6ff;
    --warn:#ffd24a; --warn-hi:#ffe9a3; --crit:#ff4a86; --crit-hi:#ff96b6;
    --ok:#4affd0; --err:#ffa8c4; --glow-a:1; --drop:rgba(0,0,0,.65);
    --p1:#b44aff; --p1-hi:#dfa0ff; --p1-text:#d08cff; --p1-shadow:#40106a;
    --p1-glow:180,90,255; --p1-edge:#7a30b4; --p1-track:#260a45;
    --p2:#2fe6ff; --p2-hi:#a6f4ff; --p2-text:#6eecff; --p2-shadow:#0b4657;
    --p2-glow:60,230,255; --p2-edge:#1f7f99; --p2-track:#06303b;
  }
  [data-theme="ice"]{
    --bg:#04151c; --bg2:#062c39; --scan:rgba(180,240,255,.03);
    --panel-a:rgba(9,54,68,.72); --panel-b:rgba(2,11,15,.92);
    --frame:#175c70; --halo:#010a0d; --line:#154f61; --edge:#1f7d97;
    --ink:#d6f8ff; --dim:#5fa2b6; --label:#a8e9f7;
    --warn:#ffd24a; --warn-hi:#ffe9a3; --crit:#ff6b6b; --crit-hi:#ffa6a6;
    --ok:#3ddcb4; --err:#ffb3b3; --glow-a:1; --drop:rgba(0,0,0,.65);
    --p1:#17c0e8; --p1-hi:#8df0ff; --p1-text:#62e8ff; --p1-shadow:#0a3d4d;
    --p1-glow:70,220,255; --p1-edge:#1f7d97; --p1-track:#082b36;
    --p2:#5f8cff; --p2-hi:#b3c8ff; --p2-text:#8fadff; --p2-shadow:#172f6b;
    --p2-glow:100,140,255; --p2-edge:#2f4d9e; --p2-track:#0e1b3d;
  }
  [data-theme="paper"]{
    --bg:#ddd6c4; --bg2:#f3edde; --scan:rgba(60,45,20,.045);
    --panel-a:rgba(255,252,244,.92); --panel-b:rgba(238,231,215,.96);
    --frame:#b9ab8d; --halo:#cfc5ad; --line:#c3b79b; --edge:#a89a7c;
    --ink:#2c2517; --dim:#7b6c50; --label:#443a25;
    --warn:#d59400; --warn-hi:#f3ca63; --crit:#cc3b2f; --crit-hi:#e8837a;
    --ok:#2f8f52; --err:#a83226; --glow-a:.25; --drop:rgba(90,75,45,.28);
    --p1:#2f6bd8; --p1-hi:#7ba6f0; --p1-text:#1f4fa8; --p1-shadow:#bcc8de;
    --p1-glow:60,110,210; --p1-edge:#9aa8c0; --p1-track:#cdd4e2;
    --p2:#1f8f62; --p2-hi:#74d0a6; --p2-text:#16714c; --p2-shadow:#b9d6c6;
    --p2-glow:40,150,105; --p2-edge:#93b3a2; --p2-track:#cfe0d5;
  }

  /* 中文用的字型堆疊。Press Start 2P 只有拉丁字母,中文會逐字 fallback,
     而兩種字型的 em 框比例差很多,混排就會高低不一。所以「會出現中文的元素」
     整個換成這組,讓該元素裡的中文和英數字用同一套度量。 */
  :root{--zh-font:"Microsoft JhengHei","PingFang TC","Noto Sans TC",
                 "Hiragino Sans TC",ui-sans-serif,sans-serif}

  *{box-sizing:border-box;margin:0;padding:0}
  body{
    min-height:100vh;
    background:
      repeating-linear-gradient(135deg, var(--scan) 0 3px, transparent 3px 8px),
      radial-gradient(120% 90% at 50% 0%, var(--bg2) 0%, var(--bg) 70%);
    color:var(--ink);
    font-family:'Press Start 2P', ui-monospace, 'DejaVu Sans Mono', monospace;
    display:flex; align-items:center; justify-content:center;
    padding:24px; -webkit-font-smoothing:none;
    transition:background-color .3s linear, color .3s linear;
  }
  .screen{
    width:min(560px,100%);
    position:relative; overflow:hidden;
    border:3px solid var(--frame); border-radius:18px;
    background:linear-gradient(180deg, var(--panel-a), var(--panel-b));
    box-shadow:0 0 0 6px var(--halo), 0 24px 60px var(--drop),
               inset 0 0 60px rgba(var(--p1-glow), calc(var(--glow-a) * .08));
    padding:24px 24px 18px;
    transition:border-color .3s linear, box-shadow .3s linear;
  }
  .panel{padding-bottom:6px}
  .panel + .panel{border-top:2px solid var(--line);padding-top:22px;margin-top:6px}
  .phead{display:flex;align-items:center;justify-content:space-between;min-height:28px;margin-bottom:20px;gap:10px}
  .pname{font-size:22px;line-height:24px;letter-spacing:1px}
  .psrc{font-size:8px;color:var(--dim);letter-spacing:1px;text-align:right;line-height:14px;text-transform:uppercase}

  /* 每個面板挑一組強調色,底下樣式只認 --acc-*,之後要加第三家也是照樣寫一段 */
  .panel{
    --acc:var(--p1); --acc-hi:var(--p1-hi); --acc-text:var(--p1-text);
    --acc-shadow:var(--p1-shadow); --acc-glow:var(--p1-glow);
    --acc-edge:var(--p1-edge); --acc-track:var(--p1-track);
  }
  .p-codex{
    --acc:var(--p2); --acc-hi:var(--p2-hi); --acc-text:var(--p2-text);
    --acc-shadow:var(--p2-shadow); --acc-glow:var(--p2-glow);
    --acc-edge:var(--p2-edge); --acc-track:var(--p2-track);
  }
  .pname{color:var(--acc-text);
    text-shadow:0 0 12px rgba(var(--acc-glow), calc(var(--glow-a) * .85)), 0 3px 0 var(--acc-shadow)}

  .row{margin-bottom:20px}
  .row:last-child{margin-bottom:6px}
  .row-top{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:9px}
  .label{font-size:11px;color:var(--label);line-height:18px}
  /* min-width 是給數字滾動用的:2% 跑到 100% 位數會變,先卡住寬度才不會抖 */
  .pct{font-size:26px;line-height:1;min-width:4.4ch;text-align:right;
    text-shadow:0 0 10px rgba(var(--acc-glow), calc(var(--glow-a) * .5))}
  .track{
    height:13px;border:2px solid var(--acc-edge);border-radius:3px;
    background:var(--acc-track);overflow:hidden;
  }
  .fill{
    height:100%;width:0%;transition:width .5s steps(20,end);
    position:relative; overflow:hidden;
    background:linear-gradient(180deg,var(--acc-hi),var(--acc));
    box-shadow:0 0 12px rgba(var(--acc-glow), calc(var(--glow-a) * .7));
  }
  .fill.warn{background:linear-gradient(180deg,var(--warn-hi),var(--warn));
    box-shadow:0 0 12px rgba(255,200,70,calc(var(--glow-a) * .6))}
  .fill.crit{background:linear-gradient(180deg,var(--crit-hi),var(--crit));
    box-shadow:0 0 12px rgba(255,90,90,calc(var(--glow-a) * .6))}
  /* 寫死像素行高,讓 8px 像素字與 10px 系統中文字佔用完全相同的垂直高度,切語言卡片不跳動 */
  .reset{font-size:8px;line-height:14px;min-height:14px;color:var(--dim);margin-top:7px;letter-spacing:.5px}
  .perr{font-size:9px;color:var(--err);line-height:20px;min-height:34px;padding:4px 0 10px}

  footer{
    display:flex;align-items:center;gap:9px;row-gap:12px;flex-wrap:wrap;
    font-size:9px;line-height:16px;color:var(--dim);
    border-top:2px solid var(--line);padding-top:14px;margin-top:12px;
  }
  /* 呼吸燈。顏色走 --dot-c,所以正常/錯誤兩種狀態共用同一組 keyframes,
     出錯時只要把週期縮短就會變得比較急促。 */
  @keyframes breathe{
    0%,100%{box-shadow:0 0 3px var(--dot-c);opacity:.45}
    50%    {box-shadow:0 0 10px var(--dot-c), 0 0 18px var(--dot-c);opacity:1}
  }
  .dot{
    --dot-c:var(--ok);
    width:9px;height:9px;border-radius:50%;background:var(--dot-c);flex:none;
    animation:breathe 2.8s ease-in-out infinite;
  }
  .dot.bad{--dot-c:var(--crit);animation-duration:1s}
  @media (prefers-reduced-motion:reduce){
    .dot{animation:none;box-shadow:0 0 9px var(--dot-c)}
  }
  .swatches{display:flex;gap:6px;margin-left:auto}
  .sw{
    width:16px;height:16px;padding:0;margin:0;cursor:pointer;border-radius:3px;
    border:2px solid var(--edge);
    background:linear-gradient(135deg, var(--sw-a) 0 50%, var(--sw-b) 50% 100%);
    transition:border-color .15s, box-shadow .15s, transform .15s;
  }
  .sw:hover{transform:translateY(-2px)}
  .sw[aria-pressed="true"]{
    border-color:var(--ink);
    box-shadow:0 0 0 2px var(--halo), 0 0 10px rgba(var(--p1-glow), calc(var(--glow-a) * .9));
  }
  /* 高度寫死 + flex 置中:按鈕的框就不會因為裡面是像素字還是中文而變高變矮。
     #lang 永遠可能顯示中文(英文模式下它寫的是「中文」),所以字型固定用中文那組,
     min-width 則是避免「中文」「EN」兩種寬度讓按鈕跳動。 */
  .btn{
    font-family:inherit;font-size:9px;color:var(--dim);background:none;
    border:2px solid var(--edge);border-radius:4px;padding:0 10px;cursor:pointer;
    height:28px;line-height:1;
    display:inline-flex;align-items:center;justify-content:center;
  }
  #lang{font-family:var(--zh-font);font-size:12px;min-width:52px}
  #refresh{min-width:96px}
  .btn:hover:not(:disabled){color:var(--ink);border-color:var(--p1)}
  .btn:active:not(:disabled){transform:translateY(1px)}
  .btn.busy{color:var(--ink);border-color:var(--p1)}
  .btn:disabled{opacity:.55;cursor:not-allowed}

  /* ── 動畫 ──────────────────────────────────────────────────
     全部集中在這一段。整段被 prefers-reduced-motion 關掉之後畫面只是
     變靜態,數值和版面都不受影響。 */

  /* CRT 開機:第一次載入時螢幕從一條水平線撐開,再閃兩下 */
  @keyframes crt-on{
    0%  {opacity:0;  transform:scaleY(.004); filter:brightness(3)}
    30% {opacity:1;  transform:scaleY(.004); filter:brightness(3)}
    50% {opacity:1;  transform:scaleY(1.05); filter:brightness(1.8)}
    65% {opacity:.65;transform:scaleY(1);    filter:brightness(1)}
    75% {opacity:1}
    85% {opacity:.8}
    100%{opacity:1;  transform:scaleY(1);    filter:brightness(1)}
  }
  .screen.boot{animation:crt-on .8s ease-out both}

  /* 換主題時整台機器閃一下,像換頻道 */
  @keyframes crt-flick{
    0%,100%{filter:none}
    25%{filter:brightness(1.55) saturate(1.25)}
    50%{filter:brightness(.78)}
    75%{filter:brightness(1.25)}
  }
  .screen.flick{animation:crt-flick .3s steps(8,end)}

  /* 掃描線:一條淡光從上到下慢慢掃過整個畫面 */
  @keyframes sweep{
    0%  {transform:translateY(-100%)}
    100%{transform:translateY(760px)}
  }
  .sweep{
    position:absolute;left:0;right:0;top:0;height:52px;pointer-events:none;z-index:2;
    background:linear-gradient(180deg, transparent,
      rgba(var(--p1-glow), calc(var(--glow-a) * .11)), transparent);
    animation:sweep 7.5s linear infinite;
  }

  /* 進度條表面的流光,只在已填滿的區段內跑(靠 .fill 的 overflow 裁掉) */
  @keyframes sheen{
    0%      {transform:translateX(-160%)}
    55%,100%{transform:translateX(420%)}
  }
  .fill::after{
    content:"";position:absolute;top:0;bottom:0;left:0;width:38%;
    background:linear-gradient(100deg, transparent,
      rgba(255,255,255,calc(var(--glow-a) * .5)), transparent);
    animation:sheen 2.8s ease-in-out infinite;
  }

  /* 進到 90% 以上,條子自己會急促地脈動 */
  @keyframes crit-pulse{
    0%,100%{box-shadow:0 0 8px rgba(255,90,90,calc(var(--glow-a) * .5))}
    50%    {box-shadow:0 0 22px rgba(255,90,90,calc(var(--glow-a) * .95)),
                       0 0 36px rgba(255,90,90,calc(var(--glow-a) * .45))}
  }
  .fill.crit{animation:crit-pulse 1.1s ease-in-out infinite}

  /* 首次載入時每一列往上浮出來,--d 由 JS 逐列遞增 */
  @keyframes row-in{
    from{opacity:0;transform:translateY(7px)}
    to  {opacity:1;transform:none}
  }
  .intro .row{animation:row-in .45s ease-out both;animation-delay:var(--d,0ms)}

  /* 抓資料的時候呼吸燈變急促。放在 .bad 後面,兩個都中時以 busy 為準 */
  .dot.busy{animation-duration:.5s}

  @media (prefers-reduced-motion:reduce){
    .screen.boot,.screen.flick,.intro .row,.fill.crit{animation:none}
    .sweep,.fill::after{display:none}
    .fill{transition:none}
  }

  /* ── 中文字級 ──────────────────────────────────────────────
     Press Start 2P 只有拉丁字母,中文會 fallback 到系統字型。像素字在 8px
     還讀得出來,中文不行,所以中文模式把小字級整體放大一級。字體本身不換,
     混排時英數字仍然是像素字。 */
  /* 會混到中文的元素整個換字型,元素內就只有一套度量,不會忽高忽低。
     .pname(Claude / Codex)和 .pct(32%)永遠是純英數,留著像素字。 */
  [data-lang="zh"] .label,
  [data-lang="zh"] .reset,
  [data-lang="zh"] .perr,
  [data-lang="zh"] .psrc,
  [data-lang="zh"] #status,
  [data-lang="zh"] .btn{font-family:var(--zh-font)}

  [data-lang="zh"] .psrc,
  [data-lang="zh"] .reset{font-size:10px}
  [data-lang="zh"] .label{font-size:12px}
  [data-lang="zh"] .perr,
  [data-lang="zh"] footer,
  [data-lang="zh"] .btn{font-size:12px}
  /* letter-spacing 是為拉丁字母調的,中文本來就是等寬方塊,再加字距會散掉 */
  [data-lang="zh"] .psrc,
  [data-lang="zh"] .reset,
  [data-lang="zh"] footer{letter-spacing:0}
</style>
</head>
<body>
<div class="screen" id="screen">
  <div class="sweep"></div>
  <div id="panels"></div>
  <footer>
    <span class="dot" id="dot"></span>
    <span id="status">…</span>
    <span class="swatches" id="swatches"></span>
    <button class="btn" id="lang"></button>
    <button class="btn" id="refresh">REFRESH</button>
  </footer>
</div>

<script>
// ── i18n ──────────────────────────────────────────────
// 翻譯全部放前端:切語言不用重打 API,也不會有伺服器語系跟瀏覽器對不上的問題。
// 伺服器只負責送穩定的 key(視窗的 kind/win、錯誤的 code),句子一律在這裡組。
// 要加第三種語言就在 I18N 補一組,鍵補齊即可,其他地方都不用動。
const LANG_KEY = "ai-usage-lang";
const I18N = {
  en: {
    title:"AI Usage", usage:"Usage", refresh:"REFRESH",
    refreshWait:s => "REFRESH (" + s + "s)", other:"中文",
    switchTo:"切換為中文",
    connecting:"Connecting…", waiting:"Waiting for first poll…",
    noData:"No data yet", noServer:"Server not responding", unknown:"Unknown error",
    ok:"OK", errors:n => n + " ERROR", age:s => "  (" + s + "s ago)",
    resets:v => "Resets in " + v, now:"Resets now",
    dur:(d,h,m) => d ? d+"d "+h+"h" : h ? h+"h "+m+"m" : m+"m",
    win:(name,w) => name + " (" + w + ")",
    kind:{session:"Session", weekly:"Weekly", monthly:"Monthly",
          opus:"Opus", fable:"Fable"},
    err:{
      no_cred_claude:"No credentials. Install Claude Code, then run `claude` to log in",
      no_cred_codex:"No credentials. Install Codex CLI, then run `codex` to log in",
      expired_claude:"Credentials invalid or expired — run `claude` again",
      expired_codex:"Credentials expired — open Codex CLI to refresh them",
      connect:a => "Connection failed: " + a,
      no_headers:a => "No usage headers in response (HTTP " + a + ")",
      http:a => "Endpoint returned HTTP " + a,
      bad_json:"Response is not valid JSON",
      no_usage:"No usage data in response",
      unexpected:a => "Unexpected error: " + a,
    },
  },
  zh: {
    title:"AI 用量", usage:"用量", refresh:"重新整理",
    refreshWait:s => "重新整理 (" + s + "s)", other:"EN",
    switchTo:"Switch to English",
    connecting:"連線中…", waiting:"等待第一次輪詢…",
    noData:"尚未取得任何資料", noServer:"伺服器沒回應", unknown:"未知錯誤",
    // 中文一律不塞字面空白:數字是像素字、中文是系統字,寬度差很多,
    // 空白會被撐得很誇張。斷詞交給標點和單位本身。
    ok:"正常", errors:n => n + "項錯誤", age:s => " · " + s + "秒前",
    resets:v => v + "後重置", now:"即將重置",
    dur:(d,h,m) => d ? d+"天"+h+"小時" : h ? h+"小時"+m+"分" : m+"分",
    win:(name,w) => name + " " + w,
    kind:{session:"工作階段", weekly:"每週", monthly:"每月",
          opus:"Opus", fable:"Fable"},
    err:{
      no_cred_claude:"找不到憑證。裝好 Claude Code 後跑一次 `claude` 登入",
      no_cred_codex:"找不到憑證。裝好 Codex CLI 後跑一次 `codex` 登入",
      expired_claude:"憑證無效或已過期,重跑一次 `claude` 登入",
      expired_codex:"憑證已過期,開一次 Codex CLI 讓它更新",
      connect:a => "連線失敗: " + a,
      no_headers:a => "回應沒有用量 header (HTTP " + a + ")",
      http:a => "端點回應 HTTP " + a,
      bad_json:"回應不是合法 JSON",
      no_usage:"回應裡沒有用量資料",
      unexpected:a => "未預期錯誤: " + a,
    },
  },
};

// 開場的語言由 head 那段 inline script 決定,這裡只是把它接手過來。
let LANG = document.documentElement.getAttribute("data-lang") === "zh" ? "zh" : "en";
function L(){ return I18N[LANG]; }

let last = null;
let busyState = false;
let cooldownUntil = 0;

function cooldownLeft(){
  return Math.max(0, Math.ceil((cooldownUntil - Date.now()) / 1000));
}

function syncRefreshBtn(){
  const btn = document.getElementById("refresh");
  const left = cooldownLeft();
  btn.disabled = busyState || left > 0;
  btn.textContent = left > 0 ? L().refreshWait(left) : L().refresh;
}

function applyLang(id, save){
  LANG = I18N[id] ? id : "en";
  const d = document.documentElement;
  d.setAttribute("data-lang", LANG);
  d.setAttribute("lang", LANG === "zh" ? "zh-Hant" : "en");
  document.title = L().title;
  const lb = document.getElementById("lang");
  lb.textContent = L().other;
  lb.title = L().switchTo;
  syncRefreshBtn();
  if (save) { try { localStorage.setItem(LANG_KEY, LANG); } catch(e){} }
  // 已經有資料就整個重畫;沒有的話至少把狀態列換掉。
  if (last) render(last, false);
  else document.getElementById("status").textContent = L().connecting;
}

// ── 主題 ──────────────────────────────────────────────
// a / b 只是色票左上、右下那兩塊顏色,跟主題本身的變數是分開的。
const THEMES = [
  {id:"midnight",  name:"Midnight",  a:"#3f7bff", b:"#0b1b3a"},
  {id:"phosphor",  name:"Phosphor",  a:"#23e07a", b:"#03120a"},
  {id:"amber",     name:"Amber",     a:"#ff9c1a", b:"#140a02"},
  {id:"synthwave", name:"Synthwave", a:"#b44aff", b:"#11052a"},
  {id:"ice",       name:"Ice",       a:"#17c0e8", b:"#04151c"},
  {id:"paper",     name:"Paper",     a:"#2f6bd8", b:"#f3edde"},
];
const THEME_KEY = "ai-usage-theme";

const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

// 重新觸發一個一次性的 CSS 動畫:先移除 class,強制 reflow,再加回去。
function replay(el, cls, ms){
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), ms);
}

function applyTheme(id, save){
  const t = THEMES.find(x => x.id === id) || THEMES[0];
  document.documentElement.setAttribute("data-theme", t.id);
  document.querySelectorAll(".sw").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.theme === t.id)));
  // save 為真代表是使用者按的,才閃;開場套用已存主題時不閃。
  if (save) {
    if (!REDUCED) replay(document.getElementById("screen"), "flick", 320);
    try { localStorage.setItem(THEME_KEY, t.id); } catch(e){}
  }
}

function buildSwatches(){
  const host = document.getElementById("swatches");
  host.innerHTML = THEMES.map(t =>
    '<button class="sw" data-theme="' + t.id + '" title="' + t.name + '" aria-label="'
    + t.name + '" aria-pressed="false" style="--sw-a:' + t.a + ';--sw-b:' + t.b + '"></button>'
  ).join("");
  host.querySelectorAll(".sw").forEach(b =>
    b.addEventListener("click", () => applyTheme(b.dataset.theme, true)));
}

// 按 T 循環切換主題,按 L 切換語言。
document.addEventListener("keydown", e => {
  const k = e.key.toLowerCase();
  if (k === "t"){
    const cur = document.documentElement.getAttribute("data-theme");
    const i = THEMES.findIndex(t => t.id === cur);
    applyTheme(THEMES[(i + 1) % THEMES.length].id, true);
  } else if (k === "l"){
    applyLang(LANG === "zh" ? "en" : "zh", true);
  }
});

// ── 用量 ──────────────────────────────────────────────
function esc(s){
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
 
// 回傳整句(「Resets in 4h 50m」/「4 小時 50 分後重置」),因為兩種語言的
// 詞序不同,不能讓呼叫端自己拼前綴。
function fmtReset(sec){
  if (sec === null || sec === undefined) return null;
  if (sec <= 0) return L().now;
  const d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600), m = Math.floor(sec%3600/60);
  return L().resets(L().dur(d, h, m));
}

// 視窗標題:伺服器送 kind/win,認得就翻譯,認不得就用它給的英文 label。
function winLabel(w){
  const name = w.kind && L().kind[w.kind];
  if (!name) return w.label || "";
  return w.win ? L().win(name, w.win) : name;
}

// 錯誤:優先用 code 查翻譯,查不到就退回伺服器給的原句。
function errText(p){
  const e = L().err[p.code];
  if (typeof e === "function") return e(p.arg);
  if (typeof e === "string") return e;
  return p.error || L().unknown;
}
 
// 每次輪詢都是整段 innerHTML 重寫,新元素一出生就是最終寬度,
// 所以 .fill 的 transition 永遠不會播。這裡記住上一輪的數值,
// 讓新的條子先長在舊位置,插進 DOM 之後再推到新值,過渡才跑得起來。
const prevPct = new Map();
let rowSeq = 0;

function renderPanel(p){
  const now = Math.floor(Date.now()/1000);
  let inner;

  if (!p.ok){
    inner = '<div class="perr">' + esc(errText(p)) + '</div>';
  } else {
    inner = p.windows.map(w => {
      const pct = Math.max(0, Math.min(100, w.percent));
      const cls = pct >= 90 ? "crit" : pct >= 70 ? "warn" : "";
      const left = fmtReset(w.reset_epoch ? w.reset_epoch - now : null);
      // 用 label 當 key,不用翻譯後的字串,不然切語言會被當成不同的列。
      const key = p.id + "|" + w.label;
      const from = prevPct.has(key) ? prevPct.get(key) : 0;
      prevPct.set(key, pct);
      const delay = (rowSeq++) * 70;
      return '<div class="row" style="--d:' + delay + 'ms">'
        + '<div class="row-top"><div class="label">' + esc(winLabel(w)) + '</div>'
        + '<div class="pct" data-from="' + from + '" data-to="' + pct + '">'
        + from.toFixed(0) + '%</div></div>'
        + '<div class="track"><div class="fill ' + cls + '" style="width:' + from
        + '%" data-to="' + pct + '"></div></div>'
        + (left ? '<div class="reset">' + esc(left) + '</div>' : '')
        + '</div>';
    }).join("");
  }
 
  return '<div class="panel p-' + esc(p.id) + '">'
    + '<div class="phead"><div class="pname">' + esc(p.name) + '</div>'
    + '<div class="psrc">' + esc(L().usage)
    + (p.plan ? '<br>' + esc(p.plan) : '') + '</div></div>'
    + inner + '</div>';
}
 
// 數字從舊值滾到新值,整數跳,配合像素字體。
function countUp(el, from, to){
  if (REDUCED || from === to){ el.textContent = to.toFixed(0) + "%"; return; }
  const dur = 620, t0 = performance.now();
  (function step(t){
    const k = Math.min(1, (t - t0) / dur);
    const eased = 1 - Math.pow(1 - k, 3);
    el.textContent = Math.round(from + (to - from) * eased) + "%";
    if (k < 1) requestAnimationFrame(step);
  })(t0);
}

// 等元素進到 DOM 之後才推寬度,不然瀏覽器會把兩次賦值合併成一次。
function playBars(root){
  root.querySelectorAll(".pct[data-to]").forEach(el =>
    countUp(el, +el.dataset.from, +el.dataset.to));
  requestAnimationFrame(() => {
    root.querySelectorAll(".fill[data-to]").forEach(el => {
      el.style.width = el.dataset.to + "%";
    });
  });
}

let firstRender = true;

function render(data, fromApi){
  last = data;
  if (fromApi){
    const rem = Math.max(0, Math.ceil(Number(data.refresh_cooldown_remaining) || 0));
    cooldownUntil = rem > 0 ? Date.now() + rem * 1000 : 0;
  }
  const providers = data.providers || [];
  const host = document.getElementById("panels");
  rowSeq = 0;
  host.innerHTML =
    providers.length ? providers.map(renderPanel).join("")
                     : '<div class="perr">' + esc(L().noData) + '</div>';

  // 逐列浮出只在第一次做,之後每 15 秒再來一次會很吵。
  if (firstRender && !REDUCED){
    host.classList.add("intro");
    setTimeout(() => host.classList.remove("intro"), 1600);
  }
  firstRender = false;
  playBars(host);

  const anyBad = !providers.length || providers.some(p => !p.ok);
  document.getElementById("dot").classList.toggle("bad", anyBad);
  tickAge();
}
 
function tickAge(){
  syncRefreshBtn();
  if (!last) return;
  const el = document.getElementById("status");
  if (!last.fetched_at){ el.textContent = L().waiting; return; }
  const age = Math.max(0, Math.floor(Date.now()/1000) - last.fetched_at);
  const bad = (last.providers || []).filter(p => !p.ok).length;
  // 分隔符收在 age 裡,因為兩種語言的間距寬度不一樣。
  el.textContent = (bad ? L().errors(bad) : L().ok) + L().age(age);
}
 
function setBusy(b){
  busyState = b;
  document.getElementById("dot").classList.toggle("busy", b);
  document.getElementById("refresh").classList.toggle("busy", b);
  syncRefreshBtn();
}

async function load(force){
  if (force && (busyState || cooldownLeft() > 0)) return;
  setBusy(true);
  try {
    const r = await fetch("/api/usage" + (force ? "?refresh=1" : ""));
    render(await r.json(), true);
  } catch (e) {
    document.getElementById("status").textContent = L().noServer;
    document.getElementById("dot").classList.add("bad");
  } finally {
    setBusy(false);
  }
}

document.getElementById("refresh").addEventListener("click", () => load(true));
document.getElementById("lang").addEventListener("click", () =>
  applyLang(LANG === "zh" ? "en" : "zh", true));

if (!REDUCED) replay(document.getElementById("screen"), "boot", 900);
buildSwatches();
let saved = null;
try { saved = localStorage.getItem(THEME_KEY); } catch(e){}
applyTheme(saved || "__DEFAULT_THEME__", false);
applyLang(LANG, false);
load(false);
setInterval(() => load(false), 15000);
setInterval(tickAge, 1000);
</script>
</body>
</html>
"""
 
 
class Handler(BaseHTTPRequestHandler):
    poller = None
    page = PAGE.replace("__DEFAULT_THEME__", "midnight").encode()
 
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/usage":
            params = urllib.parse.parse_qs(parsed.query)
            want_refresh = params.get("refresh", [""])[0].lower() in ("1", "true", "yes")
            payload = self.poller.refresh() if want_refresh else self.poller.snapshot()
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(payload).encode())
        elif parsed.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", self.page)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")
 
    def _send(self, code, ctype, payload):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
 
    def log_message(self, *args):
        pass  # 不要洗版


# ═════════════════════════════════════════════ 背景常駐服務設定產生器

def _resolve_service_executable(executable=None):
    if executable is not None:
        return [executable] if isinstance(executable, str) else list(executable)
    argv0 = Path(sys.argv[0]) if sys.argv else None
    if argv0 and argv0.name == "ai-usage-web" and argv0.exists():
        return [str(argv0.resolve())]
    found = shutil.which("ai-usage-web")
    if found:
        return [found]
    return [sys.executable, str(Path(__file__).resolve())]


def build_service_command(args, *, executable=None):
    """組出給 systemd / launchd 用的啟動參數陣列(刻意不含明文 token 參數)。"""
    cmd = _resolve_service_executable(executable)
    cmd += ["--host", str(args.host), "--port", str(args.port), "--interval", str(args.interval)]
    if getattr(args, "only", "both") != "both":
        cmd += ["--only", args.only]
    if getattr(args, "theme", "midnight") != "midnight":
        cmd += ["--theme", args.theme]
    cooldown = getattr(args, "refresh_cooldown", REFRESH_COOLDOWN_SEC)
    if cooldown != REFRESH_COOLDOWN_SEC:
        cmd += ["--refresh-cooldown", str(cooldown)]
    if getattr(args, "demo", False):
        cmd.append("--demo")
    return cmd


def render_systemd_unit(args, *, executable=None):
    cmd = build_service_command(args, executable=executable)
    exec_start = shlex.join(cmd)
    return (
        "[Unit]\n"
        "Description=AI Usage Web — Local Claude & Codex Usage Dashboard\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "EnvironmentFile=-%h/.config/ai-usage-web/env\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_launchd_plist(args, *, executable=None, log_dir="/tmp"):
    cmd = build_service_command(args, executable=executable)
    base = str(log_dir).rstrip("/") or "/tmp"
    doc = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": f"{base}/ai-usage-web.out.log",
        "StandardErrorPath": f"{base}/ai-usage-web.err.log",
    }
    return plistlib.dumps(doc, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def build_arg_parser():
    ap = argparse.ArgumentParser(description="Claude + Codex 用量本機儀表板")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="預設只綁本機。想給同網段其他裝置看再改 0.0.0.0")
    ap.add_argument("--interval", type=int, default=120, help="輪詢秒數(預設 120)")
    ap.add_argument("--refresh-cooldown", type=int, default=REFRESH_COOLDOWN_SEC,
                    help=f"手動重新整理冷卻秒數(預設 {REFRESH_COOLDOWN_SEC})")
    ap.add_argument("--only", choices=["both", "claude", "codex"], default="both")
    ap.add_argument("--claude-token", default=None)
    ap.add_argument("--codex-token", default=None)
    ap.add_argument("--demo", action="store_true", help="用假資料,不打 API")
    ap.add_argument("--theme", default="midnight", choices=THEME_IDS,
                    help="開啟時的預設配色(瀏覽器記過的選擇優先)")
    svc = ap.add_mutually_exclusive_group()
    svc.add_argument("--print-systemd", action="store_true",
                     help="輸出 Linux systemd --user 服務單元檔並結束")
    svc.add_argument("--print-launchd", action="store_true",
                     help="輸出 macOS launchd plist 設定檔並結束")
    return ap


def main(argv=None):
    ap = build_arg_parser()
    args = ap.parse_args(argv)

    if args.print_systemd:
        sys.stdout.write(render_systemd_unit(args))
        return 0
    if args.print_launchd:
        sys.stdout.write(render_launchd_plist(args))
        return 0

    poller = Poller(args)
    threading.Thread(target=poller.run_forever, daemon=True).start()

    Handler.poller = poller
    Handler.page = PAGE.replace("__DEFAULT_THEME__", args.theme).encode()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\n  AI Usage Web  →  http://{args.host}:{args.port}")
    print(f"  來源 {args.only}   輪詢 {args.interval}s"
          f"{'   (demo 模式)' if args.demo else ''}   Ctrl-C 結束\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye")
    finally:
        poller.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())