"""
Personal AI Secretary — capture server (sanitized example skeleton)

The server is intentionally "dumb": it captures SMS/calls/voice from the phone,
transcribes audio with Soniox (using a custom vocabulary), and appends one JSON
line per item to a queue file. A separate long-lived Claude agent polls that
queue and does all the judgment. See README.md.

Run:  uvicorn server:app --host 127.0.0.1 --port 8095
(put it behind HTTPS via your reverse proxy, run under systemd)
"""
import os, time, json, hmac, hashlib, base64, fcntl, tempfile, subprocess, threading
import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ── config (all from env — never hardcode secrets) ───────────────────────────
SONIOX_KEY     = os.environ["SONIOX_API_KEY"]
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]          # shared secret the phone sends
TG_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]      # for failure alerts only
CHAT_ID        = os.environ["CHAT_ID"]
QUEUE          = os.environ.get("INBOX_QUEUE", "/var/lib/secretary/inbox.jsonl")
QUEUE_LOCK     = QUEUE + ".lock"
FAILED_DIR     = os.environ.get("FAILED_DIR", "/var/lib/secretary/failed")

SONIOX = "https://api.soniox.com/v1"
MIN_DURATION = 1.0
MAX_UPLOAD   = 300 * 1024 * 1024   # cap to avoid surprise STT bills / OOM

app = FastAPI()

# ── custom vocabulary: the reason we use Soniox ──────────────────────────────
# Pre-register the words you say constantly so STT biases toward them.
# Build this from YOUR source of truth (a DB query, a static list, ...).
FIXED_TERMS = ["invoice", "reschedule", "follow up", "make-up class"]  # your jargon

def build_context_terms():
    """Return the glossary for Soniox context.terms. Cache ~1h in real use.
    Example: pull names from your DB. Here just the static list."""
    terms = list(FIXED_TERMS)
    # e.g. terms += pull_names_from_your_db()   # names, places, etc.
    return list(dict.fromkeys(terms))           # de-dupe, keep order

# Closed-set last-mile fix for near-homophones that soft hints can't beat.
# Only map forms that don't otherwise exist (avoid over-correcting).
STT_CORRECTIONS = {}  # e.g. {"misheard form": "correct name"}
def _fix(text: str) -> str:
    for wrong, right in STT_CORRECTIONS.items():
        text = (text or "").replace(wrong, right)
    return text

# ── helpers ──────────────────────────────────────────────────────────────────
def _tg(msg: str):
    try:
        httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                   data={"chat_id": CHAT_ID, "text": msg}, timeout=20)
    except Exception:
        pass

def audio_duration(audio: bytes, suffix: str) -> float:
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".m4a") as f:
            f.write(audio); f.flush()
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", f.name],
                capture_output=True, text=True, timeout=15)
            return float(out.stdout.strip())
    except Exception:
        return -1.0

def strip_silence(audio: bytes, suffix: str) -> bytes:
    """ffmpeg silenceremove — Soniox bills by length, so drop silence. Fail open."""
    fin = fout = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".m4a", delete=False) as f:
            f.write(audio); fin = f.name
        fout = fin + ".trim.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", fin,
             "-af", "silenceremove=start_periods=1:start_threshold=-40dB:start_silence=0.5:"
                    "stop_periods=-1:stop_threshold=-40dB:stop_silence=1.5",
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", fout],
            capture_output=True, timeout=900)
        with open(fout, "rb") as f:
            out = f.read()
        return out if len(out) > 200 else audio
    except Exception:
        return audio
    finally:
        for p in (fin, fout):
            try:
                if p and os.path.exists(p): os.unlink(p)
            except Exception:
                pass

def transcribe(audio: bytes, filename: str) -> str:
    """Soniox async STT with custom vocabulary (context.terms)."""
    h = {"Authorization": f"Bearer {SONIOX_KEY}"}
    with httpx.Client(timeout=120) as c:
        r = c.post(f"{SONIOX}/files", headers=h, files={"file": (filename, audio)})
        r.raise_for_status(); fid = r.json()["id"]
        r = c.post(f"{SONIOX}/transcriptions", headers=h, json={
            "model": "stt-async-v5", "file_id": fid,
            "language_hints": ["en"],                       # set your language
            "context": {"terms": build_context_terms()}})   # ← custom vocabulary
        r.raise_for_status(); tid = r.json()["id"]
        for _ in range(300):                                # up to ~10 min for long audio
            st = c.get(f"{SONIOX}/transcriptions/{tid}", headers=h).json().get("status")
            if st == "completed": break
            if st == "error": raise RuntimeError("soniox error")
            time.sleep(2)
        else:
            raise TimeoutError("soniox timed out")
        data = c.get(f"{SONIOX}/transcriptions/{tid}/transcript", headers=h).json()
        text = data.get("text") or "".join(t.get("text", "") for t in data.get("tokens", []))
        return _fix(text.strip())

def enqueue(source: str, text: str, extra: dict = None):
    """Append one item to the queue under flock (poller can't truncate a write)."""
    try:
        item = {"source": source, "text": text or "",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "id": f"{source}-{int(time.time()*1000)}"}
        if extra: item.update(extra)
        os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
        with open(QUEUE_LOCK, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                with open(QUEUE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
        return item["id"]
    except Exception as e:
        _tg(f"queue append failed ({source}): {e}\n{(text or '')[:500]}")
        return None

def _save_failed(audio: bytes, kind: str, err: str):
    try:
        os.makedirs(FAILED_DIR, exist_ok=True)
        path = os.path.join(FAILED_DIR, f"{kind}-{int(time.time()*1000)}.bin")
        with open(path, "wb") as f: f.write(audio or b"")
        _tg(f"⚠️ {kind} STT failed — audio kept: {path}\n{str(err)[:200]} (re-processable)")
    except Exception:
        pass

# ── auth: treat the public endpoint as hostile ───────────────────────────────
_tok_fail: dict = {}
def _auth(token, ip):
    if not token or not hmac.compare_digest(str(token), str(INTERNAL_TOKEN)):
        now = time.time(); e = _tok_fail.get(ip)
        if not e or now > e[1]: _tok_fail[ip] = [1, now + 60]
        else:
            e[0] += 1
            if e[0] > 10: raise HTTPException(429, "too many attempts")
        raise HTTPException(401, "bad token")

async def _read_audio(request: Request):
    ct = request.headers.get("content-type", "")
    if "multipart" in ct:
        form = await request.form()
        f = form.get("file") or next((v for v in form.values() if hasattr(v, "read")), None)
        if hasattr(f, "read"):
            return await f.read(), (getattr(f, "filename", None) or "audio.m4a")
    return await request.body(), "audio.m4a"

# ── endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
def health(): return {"ok": True}

@app.post("/sms")
async def sms(request: Request, x_internal_token: str = Header(None)):
    ip = request.headers.get("x-forwarded-for", "unknown").split(",")[0].strip()
    _auth(x_internal_token, ip)
    raw = (await request.body()).decode("utf-8", "replace")
    ct = request.headers.get("content-type", "")
    if "json" in ct:
        d = json.loads(raw or "{}")
    else:
        from urllib.parse import parse_qs
        q = parse_qs(raw, keep_blank_values=True)
        d = {k: (v[0] if v else "") for k, v in q.items()}
    body = str(d.get("text") or d.get("body") or d.get("message") or "")
    if not body.strip(): return {"ok": False, "reason": "empty"}
    enqueue("sms", body, {"sender": str(d.get("from") or d.get("sender") or ""),
                          "sms_ts": str(d.get("ts") or d.get("time") or "")})
    return {"ok": True}

# In-memory audio de-dupe (6h window). Guards against the phone re-sending the
# same recording — e.g. a "call ended" macro firing on a *missed* call with a
# stale file variable. See GETTING_STARTED "Guard against missed-call re-sends".
DEDUP_WINDOW = 6 * 3600
_seen_audio: "dict[str, float]" = {}

async def _audio_endpoint(request, token, kind, trim):
    ip = request.headers.get("x-forwarded-for", "unknown").split(",")[0].strip()
    _auth(token, ip)
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD:
        raise HTTPException(413, "payload too large")
    audio, fname = await _read_audio(request)
    if len(audio) < 200:
        _tg(f"{kind}: empty upload ({len(audio)}B) — check MacroDroid HTTP file/body setting")
        return {"ok": False, "reason": "no audio"}
    # Skip if we've already seen these exact bytes recently (duplicate re-send).
    h = hashlib.sha256(audio).hexdigest(); now = time.time()
    for k, exp in list(_seen_audio.items()):
        if exp < now: _seen_audio.pop(k, None)
    if h in _seen_audio:
        return {"ok": True, "skipped": "duplicate"}
    _seen_audio[h] = now + DEDUP_WINDOW
    suffix = os.path.splitext(fname)[1] or ".m4a"
    if kind == "voice" and 0 <= audio_duration(audio, suffix) < MIN_DURATION:
        return {"ok": True, "skipped": "too_short"}
    try:
        data = strip_silence(audio, suffix) if trim else audio
        text = transcribe(data, "audio.wav" if trim else fname)
    except Exception as e:
        _save_failed(audio, kind, str(e))
        return {"ok": False, "reason": "transcribe_failed"}
    if not text: return {"ok": True, "transcript": ""}
    enqueue("voice" if kind in ("voice", "lecture") else kind, text)
    return {"ok": True, "transcript_len": len(text)}

@app.post("/voice")
async def voice(request: Request, x_internal_token: str = Header(None)):
    return await _audio_endpoint(request, x_internal_token, "voice", trim=False)

@app.post("/lecture")
async def lecture(request: Request, x_internal_token: str = Header(None)):
    return await _audio_endpoint(request, x_internal_token, "lecture", trim=True)

@app.post("/call")
async def call_rec(request: Request, x_internal_token: str = Header(None)):
    return await _audio_endpoint(request, x_internal_token, "call", trim=True)
