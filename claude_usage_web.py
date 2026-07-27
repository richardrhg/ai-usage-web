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
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
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
CLAUDE_WINDOW_ORDER = ["5h", "7d", "7d-oauth", "7d-opus", "7d-fable"]
UTIL_RE = re.compile(r"^anthropic-ratelimit-unified-(.+)-utilization$", re.I)

# 網頁配色。要加新主題:PAGE 的 CSS 補一段 [data-theme="xxx"]、
# PAGE 的 JS THEMES 陣列補一行,然後把 id 加進這裡。
THEME_IDS = ["midnight", "phosphor", "amber", "synthwave", "ice", "paper"]
 
 
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
 
 
# ═════════════════════════════════════════════ Claude
 
def claude_token():
    """回傳 (token, 來源說明)。每次輪詢都重讀,Claude Code 換發後會自動跟上。"""
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env.strip(), "環境變數"
 
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
                              "找不到憑證。裝好 Claude Code 後跑一次 `claude` 登入")
 
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
        return provider_error("claude", "Claude", f"連線失敗: {exc}")
 
    lower = {k.lower(): v for k, v in headers.items()}
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
        windows[slug] = {
            "label": CLAUDE_WINDOW_LABELS.get(slug, slug.upper()),
            "percent": round(pct, 1),
            "reset_epoch": to_epoch(lower.get(f"anthropic-ratelimit-unified-{slug}-reset")),
        }
 
    if not windows:
        msg = ("憑證無效或已過期,重跑一次 `claude` 登入" if status == 401
               else f"回應沒有用量 header (HTTP {status})")
        return provider_error("claude", "Claude", msg)
 
    ordered = [windows.pop(k) for k in CLAUDE_WINDOW_ORDER if k in windows]
    ordered += [windows[k] for k in sorted(windows)]
    return {"id": "claude", "name": "Claude", "ok": True, "error": None,
            "source": source, "windows": ordered}
 
 
# ═════════════════════════════════════════════ Codex
 
def codex_auth():
    """回傳 (access_token, account_id, 來源說明)。"""
    env = os.environ.get("CODEX_ACCESS_TOKEN")
    if env:
        token = env.strip()
        claims = decode_jwt_claims(token).get(OPENAI_AUTH_CLAIM) or {}
        return token, claims.get("chatgpt_account_id"), "環境變數"
 
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
    return {
        "label": f"{name} ({win})" if win else name,
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
                              "找不到憑證。裝好 Codex CLI 後跑一次 `codex` 登入")
 
    headers = {"authorization": f"Bearer {token}", "accept": "application/json",
               "user-agent": "ai-usage-web/1.0"}
    if account:
        headers["chatgpt-account-id"] = account
 
    try:
        status, _, raw = http_json(CODEX_ENDPOINT, headers=headers)
    except Exception as exc:
        return provider_error("codex", "Codex", f"連線失敗: {exc}")
 
    if status == 401:
        return provider_error("codex", "Codex", "憑證已過期,開一次 Codex CLI 讓它更新")
    if status >= 400:
        return provider_error("codex", "Codex", f"端點回應 HTTP {status}")
 
    try:
        data = json.loads(raw)
    except Exception:
        return provider_error("codex", "Codex", "回應不是合法 JSON")
 
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
        return provider_error("codex", "Codex", "回應裡沒有用量資料")
 
    plan = ((data.get("credits") or {}).get("plan") or data.get("plan_type") or "")
    return {"id": "codex", "name": "Codex", "ok": True, "error": None,
            "source": source, "plan": plan, "windows": windows}
 
 
# ═════════════════════════════════════════════ 輪詢
 
def provider_error(pid, name, message):
    return {"id": pid, "name": name, "ok": False, "error": message, "windows": []}
 
 
def demo_providers():
    now = int(time.time())
    return [
        {"id": "claude", "name": "Claude", "ok": True, "error": None, "source": "demo", "windows": [
            {"label": "Session (5h)", "percent": 12.0, "reset_epoch": now + 4 * 3600 + 30 * 60},
            {"label": "Weekly (7d)", "percent": 23.0, "reset_epoch": now + 5 * 86400},
            {"label": "Fable (7d)", "percent": 32.0, "reset_epoch": now + 5 * 86400},
        ]},
        {"id": "codex", "name": "Codex", "ok": True, "error": None, "source": "demo", "windows": [
            {"label": "Session (5h)", "percent": 74.0, "reset_epoch": now + 2 * 3600 + 10 * 60},
            {"label": "Weekly (7d)", "percent": 91.0, "reset_epoch": now + 3 * 86400},
        ]},
    ]
 
 
class Poller:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.state = {"providers": [], "fetched_at": None}
        self._wake = threading.Event()
 
    def snapshot(self):
        with self.lock:
            return dict(self.state)
 
    def refresh_now(self):
        self._wake.set()
 
    def run_forever(self):
        while True:
            self._tick()
            self._wake.wait(timeout=self.args.interval)
            self._wake.clear()
 
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
                    results[pid] = provider_error(pid, pid.title(), f"未預期錯誤: {exc}")
 
            for pid in wanted:
                t = threading.Thread(target=run, args=(pid,))
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
 
            providers = [results[p] for p in wanted if p in results]
 
        with self.lock:
            self.state = {"providers": providers, "fetched_at": int(time.time())}
 
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
  // 在畫面畫出來之前先套主題,避免載入時閃一下預設色。
  (function(){
    var t = null;
    try { t = localStorage.getItem("ai-usage-theme"); } catch(e){}
    document.documentElement.setAttribute("data-theme", t || "__DEFAULT_THEME__");
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
    border:3px solid var(--frame); border-radius:18px;
    background:linear-gradient(180deg, var(--panel-a), var(--panel-b));
    box-shadow:0 0 0 6px var(--halo), 0 24px 60px var(--drop),
               inset 0 0 60px rgba(var(--p1-glow), calc(var(--glow-a) * .08));
    padding:24px 24px 18px;
    transition:border-color .3s linear, box-shadow .3s linear;
  }
  .panel{padding-bottom:6px}
  .panel + .panel{border-top:2px solid var(--line);padding-top:22px;margin-top:6px}
  .phead{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:20px;gap:10px}
  .pname{font-size:22px;letter-spacing:1px}
  .psrc{font-size:8px;color:var(--dim);letter-spacing:1px;text-align:right;line-height:1.7;text-transform:uppercase}

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
  .label{font-size:11px;color:var(--label);line-height:1.6}
  .pct{font-size:26px;line-height:1;text-shadow:0 0 10px rgba(var(--acc-glow), calc(var(--glow-a) * .5))}
  .track{
    height:13px;border:2px solid var(--acc-edge);border-radius:3px;
    background:var(--acc-track);overflow:hidden;
  }
  .fill{
    height:100%;width:0%;transition:width .5s steps(20,end);
    background:linear-gradient(180deg,var(--acc-hi),var(--acc));
    box-shadow:0 0 12px rgba(var(--acc-glow), calc(var(--glow-a) * .7));
  }
  .fill.warn{background:linear-gradient(180deg,var(--warn-hi),var(--warn));
    box-shadow:0 0 12px rgba(255,200,70,calc(var(--glow-a) * .6))}
  .fill.crit{background:linear-gradient(180deg,var(--crit-hi),var(--crit));
    box-shadow:0 0 12px rgba(255,90,90,calc(var(--glow-a) * .6))}
  .reset{font-size:8px;color:var(--dim);margin-top:7px;letter-spacing:.5px}
  .perr{font-size:9px;color:var(--err);line-height:2;padding:4px 0 10px}

  footer{
    display:flex;align-items:center;gap:9px;row-gap:12px;flex-wrap:wrap;
    font-size:9px;color:var(--dim);
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
  .btn{
    font-family:inherit;font-size:9px;color:var(--dim);background:none;
    border:2px solid var(--edge);border-radius:4px;padding:6px 9px;cursor:pointer;
  }
  .btn:hover{color:var(--ink);border-color:var(--p1)}
</style>
</head>
<body>
<div class="screen">
  <div id="panels"></div>
  <footer>
    <span class="dot" id="dot"></span>
    <span id="status">連線中…</span>
    <span class="swatches" id="swatches"></span>
    <button class="btn" id="refresh">REFRESH</button>
  </footer>
</div>

<script>
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

function applyTheme(id, save){
  const t = THEMES.find(x => x.id === id) || THEMES[0];
  document.documentElement.setAttribute("data-theme", t.id);
  document.querySelectorAll(".sw").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.theme === t.id)));
  if (save) { try { localStorage.setItem(THEME_KEY, t.id); } catch(e){} }
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

// 按 T 循環切換。
document.addEventListener("keydown", e => {
  if (e.key !== "t" && e.key !== "T") return;
  const cur = document.documentElement.getAttribute("data-theme");
  const i = THEMES.findIndex(t => t.id === cur);
  applyTheme(THEMES[(i + 1) % THEMES.length].id, true);
});

// ── 用量 ──────────────────────────────────────────────
function esc(s){
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
 
function fmtCountdown(sec){
  if (sec === null || sec === undefined) return null;
  if (sec <= 0) return "now";
  const d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600), m = Math.floor(sec%3600/60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}
 
let last = null;
 
function renderPanel(p){
  const now = Math.floor(Date.now()/1000);
  let inner;
 
  if (!p.ok){
    inner = '<div class="perr">' + esc(p.error || "未知錯誤") + '</div>';
  } else {
    inner = p.windows.map(w => {
      const pct = Math.max(0, Math.min(100, w.percent));
      const cls = pct >= 90 ? "crit" : pct >= 70 ? "warn" : "";
      const left = fmtCountdown(w.reset_epoch ? w.reset_epoch - now : null);
      return '<div class="row">'
        + '<div class="row-top"><div class="label">' + esc(w.label) + '</div>'
        + '<div class="pct">' + pct.toFixed(0) + '%</div></div>'
        + '<div class="track"><div class="fill ' + cls + '" style="width:' + pct + '%"></div></div>'
        + (left ? '<div class="reset">Resets in ' + left + '</div>' : '')
        + '</div>';
    }).join("");
  }
 
  return '<div class="panel p-' + esc(p.id) + '">'
    + '<div class="phead"><div class="pname">' + esc(p.name) + '</div>'
    + '<div class="psrc">Usage' + (p.plan ? '<br>' + esc(p.plan) : '') + '</div></div>'
    + inner + '</div>';
}
 
function render(data){
  last = data;
  const providers = data.providers || [];
  document.getElementById("panels").innerHTML =
    providers.length ? providers.map(renderPanel).join("")
                     : '<div class="perr">尚未取得任何資料</div>';
 
  const anyBad = !providers.length || providers.some(p => !p.ok);
  document.getElementById("dot").classList.toggle("bad", anyBad);
  tickAge();
}
 
function tickAge(){
  if (!last) return;
  const el = document.getElementById("status");
  if (!last.fetched_at){ el.textContent = "等待第一次輪詢…"; return; }
  const age = Math.max(0, Math.floor(Date.now()/1000) - last.fetched_at);
  const bad = (last.providers || []).filter(p => !p.ok).length;
  el.textContent = (bad ? bad + " ERROR" : "OK") + "  (" + age + "s ago)";
}
 
async function load(force){
  try {
    const r = await fetch("/api/usage" + (force ? "?refresh=1" : ""));
    render(await r.json());
  } catch (e) {
    document.getElementById("status").textContent = "伺服器沒回應";
    document.getElementById("dot").classList.add("bad");
  }
}
 
document.getElementById("refresh").addEventListener("click", () => load(true));

buildSwatches();
let saved = null;
try { saved = localStorage.getItem(THEME_KEY); } catch(e){}
applyTheme(saved || "__DEFAULT_THEME__", false);
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
        if self.path.startswith("/api/usage"):
            if "refresh=1" in self.path:
                self.poller.refresh_now()
                time.sleep(1.5)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(self.poller.snapshot()).encode())
        elif self.path in ("/", "/index.html"):
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
 
 
def main():
    ap = argparse.ArgumentParser(description="Claude + Codex 用量本機儀表板")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="預設只綁本機。想給同網段其他裝置看再改 0.0.0.0")
    ap.add_argument("--interval", type=int, default=120, help="輪詢秒數(預設 120)")
    ap.add_argument("--only", choices=["both", "claude", "codex"], default="both")
    ap.add_argument("--claude-token", default=None)
    ap.add_argument("--codex-token", default=None)
    ap.add_argument("--demo", action="store_true", help="用假資料,不打 API")
    ap.add_argument("--theme", default="midnight", choices=THEME_IDS,
                    help="開啟時的預設配色(瀏覽器記過的選擇優先)")
    args = ap.parse_args()
 
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
 
 
if __name__ == "__main__":
    main()