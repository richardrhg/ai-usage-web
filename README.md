# AI Usage Web v2 — 遠端多帳號自動續期儀表板

[![CI](https://github.com/richardrhg/ai-usage-web/actions/workflows/ci.yml/badge.svg)](https://github.com/richardrhg/ai-usage-web/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

支援**遠端架設、多人獨立帳號、網頁引導 OAuth 綁定與 Token 過期全自動續期**的「Claude + ChatGPT (Codex) + Gemini」訂閱額度監測儀表板，保留復古像素風與 CRT 動態視覺。

- **支援三大平台訂閱配額**：Claude（5h / 7d / Opus / Sonnet / Extra Credits）、ChatGPT Codex（5h / 7d）、Google Gemini（各模型 24h 配額）
- **啟動即檢查 ＋ 過期自動續期（Single-flight Auto-Refresh）**：伺服器啟動時、到期前 5 分鐘與收到 `401` 時自動換發新 Token；內建每組帳號獨立互斥鎖，防止單次旋轉 `refresh_token` 併發衝突
- **多人獨立帳號 ＋ 靜態加密儲存**：每位使用者註冊登入後僅能檢視與管理自己綁定的帳號，SQLite 內所有憑證皆以 `APP_SECRET_KEY`（Fernet 對稱驗證加密）加密存放，前端 API 永不回傳明文 Token
- **嚴格單次聚合資料流（`O(1)`）與型別契約**：儀表板載入僅呼叫單一 `GET /api/usage` 端點取得全平台快照與連線狀態；前端 TypeScript 型別由後端 FastAPI OpenAPI 透過 `npm run gen:api` 自動產生

---

## 快速啟動（Docker 遠端／伺服器部署）

最適合部署至 VPS、家用伺服器、Fly.io、Render 或 Railway：

```bash
cp .env.example .env
# 編輯 .env 填入一組固定的 APP_SECRET_KEY（例如 openssl rand -base64 36）
docker compose up -d --build
```

啟動後開啟 `http://你的伺服器位址:8080`（若掛在 HTTPS 反向代理後方，請將 `.env` 內的 `COOKIE_SECURE=1` 開啟；私人團隊註冊完畢後可設 `ALLOW_REGISTRATION=0` 關閉公開註冊）。

---

## 本機開發啟動（FastAPI + React）

### 1. 啟動後端（預設 `http://127.0.0.1:8000`）

```bash
pip install -r backend/requirements.txt
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 2. 啟動前端開發伺服器（預設 `http://127.0.0.1:5173`，自動代理 `/api`）

```bash
cd frontend
npm install
npm run dev
```

若修改了後端 Pydantic Schema，請執行以下指令重新同步 OpenAPI 型別（切勿手動編輯 `frontend/src/types/api/schema.d.ts`）：

```bash
cd frontend
npm run gen:api
```

---

## 網頁 OAuth 綁定流程說明

登入網站後點擊右上角 **「連結帳號（ACCOUNTS）」**，即可綁定各平台帳號（首次完成後即由後端 `refresh_token` 長期自動續期）：

1. **ChatGPT / Codex（原生 Device Code 裝置碼）**  
   點擊「OAuth 連結」→ 畫面顯示單次驗證碼（如 `ABCD-1234`）→ 點擊開啟 `auth.openai.com/codex/device` 輸入驗證碼 → 網頁自動輪詢偵測授權完成並綁定。
2. **Claude（PKCE 授權碼貼回）**  
   點擊「OAuth 連結」→ 開啟 `claude.ai` 同意授權 → 將 Anthropic 官方畫面顯示的 `code#state` 授權碼貼回輸入框送出即可（後端改用零 Token 消耗的 `GET /api/oauth/usage` 端點讀取額度）。
3. **Google Gemini（Localhost Callback 網址貼回）**  
   先於 `.env` 填入 `GEMINI_OAUTH_CLIENT_ID` 與 `GEMINI_OAUTH_CLIENT_SECRET`，點擊「OAuth 連結」→ 完成 Google 登入後，當瀏覽器跳轉至 `http://localhost:8085/oauth2callback?code=...`（顯示無法連線屬正常現象）時，複製瀏覽器網址列整串網址貼回輸入框即可。  
   *註：Google 對部分個人消費方案層級限制 CLI OAuth 配額查詢；本專案借用各原廠 CLI OAuth 流程之設計僅供個人或小團隊私有自架（Self-hosted）使用，請勿對外開放陌生大眾公開註冊。*

---

## 輕量單檔本機版（保留相容）

若你只需要在個人電腦本機讀取已安裝的 `claude` / `codex` CLI 憑證檔、不想安裝任何套件，原本的零依賴單檔腳本依然完整保留：

```bash
python claude_usage_web.py                  # 本機 http://127.0.0.1:8787
python claude_usage_web.py --demo           # 假資料預覽模式
```

---

## 測試與驗證

```bash
# 1. 後端自動化測試（含啟動自動續期、Single-flight 旋轉鎖、401 自動恢復、多人隔離與三平台 OAuth 流程）
cd backend && pytest -v

# 2. 單檔本機版單元測試
python -m unittest discover -s tests -v

# 3. 前端 OpenAPI 型別同步檢查、TypeScript 檢查與打包驗證
cd frontend && npm run gen:api && npm run typecheck && npm run build
```

## License

[MIT](LICENSE)
