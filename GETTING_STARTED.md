# Getting started

End-to-end. A developer comfortable with a Linux box + a Telegram bot can get this running in
an afternoon. Read the [README](README.md) first for the *why*; this is the *how*.

## Prerequisites

- An always-on box (a $5 VPS, or a home Mac Mini / mini-PC). See README → Hosting.
- A way for your phone to reach it: a public subdomain + HTTPS, or a tunnel (Tailscale / Cloudflare Tunnel).
- A **Soniox** API key (for STT with custom vocabulary).
- An **LLM** for the agent — the author uses Claude Code (`claude --channels`) as a Telegram bot; any LLM API + a Telegram bot works.
- An **Android** phone with **MacroDroid** (iOS can't forward SMS/notifications).
- `python3`, `ffmpeg`, `git`.

## 1. Server (capture → transcribe → queue)

```bash
git clone https://github.com/<you>/anti-amnesia-assistant && cd anti-amnesia-assistant/examples
python3 -m venv venv && . venv/bin/activate
pip install fastapi uvicorn httpx
cp .env.example .env && nano .env          # fill SONIOX_API_KEY, INTERNAL_TOKEN (long random), Telegram token+chat id
uvicorn server:app --host 127.0.0.1 --port 8095
```

Expose it (pick one):
- **Tunnel (recommended for a home box):** `tailscale up` then point the phone at the tailnet IP; or `cloudflared tunnel`.
- **VPS:** put Caddy/Nginx in front with HTTPS, proxy to `127.0.0.1:8095`.

Verify: `curl https://<your-host>/health` → `{"ok":true}`. Never expose uvicorn directly.

Run it as a service: copy `secretary.service`, edit paths, `systemctl enable --now secretary`.

## 2. Memory

```bash
mkdir memory                                # one .md file per fact — see examples/memory/SCHEMA.md
python3 memory/recall.py "test query" --dir ./memory
```
Optional but recommended: wire `memory/sleep_consolidate.py` to a nightly cron to keep the
graph healthy (it only emits *proposals* — your agent applies them).

## 3. Store (where the agent files things)

```bash
sqlite3 store.db < store/schema.sql         # or use Postgres/Notion/a calendar — see store/schema.sql
```

## 4. The agent (the smart half)

Read [`examples/AGENT.md`](examples/AGENT.md) — it has the processing loop, the system prompt,
and the safety rule. Then:
- Stand up your agent (a `claude --channels` Telegram bot, or your own LLM+bot).
- Schedule the poller: `* * * * * /path/to/inbox-poller.sh` (or a systemd timer). It drains the
  queue and hands each item to the agent, which recalls context, reports to you, and acts on
  your command.

## 5. Phone — the three MacroDroid macros

Each macro = **a trigger** + **an HTTP Request (POST)** action. Common settings for the HTTP action:
- **URL:** `https://<your-host>/<endpoint>`
- **Header:** `x-internal-token: <the INTERNAL_TOKEN from your .env>`
- Method: **POST**

### Macro A — SMS / messaging
- **Trigger:** *Device Events → Notification Received*. Set **Application = your Messages app**
  (and/or a messaging app like KakaoTalk). To pull in only specific chat rooms, set the
  **content/title filter to the room or sender name** — that's how you whitelist "just the work team room."
- **Action:** HTTP POST `…/sms`, Content-Type `application/json`, body:
  ```json
  {"from": "[notification_title]", "text": "[notification_text]", "ts": "[timestamp]"}
  ```
  (use MacroDroid magic-text variables for the notification fields).

### Macro B — Call recording
- **Trigger:** *Phone → Call Ended* (use a call-recorder app/ROM that saves a file).
- **Action:** HTTP POST `…/call`, **body = the recording file** (multipart/file upload —
  point it at the just-saved recording path). ⚠️ If the server logs `recv audio=0B`, the file
  wasn't attached — fix the HTTP action to send the file as the request body/part.

### Macro C — Voice memo
- **Trigger:** a home-screen **widget / button** (or a Bluetooth-headset button via a
  connectivity-helper app for hands-free).
- **Action:** record audio → HTTP POST `…/voice` (short) or `…/lecture` (long), file as multipart.

> Tip: keep the endpoint **URLs stable** so you never have to re-edit macros when you change
> server behavior.

## 6. Test each channel

- Text yourself (or post in a whitelisted room) → watch the server log + your Telegram report.
- Make a short call, hang up → expect a `📞` summary.
- Record a 5-second memo → expect a `🎙️` summary.
- Send yourself an email → on the next poll cycle, expect a `📧` report asking to mark read.

If a channel is silent: check the server log (auth 401 = token mismatch; `recv audio=0B` = file
not attached), and that the poller cron is running.

## 7. Make it yours

- Build your glossary in `server.py` `build_context_terms()` from your real names/jargon.
- Tune recall weights in `memory/recall.py` to your data.
- Decide your store schema. Decide what the agent may auto-do vs. propose (default: propose).
- Re-read the README **Security** section and size your hardening to your data's sensitivity.
