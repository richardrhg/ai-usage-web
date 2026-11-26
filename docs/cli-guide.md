# 本機 CLI 套件安裝與背景常駐指南（`ai-usage-web`）

本文件說明如何透過 `uvx`、`pipx` 免管理虛擬環境直接啟動本機版 `ai-usage-web`，以及如何在 Linux（`systemd --user`）與 macOS（`launchd`）設定登入後自動常駐執行。

---

## 資料流與刷新保護機制

- **固定 `O(1)` 查詢**：瀏覽器每 15 秒僅呼叫 `1` 次 `GET /api/usage` 直接讀取本機記憶體內的最新快照，呼叫次數不隨平台或額度視窗筆數成長（零 N+1 查詢）。
- **確定性手動重新整理**：`GET /api/usage?refresh=1` 透過 `Poller` 的 `threading.Condition` 與輪詢世代計數器（Generation Counter）同步等待新觸發的輪詢完成（預設最長等待 `10` 秒），即使上游回應超過 `1.5` 秒也不會提前回傳舊快照。
- **冷卻保護（Refresh Cooldown）**：手動重新整理預設帶有 `30` 秒冷卻時間（可由 `--refresh-cooldown` 調整），冷卻期間重複送出 `?refresh=1` 會直接複用最新快取並回傳 `refresh_cooldown_remaining` 剩餘秒數，網頁右下角重新整理按鈕亦會同步顯示倒數並暫時鎖定，避免連續打爆 Anthropic `/v1/messages` 或 Codex 用量端點。

---

## 一、零依賴安裝與啟動（`uvx` / `pipx`）

`ai-usage-web` 完全只使用 Python 標準函式庫（`dependencies = []`，需求 Python `>= 3.10`），不含任何第三方執行期套件。

### 1. 使用 `uvx` 單次免安裝直接執行

```bash
uvx --from git+https://github.com/richardrhg/ai-usage-web ai-usage-web
```

想先看畫面而不打任何外部 API，可加上 `--demo`：

```bash
uvx --from git+https://github.com/richardrhg/ai-usage-web ai-usage-web --demo
```

### 2. 使用 `uv tool` 或 `pipx` 常駐安裝 `ai-usage-web` 指令

若打算長期使用或掛載成系統背景服務，建議安裝為使用者層級 CLI 指令（通常位於 `~/.local/bin/ai-usage-web`）：

```bash
# 使用 uv
uv tool install git+https://github.com/richardrhg/ai-usage-web

# 或使用 pipx（遠端儲存庫）
pipx install git+https://github.com/richardrhg/ai-usage-web

# 或在已 clone 的專案目錄內安裝
pipx install .
```

安裝完成後即可在任意目錄直接執行：

```bash
ai-usage-web
ai-usage-web --port 9000 --interval 60 --theme amber
```

---

## 二、CLI 參數一覽

| 參數 | 預設值 | 說明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 監聽位址；預設僅限本機，若需同區網裝置瀏覽可改 `0.0.0.0` |
| `--port` | `8787` | HTTP 伺服器埠號 |
| `--interval` | `120` | 背景自動輪詢秒數 |
| `--refresh-cooldown` | `30` | 手動 `?refresh=1` 重新整理的冷卻秒數（`0` 代表停用冷卻） |
| `--only` | `both` | 指定抓取的平台：`both`、`claude` 或 `codex` |
| `--theme` | `midnight` | 預設配色（`midnight` / `phosphor` / `amber` / `synthwave` / `ice` / `paper`） |
| `--demo` | `False` | 使用內建展示資料，不讀取本機憑證也不呼叫外部 API |
| `--claude-token` | `None` | 手動覆寫 Claude OAuth Token（優先於環境變數與憑證檔） |
| `--codex-token` | `None` | 手動覆寫 Codex Access Token（優先於環境變數與 `auth.json`） |
| `--print-systemd` | `False` | 將對應目前參數的 Linux `systemd --user` 單元檔印至標準輸出後結束 |
| `--print-launchd` | `False` | 將對應目前參數的 macOS `launchd` `.plist` 印至標準輸出後結束 |

> 安全提示：`--print-systemd` 與 `--print-launchd` 產生服務設定檔時，刻意**不會**把 `--claude-token` 或 `--codex-token` 明文寫進啟動命令列，以免透過 `ps` 外洩。若需傳入長期 Token，請改用環境變數檔。

---

## 三、Linux 背景常駐（`systemd --user`）

### 1. 一鍵產生並啟用使用者服務

先以 `uv tool install` 或 `pipx install` 安裝好 `ai-usage-web`，接著執行：

```bash
mkdir -p ~/.config/systemd/user
ai-usage-web --print-systemd > ~/.config/systemd/user/ai-usage-web.service
systemctl --user daemon-reload
systemctl --user enable --now ai-usage-web.service
```

若想自訂埠號、輪詢秒數或預設主題，只要在 `--print-systemd` 之前一併帶入即可：

```bash
ai-usage-web --port 9000 --interval 180 --theme phosphor --print-systemd \
  > ~/.config/systemd/user/ai-usage-web.service
systemctl --user daemon-reload
systemctl --user restart ai-usage-web.service
```

### 2. 設定長期 Token 環境變數（選用）

產生的服務檔已預設載入選用的 `~/.config/ai-usage-web/env`（檔案不存在時自動略過）：

```bash
mkdir -p ~/.config/ai-usage-web
cat > ~/.config/ai-usage-web/env <<'EOF'
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
EOF
chmod 600 ~/.config/ai-usage-web/env
systemctl --user restart ai-usage-web.service
```

### 3. 查看狀態、日誌與停用

```bash
systemctl --user status ai-usage-web.service
journalctl --user -u ai-usage-web.service -f

# 停用並停止常駐
systemctl --user disable --now ai-usage-web.service
```

---

## 四、macOS 背景常駐（`launchd` LaunchAgent）

### 1. 一鍵產生並載入 LaunchAgent

```bash
mkdir -p ~/Library/LaunchAgents
ai-usage-web --print-launchd > ~/Library/LaunchAgents/com.ai-usage-web.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ai-usage-web.plist
```

（若在較舊版 macOS，亦可使用 `launchctl load -w ~/Library/LaunchAgents/com.ai-usage-web.plist`。）

同樣可在產生 `.plist` 時一併指定參數：

```bash
ai-usage-web --port 9000 --theme amber --print-launchd \
  > ~/Library/LaunchAgents/com.ai-usage-web.plist
```

### 2. 查看日誌與卸載服務

預設標準輸出與錯誤日誌會寫入 `/tmp/ai-usage-web.out.log` 與 `/tmp/ai-usage-web.err.log`：

```bash
tail -f /tmp/ai-usage-web.out.log /tmp/ai-usage-web.err.log

# 卸載常駐服務
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.ai-usage-web.plist
```

> macOS Keychain 提醒：若 Claude 憑證存放在 macOS Keychain（`Claude Code-credentials`），第一次由背景服務呼叫 `security` 讀取時，系統可能會跳出授權視窗詢問是否允許存取鑰匙圈，請選擇「永遠允許」，或改用 `claude setup-token` 搭配環境變數。

---

## 五、靜態參考範本

儲存庫內的 `contrib/` 目錄亦提供可直接對照的靜態範本：

- `contrib/ai-usage-web.service`：Linux `systemd --user` 服務單元檔範本
- `contrib/com.ai-usage-web.plist`：macOS `launchd` LaunchAgent 設定檔範本
