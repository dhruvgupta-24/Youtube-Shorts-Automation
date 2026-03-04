"""
YouTube Shorts Automation — Flask Backend v12
=============================================
FIXES vs v11:
  1. QUALITY: Try multiple player clients in order until we get >=720p.
     tv_embedded → web → ios → android. Probe the result and log it.
  2. MULTI-CLIP: score_transcript always returns exactly top_n=4 clips.
  3. All endpoints cleaned up and stable.
"""

from __future__ import annotations

import glob
import json
import logging
import os
_ffmpeg_bin = os.environ.get("FFMPEG_BIN", r"C:\ffmpeg\bin")
if _ffmpeg_bin:
    os.environ["PATH"] = _ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from functools import wraps

import requests as http_requests
from flask import Flask, jsonify, request, send_file

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

TMP_DIR = os.environ.get("TMP_DIR", "C:/tmp")
os.makedirs(TMP_DIR, exist_ok=True)

_log_fmt = "%(asctime)s [%(levelname)s] %(message)s"
_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    _handlers.append(logging.FileHandler(f"{TMP_DIR}/server.log", encoding="utf-8"))
except Exception:
    pass
logging.basicConfig(level=logging.INFO, format=_log_fmt, handlers=_handlers)
log = logging.getLogger("shorts")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

PROCESSED_FILE   = os.environ.get("PROCESSED_FILE",  f"{TMP_DIR}/processed_videos.json")
YT_API_KEY       = os.environ.get("YOUTUBE_API_KEY", "")
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY",  "")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY",  "")
AI_PROVIDER      = os.environ.get("AI_PROVIDER",     "gemini")
GEMINI_MODEL     = os.environ.get("GEMINI_MODEL",    "gemini-2.0-flash")
OPENAI_MODEL     = os.environ.get("OPENAI_MODEL",    "gpt-4o-mini")
WHISPER_MODEL    = os.environ.get("WHISPER_MODEL",   "tiny")
WHISPER_TIMEOUT  = int(os.environ.get("WHISPER_TIMEOUT",   "1800"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT",  "900"))
SECTION_TIMEOUT  = int(os.environ.get("SECTION_TIMEOUT",   "600"))
AI_TIMEOUT       = int(os.environ.get("AI_TIMEOUT",        "20"))
LOCAL_ONLY       = os.environ.get("LOCAL_ONLY", "false").lower() == "true"

# Player clients tried in order for high-quality downloads
# tv_embedded: no login needed, gives 1080p DASH on most videos
# web: standard, reliable 1080p
# ios: 1080p but sometimes throttled
# android_vr: HLS only, ~480p max — last resort
QUALITY_CLIENTS = ["tv_embedded", "web", "ios", "android_vr"]

app = Flask(__name__)
_processed_lock = threading.Lock()  # prevents race conditions on processed_videos.json

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _base_ctx(data: dict) -> dict:
    return {
        "videoId":      data.get("videoId",      ""),
        "videoTitle":   data.get("videoTitle",   ""),
        "channelId":    data.get("channelId",    ""),
        "channelTitle": data.get("channelTitle", ""),
    }

def ok(ctx: dict, **extra) -> tuple:
    return jsonify({"success": True, "error": None, **ctx, **extra}), 200

def fail(ctx: dict, msg: str, **extra) -> tuple:
    log.warning("FAIL [%s] %s", ctx.get("videoId", "?"), msg)
    return jsonify({"success": False, "error": str(msg), "clipPath": None,
                    "videoPath": None, **ctx, **extra}), 200

def timed(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        log.info("→ %s", fn.__name__)
        result = fn(*args, **kwargs)
        log.info("← %s (%.1fs)", fn.__name__, time.time() - t0)
        return result
    return wrapper

@app.errorhandler(Exception)
def global_error(e):
    log.exception("Unhandled exception: %s", e)
    return jsonify({"success": False, "error": f"Internal error: {e}",
                    "videoId": "", "channelId": "", "clipPath": None,
                    "videoPath": None, "videoTitle": "", "channelTitle": ""}), 200

# ─────────────────────────────────────────────────────────────────────────────
# yt-dlp HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _yt_base_cmd(client: str, *extra: str) -> list[str]:
    """Base yt-dlp command for a given player client."""
    cmd = [
        "yt-dlp",
        "--extractor-args", f"youtube:player_client={client}",
        "--no-playlist",
        "--no-warnings",
        "--extractor-retries", "3",
        "--fragment-retries",  "5",
        "--retries",           "3",
        "--concurrent-fragments", "4",
    ]
    # DASH clients need merge; HLS client (android_vr) must NOT have it
    if client != "android_vr":
        cmd += ["--merge-output-format", "mp4"]
    cmd += list(extra)
    return cmd

def _yt_meta_cmd(*extra: str) -> list[str]:
    """Minimal yt-dlp command for metadata/subtitle ops — no format selection,
    no merge, so it never hits 'format not available' errors."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--extractor-retries", "2",
        "--retries", "2",
    ]
    cmd += list(extra)
    return cmd

def _format_for_client(client: str) -> str:
    """Return best format string for a given client."""
    if client == "android_vr":
        # HLS only — muxed streams
        return "best[height<=1080][ext=mp4]/best[height<=1080]/best"
    # DASH clients: separate video+audio tracks
    return (
        "bestvideo[height=1080][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height>=720][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height>=720]+bestaudio"
        "/bestvideo[height<=1080]+bestaudio"
        "/best"
    )

def _run(cmd: list[str], timeout: int, retries: int = 2) -> subprocess.CompletedProcess:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if r.returncode == 0:
                return r
            last_exc = RuntimeError(r.stderr[-1200:] or r.stdout[-400:] or "non-zero exit")
            log.warning("cmd failed (attempt %d/%d): %s", attempt + 1, retries + 1, str(last_exc)[-300:])
            if any(s in str(last_exc) for s in [
                "Video unavailable", "This video is private",
                "has been removed", "not available in your country",
            ]):
                raise last_exc
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
        except subprocess.TimeoutExpired:
            last_exc = RuntimeError(f"Timeout after {timeout}s")
            if attempt < retries:
                time.sleep(5)
        except FileNotFoundError as e:
            raise RuntimeError(f"Binary not found: {e}") from e
    raise last_exc or RuntimeError("Unknown subprocess failure")

def _find_output(expected_path: str) -> str | None:
    if os.path.exists(expected_path):
        return expected_path
    base = os.path.splitext(expected_path)[0]
    candidates = [c for c in glob.glob(f"{base}.*")
                  if not c.endswith((".part", ".ytdl", ".json"))]
    return candidates[0] if candidates else None

def _safe_remove(*paths: str):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

def _probe_resolution(path: str) -> tuple[int, int]:
    """Returns (width, height) of a video file. Returns (0,0) on failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        parts = r.stdout.strip().split(",")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 0, 0

def _download_best_quality(url: str, out_tpl: str, timeout: int,
                            section: str | None = None) -> tuple[str | None, str]:
    """
    Try each player client in QUALITY_CLIENTS order.
    Returns (output_path, client_used) or (None, '') on total failure.
    Guarantees >=720p if at all possible.
    """
    base = out_tpl.replace(".%(ext)s", "")

    for client in QUALITY_CLIENTS:
        fmt   = _format_for_client(client)
        extra = []
        if section:
            extra = ["--download-sections", f"*{section}"]

        cmd = _yt_base_cmd(client, "-f", fmt, "--output", out_tpl, *extra, url)
        log.info("Trying client=%s fmt=%s", client, fmt[:60])

        try:
            _run(cmd, timeout=timeout, retries=1)
        except RuntimeError as e:
            log.warning("Client %s failed: %s", client, str(e)[:200])
            # Clean up any partial files before next attempt
            for f in glob.glob(f"{base}.*"):
                _safe_remove(f)
            continue

        path = _find_output(f"{base}.mp4") or _find_output(base)
        if not path:
            log.warning("Client %s: no output file found", client)
            continue

        w, h = _probe_resolution(path)
        log.info("Client %s → %s resolution: %dx%d", client, path, w, h)

        if h >= 720:
            log.info("✓ Got %dp from client=%s", h, client)
            return path, client

        # Got something but < 720p — keep it as fallback but try next client
        log.warning("Client %s only gave %dp, trying next client", client, h)
        # Rename to fallback so next client can write fresh
        fallback = f"{base}_fallback_{client}.mp4"
        try:
            os.rename(path, fallback)
        except Exception:
            pass

    # No client gave >=720p — use best fallback available
    fallbacks = sorted(glob.glob(f"{base}_fallback_*.mp4"), key=os.path.getsize, reverse=True)
    if fallbacks:
        best_fallback = fallbacks[0]
        target = f"{base}.mp4"
        os.rename(best_fallback, target)
        for f in fallbacks[1:]:
            _safe_remove(f)
        w, h = _probe_resolution(target)
        log.warning("Using best fallback at %dp", h)
        return target, "fallback"

    return None, ""

def _run_ffmpeg(args: list[str], timeout: int = 300) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["ffmpeg"] + args, capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if r.returncode != 0:
            return False, r.stderr[-800:]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timeout after {timeout}s"
    except FileNotFoundError:
        return False, "ffmpeg not found"
    except Exception as e:
        return False, str(e)

def _ffmpeg_crop_to_short(input_path: str, output_path: str,
                           start_sec: float, dur: float) -> tuple[bool, str]:
    """
    Convert landscape video to 9:16 1080x1920 Short format.

    Strategy: FULL FRAME PRESERVED with blurred background.
    - Background: the source video scaled to fill 1080x1920, then heavily
      blurred (boxblur) so it looks like a branded backdrop.
    - Foreground: the source video scaled to fit ENTIRELY within 1080 wide
      (scale=1080:-2), centred vertically on the 1920 canvas.
    - Result: the entire original frame is always visible. Nothing is cropped.

    Filter graph explanation:
      [0:v] split into two streams:
        [bg]  → scale to 1080x1920 (fill, may stretch slightly), blur heavily
        [fg]  → scale to fit width=1080 keeping AR (height will be ~607 for 16:9)
      overlay fg centred on bg: x=0, y=(1920-fg_h)/2
    """
    vf = (
        # Split source into background and foreground
        "[0:v]split=2[bg_in][fg_in];"
        # Background: scale to fill full 1080x1920, blur it
        "[bg_in]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "boxblur=20:6,"
        "setsar=1[bg];"
        # Foreground: scale to fit within 1080 wide, keep aspect ratio
        "[fg_in]scale=1080:-2:flags=lanczos,setsar=1[fg];"
        # Overlay fg centred vertically on bg
        "[bg][fg]overlay=x=0:y=(H-h)/2"
    )
    return _run_ffmpeg([
        "-y",
        "-ss", str(start_sec),
        "-i", input_path,
        "-t", str(dur),
        "-filter_complex", vf,
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-profile:v", "high", "-level", "4.1",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ], timeout=300)

# ─────────────────────────────────────────────────────────────────────────────
# AI HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _ai_call(prompt: str, system: str = "") -> str | None:
    if LOCAL_ONLY:
        return None
    if AI_PROVIDER == "openai" and OPENAI_API_KEY:
        return _call_openai(prompt, system)
    if GEMINI_API_KEY:
        return _call_gemini(prompt)
    return None

def _call_gemini(prompt: str) -> str | None:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    try:
        r = http_requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=AI_TIMEOUT,
        )
        if not r.ok:
            log.warning("Gemini %d: %s", r.status_code, r.text[:200])
            return None
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        log.warning("Gemini call failed: %s", e)
        return None

def _call_openai(prompt: str, system: str) -> str | None:
    try:
        r = http_requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": OPENAI_MODEL,
                "messages": [
                    *([] if not system else [{"role": "system", "content": system}]),
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 200, "temperature": 0.7,
            },
            timeout=AI_TIMEOUT,
        )
        if not r.ok:
            return None
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning("OpenAI call failed: %s", e)
        return None

# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────

def _s2ts(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"

def _parse_json3(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    segs = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        t0   = ev.get("tStartMs", 0)
        dur  = ev.get("dDurationMs", 2000)
        text = "".join(s.get("utf8", "") for s in ev["segs"]).strip()
        if text and text != "\n":
            segs.append({"start": t0 / 1000, "end": (t0 + dur) / 1000, "text": text})
    return segs

_STRONG = [
    "oh my god","oh my gosh","what the","no way","are you serious",
    "i can't believe","what just happened","holy","are you kidding",
    "this is insane","wait what","bro what","hold on","i'm dead",
    "i'm crying","this is crazy","you're joking","shut up","let's go",
    "no way bro","that's insane","i'm shaking","are you kidding me",
    "oh no","oh wow","oh my","oh god","what is that","that's actually",
    "i've never","you won't believe","watch this",
]
_MEDIUM = [
    "actually","literally","honestly","bro","bruh","dude","insane",
    "crazy","unbelievable","incredible","fire","lowkey","ngl","fr",
    "look at this","you guys","i swear","no cap","real talk",
    "that's wild","wait wait","hold up","okay okay","no no no",
    "yes yes","come on","let's go","here we go",
]
_LAUGH  = ["[laughter]","[laughing]","[applause]","haha","lol","lmao","hahaha","hehe"]
_HOOK   = [
    "you won't believe","i never expected","confession","story time",
    "here's what happened","3 2 1","watch closely","pay attention",
    "this changed everything","the moment","plot twist","turns out",
    "it turns out","the truth","real reason",
]
_STRONG_HI = [
    "yaar sach mein","bhai kya","matlab kya","seriously kya",
    "nahi yaar","kya baat hai","dekho bhai","sun zara",
    "ruk ja","bas kar","pagal hai kya","kya ho gaya",
    "mujhe nahi pata tha","yeh toh kamaal hai","bhai dekho",
    "ek second","hold on yaar","yeh kya tha",
]
_MEDIUM_HI = [
    "bhai","yaar","arre","acha","sahi hai","bilkul","ekdum",
    "mast","bindaas","solid","basically","actually bhai","matlab","samjhe",
]
_LAUGH_HI = ["haha","hahaha","lol","lmao","hehe","pagal","funny","maza aaya"]
_NEGATIVE = [
    ("use code", -12), ("promo code", -12), ("sponsored by", -12),
    ("this video is sponsored", -15), ("check out the link", -10),
    ("subscribe", -6), ("like and subscribe", -8), ("thanks for watching", -8),
    ("comment down below", -5), ("hit the bell", -6), ("welcome back", -5),
    ("in today's video", -5), ("moving on", -3), ("patreon", -8),
    ("merch", -5), ("follow me on", -5),
]

def _score_window(text: str, duration: float, start: float, comment_ts: list[dict]) -> float:
    if not text or len(text.split()) < 8:
        return -10.0
    lo = text.lower()
    s  = 0.0
    for p in _STRONG:
        if p in lo: s += 4.0
    for p in _MEDIUM:
        if p in lo: s += 1.5
    for p in _LAUGH:
        s += min(lo.count(p) * 2.5, 10.0)
    for p in _STRONG_HI:
        if p in lo: s += 4.0
    for p in _MEDIUM_HI:
        if p in lo: s += 1.5
    for p in _LAUGH_HI:
        s += min(lo.count(p) * 2.5, 10.0)
    for p in _HOOK:
        if p in lo: s += 5.0
    if "?" in lo[:200]: s += 5.0
    if "?" in lo[:50]:  s += 3.0
    s += min(text.count("!") * 0.8, 4.0)
    s += min(text.count("?") * 0.6, 3.0)
    words = text.split()
    caps  = sum(1 for w in words if w.isupper() and len(w) > 2)
    s    += min(caps / max(len(words), 1) * 25, 5.0)
    if duration > 0:
        wps = len(words) / duration
        if 1.5 <= wps <= 3.5:  s += 3.0
        elif 0.8 <= wps < 1.5: s += 1.0
        elif wps < 0.5:        s -= 2.0
        elif wps > 5.0:        s -= 1.0
    if start > 120: s += 2.0
    if start > 300: s += 1.0
    for phrase, penalty in _NEGATIVE:
        if phrase in lo:
            s += penalty
    if comment_ts:
        end = start + duration
        for ct in comment_ts:
            ts = ct.get("timestamp_seconds", 0)
            if start <= ts <= end:
                s += 12.0 + ct.get("mention_count", 1) * 3.0
    return s

def _score_segments(segments: list[dict], comment_ts: list[dict], top_n: int = 4) -> list[dict]:
    if not segments:
        return []
    total   = segments[-1]["end"]
    WINDOWS = [45.0, 50.0, 40.0, 55.0, 35.0, 58.0, 30.0, 25.0, 20.0]
    best: list[dict] = []
    t = segments[0]["start"]
    while t + 20 <= total:
        best_score_here = -999.0
        best_candidate  = None
        for win in WINDOWS:
            win_end = t + win
            if win_end > total:
                continue
            ws = [seg for seg in segments if seg["start"] >= t and seg["end"] <= win_end + 3]
            if not ws:
                continue
            text = " ".join(s["text"] for s in ws)
            a_s  = ws[0]["start"]
            a_e  = ws[-1]["end"]
            dur  = a_e - a_s
            if not (18 <= dur <= 62):
                continue
            score = _score_window(text, dur, a_s, comment_ts)
            if score > best_score_here:
                best_score_here = score
                best_candidate  = {
                    "start": _s2ts(a_s), "end": _s2ts(a_e),
                    "start_sec": a_s, "end_sec": a_e,
                    "duration": dur, "text": text, "score": score,
                }
        if best_candidate:
            best.append(best_candidate)
        t += 3.0

    if not best:
        return []

    best.sort(key=lambda x: x["score"], reverse=True)
    kept: list[dict] = []
    for c in best:
        overlaps = any(
            min(c["end_sec"], k["end_sec"]) - max(c["start_sec"], k["start_sec"])
            > 0.4 * min(c["duration"], k["duration"])
            for k in kept
        )
        if not overlaps:
            kept.append(c)
        if len(kept) >= top_n:
            break

    if len(kept) > 1:
        mn  = min(c["score"] for c in kept)
        mx  = max(c["score"] for c in kept)
        rng = mx - mn or 1.0
        for c in kept:
            c["score"] = round((c["score"] - mn) / rng * 10, 2)
    elif kept:
        kept[0]["score"] = 10.0
    return kept

def _gemini_rerank(candidates: list[dict], video_title: str) -> list[dict] | None:
    if LOCAL_ONLY or not GEMINI_API_KEY:
        return None
    snippets = ""
    for i, c in enumerate(candidates[:6], 1):
        snippets += (
            f"Candidate {i} [{c['start']}–{c['end']}] "
            f"dur={c['duration']:.0f}s score={c['score']:.1f}/10\n"
            f"  \"{c['text'][:300]}\"\n\n"
        )
    prompt = (
        f"You are a YouTube Shorts expert. Video: \"{video_title}\".\n"
        f"Pick the best 4 for YouTube Shorts. Criteria:\n"
        f"  - Strong emotional hook in first 3 seconds\n"
        f"  - Self-contained story (makes sense without context)\n"
        f"  - Genuine reaction, surprise, or laughter\n"
        f"  - No sponsor/subscribe mentions\n"
        f"  - Diverse moments (not all from same part of video)\n\n"
        f"{snippets}"
        f"Return ONLY valid JSON array, no markdown:\n"
        f'[{{"candidate":1,"title":"catchy short title","score":9}},...]'
    )
    time.sleep(2)
    raw = _ai_call(prompt)
    if not raw:
        return None
    try:
        raw = re.sub(r"```json|```", "", raw).strip()
        ranked = json.loads(raw)
        result = []
        for item in ranked[:4]:
            idx = item.get("candidate", 0) - 1
            if 0 <= idx < len(candidates):
                c = candidates[idx].copy()
                c["title"]        = item.get("title", f"Clip {idx + 1}")
                c["gemini_score"] = item.get("score", c["score"])
                result.append(c)
        return result or None
    except Exception as e:
        log.warning("Gemini rerank parse error: %s", e)
        return None

# ─────────────────────────────────────────────────────────────────────────────
# CAPTION FETCHERS
# ─────────────────────────────────────────────────────────────────────────────

def _captions_timedtext(video_id: str) -> list[dict] | None:
    try:
        page = http_requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"},
            timeout=15,
        )
        if not page.ok:
            return None
        m = re.search(r'"captionTracks":\s*(\[.*?\])', page.text)
        if not m:
            return None
        en = (
            re.search(r'\{"[^}]*"languageCode":"en"[^}]*"baseUrl":"([^"]+)"', m.group(1))
            or re.search(r'"baseUrl":"([^"]+timedtext[^"]+lang=en[^"]*)"', m.group(1))
        )
        if not en:
            return None
        url = en.group(1).replace("\\u0026", "&") + "&fmt=json3"
        r = http_requests.get(url, timeout=15)
        if not r.ok:
            return None
        segs = []
        for ev in r.json().get("events", []):
            if "segs" not in ev:
                continue
            t0   = ev.get("tStartMs", 0)
            dur  = ev.get("dDurationMs", 2000)
            text = "".join(s.get("utf8", "") for s in ev["segs"]).strip()
            if text and text != "\n":
                segs.append({"start": t0 / 1000, "end": (t0 + dur) / 1000, "text": text})
        return segs or None
    except Exception as e:
        log.debug("timedtext failed: %s", e)
        return None

def _captions_ytdlp(video_id: str) -> list[dict] | None:
    url     = f"https://www.youtube.com/watch?v={video_id}"
    out_tpl = f"{TMP_DIR}/{video_id}_sub"
    # Use _yt_meta_cmd — no format selection so no "format not available" error
    for lang in ("en", "en-US", "en-GB"):
        try:
            _run(_yt_meta_cmd(
                "--write-auto-sub", "--write-sub",
                "--sub-lang", lang, "--sub-format", "json3",
                "--skip-download", "--output", out_tpl, url,
            ), timeout=60)
            for fname in os.listdir(TMP_DIR):
                if fname.startswith(f"{video_id}_sub") and fname.endswith(".json3"):
                    path = os.path.join(TMP_DIR, fname)
                    try:
                        segs = _parse_json3(path)
                        if segs:
                            return segs
                    finally:
                        _safe_remove(path)
        except Exception as e:
            log.debug("yt-dlp captions failed for lang=%s: %s", lang, e)
    return None

# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    checks = {
        "yt_dlp":    shutil.which("yt-dlp") is not None,
        "ffmpeg":    shutil.which("ffmpeg") is not None,
        "tmp_write": os.access(TMP_DIR, os.W_OK),
        "ai":        AI_PROVIDER,
        "ai_key":    bool(GEMINI_API_KEY or OPENAI_API_KEY),
        "clients":   QUALITY_CLIENTS,
    }
    return jsonify({"status": "ok" if checks["yt_dlp"] and checks["ffmpeg"] else "degraded", **checks}), 200

@app.route("/detect_strategy", methods=["POST"])
@timed
def detect_strategy():
    data = request.json or {}
    ctx  = _base_ctx(data)
    vid  = ctx["videoId"]
    if not vid:
        return fail(ctx, "Missing videoId")
    if YT_API_KEY:
        try:
            r = http_requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "snippet,status", "id": vid, "key": YT_API_KEY},
                timeout=10,
            )
            items = r.json().get("items", []) if r.ok else []
            if items:
                snip = items[0].get("snippet", {})
                stat = items[0].get("status", {})
                ctx["videoTitle"]   = snip.get("title",        ctx["videoTitle"])
                ctx["channelTitle"] = snip.get("channelTitle", ctx["channelTitle"])
                ctx["channelId"]    = snip.get("channelId",    ctx["channelId"])
                if stat.get("privacyStatus", "public") != "public":
                    return ok(ctx, strategy=3, reason="restricted")
                return ok(ctx, strategy=1, reason="YT API")
        except Exception as e:
            log.warning("YT API detect_strategy error: %s", e)
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        r = _run(_yt_meta_cmd("--dump-json", "--skip-download", url), timeout=30)
        meta = json.loads(r.stdout)
        ctx["videoTitle"]   = meta.get("title",    ctx["videoTitle"])
        ctx["channelTitle"] = meta.get("uploader", ctx["channelTitle"])
        has_en = ("en" in meta.get("subtitles", {}) or
                  "en" in meta.get("automatic_captions", {}))
        return ok(ctx, strategy=1 if has_en else 2, reason="yt-dlp probe",
                  duration=meta.get("duration", 0))
    except Exception as e:
        return ok(ctx, strategy=3, reason=f"probe failed: {e}")

@app.route("/fetch_captions", methods=["POST"])
@timed
def fetch_captions():
    data = request.json or {}
    ctx  = _base_ctx(data)
    vid  = ctx["videoId"]
    if not vid:
        return fail(ctx, "Missing videoId")
    segs = _captions_timedtext(vid) or _captions_ytdlp(vid)
    if not segs:
        return fail(ctx, "No captions obtainable")
    return ok(ctx, segments=segs, segmentCount=len(segs))

@app.route("/score_transcript", methods=["POST"])
@timed
def score_transcript():
    data       = request.json or {}
    ctx        = _base_ctx(data)
    vid        = ctx["videoId"]
    segments   = data.get("segments", [])
    comment_ts = data.get("commentTimestamps", [])
    strategy   = data.get("strategy", 1)
    video_path = data.get("videoPath")
    lang       = data.get("detectedLanguage", "en")

    if not vid:
        return fail(ctx, "Missing videoId")
    if not segments:
        return fail(ctx, "No segments provided")

    candidates = _score_segments(segments, comment_ts or [], top_n=4)
    if not candidates:
        return fail(ctx, "No viable segments found")

    reranked  = _gemini_rerank(candidates, ctx["videoTitle"])
    scored_by = "gemini+heuristic" if reranked else "heuristic_only"
    top4      = reranked or candidates[:4]

    # Generate title + hook for EACH clip server-side so n8n doesn't need
    # to make per-clip AI calls (which collapse multi-item flows to 1 item)
    clips = []
    for i, c in enumerate(top4):
        clip_title_raw = c.get("title", f"Clip {i+1}")
        clip_text      = c.get("text", "")[:300]

        # --- Title ---
        ai_title = _generate_clip_title(
            video_title=ctx["videoTitle"],
            clip_title=clip_title_raw,
            clip_text=clip_text,
            lang=lang,
        )
        raw = (ai_title or ctx["videoTitle"] or "Watch This").strip()[:73]
        short_title = raw if "#shorts" in raw.lower() else raw + " #Shorts"

        # --- Hook ---
        start_ts = c.get("start", "")
        ai_hook  = _generate_clip_hook(
            video_title=ctx["videoTitle"],
            clip_title=clip_title_raw,
            clip_text=clip_text,
            start=start_ts,
            lang=lang,
        )
        hook        = ai_hook or f"You won't believe what happened at {start_ts} 👀"
        description = hook + "\n\n#Shorts #YouTubeShorts"

        clips.append({
            "clipIndex":    i + 1,
            "start":        c["start"],
            "end":          c["end"],
            "title":        clip_title_raw,
            "shortTitle":   short_title,
            "description":  description,
            "score":        c.get("gemini_score", c["score"]),
            "scoredBy":     scored_by,
            "videoId":      vid,
            "videoTitle":   ctx["videoTitle"],
            "channelId":    ctx["channelId"],
            "channelTitle": ctx["channelTitle"],
            "videoPath":    video_path,
            "detectedLanguage": lang,
        })

    log.info("Returning %d clips for %s", len(clips), vid)
    return ok(ctx, clips=clips, scoredBy=scored_by,
              totalCandidates=len(candidates), strategy=strategy)


def _generate_clip_title(video_title: str, clip_title: str,
                         clip_text: str, lang: str) -> str | None:
    if LOCAL_ONLY or not (GEMINI_API_KEY or OPENAI_API_KEY):
        return None
    lang_note = "Write in Hinglish (Roman Hindi-English)." if lang in ("hi","ur") else "Write in English."
    prompt = (
        f"Generate ONE YouTube Shorts title (max 75 chars, no hashtags).\n"
        f"Curiosity-gap hook, punchy, present-tense, specific — never generic like 'Clip 1'.\n"
        f"{lang_note}\n\n"
        f"Video: \"{video_title}\"\n"
        f"Clip topic: \"{clip_title}\"\n"
        + (f"Transcript: \"{clip_text}\"\n" if clip_text else "")
        + "\nReturn ONLY the title, nothing else."
    )
    result = _ai_call(prompt)
    if not result:
        return None
    t = result.strip('"\'` \n').split("\n")[0][:75]
    if t.lower().startswith("clip ") or len(t) < 5:
        return None
    return t


def _generate_clip_hook(video_title: str, clip_title: str, clip_text: str,
                        start: str, lang: str) -> str | None:
    if LOCAL_ONLY or not (GEMINI_API_KEY or OPENAI_API_KEY):
        return None
    lang_note = "Write in Hinglish (Roman Hindi-English)." if lang in ("hi","ur") else "Write in English."
    prompt = (
        f"Generate ONE YouTube Shorts description hook (1 sentence, max 120 chars).\n"
        f"Must create curiosity. Add 2 relevant emojis. No hashtags.\n"
        f"{lang_note}\n\n"
        f"Video: \"{video_title}\"\nClip: \"{clip_title}\"\n"
        + (f"Transcript: \"{clip_text[:200]}\"\n" if clip_text else "")
        + "\nReturn ONLY the hook sentence."
    )
    result = _ai_call(prompt)
    if not result:
        return None
    return result.strip('"\'` \n').split("\n")[0][:120]

@app.route("/download_section", methods=["POST"])
@timed
def download_section():
    data       = request.json or {}
    ctx        = _base_ctx(data)
    vid        = ctx["videoId"]
    start      = data.get("start", "00:00")
    end        = data.get("end",   "01:00")
    clip_index  = int(data.get("clipIndex", 1))
    title       = data.get("title",       f"clip{clip_index}")
    total_clips = data.get("totalClips",  1)
    short_title = data.get("shortTitle",  "")
    description = data.get("description", "#Shorts #YouTubeShorts")

    def _ts_to_sec(ts: str) -> float:
        parts = [float(p) for p in ts.strip().split(":")]
        return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]

    start_sec = _ts_to_sec(start)
    dur       = max(_ts_to_sec(end) - start_sec, 1.0)

    if not vid:
        return fail(ctx, "Missing videoId")

    url      = f"https://www.youtube.com/watch?v={vid}"
    raw_base = f"{TMP_DIR}/{vid}_raw{clip_index}"
    raw_tpl  = f"{raw_base}.%(ext)s"
    clip_out = f"{TMP_DIR}/{vid}_clip{clip_index}.mp4"
    _safe_remove(clip_out)

    # Try section download first (fast), fall back to full video if needed
    raw_path, client = _download_best_quality(url, raw_tpl, SECTION_TIMEOUT, section=f"{start}-{end}")
    used_full_fallback = False

    if not raw_path:
        log.warning("Section download failed for all clients, trying full video download")
        raw_path, client = _download_best_quality(url, raw_tpl, DOWNLOAD_TIMEOUT, section=None)
        used_full_fallback = True

    if not raw_path:
        return fail(ctx, "All download attempts failed for all clients",
                    clipIndex=clip_index, title=title, start=start, end=end,
                    totalClips=total_clips, shortTitle=short_title, description=description)

    w, h = _probe_resolution(raw_path)
    log.info("Final source: %dx%d via client=%s", w, h, client)

    # If we downloaded the full video as fallback, pass the real start offset to FFmpeg.
    # If we downloaded just the section, the clip starts at 0.
    ffmpeg_start = start_sec if used_full_fallback else 0.0
    ok_ff, ff_err = _ffmpeg_crop_to_short(raw_path, clip_out, ffmpeg_start, dur)
    _safe_remove(raw_path)

    if not ok_ff:
        return fail(ctx, f"ffmpeg crop failed: {ff_err}", clipIndex=clip_index, title=title, start=start, end=end,
                    totalClips=total_clips, shortTitle=short_title, description=description)
    if not os.path.exists(clip_out):
        return fail(ctx, "ffmpeg exited 0 but output missing", clipIndex=clip_index, title=title, start=start, end=end,
                    totalClips=total_clips, shortTitle=short_title, description=description)

    out_w, out_h = _probe_resolution(clip_out)
    log.info("Output clip: %dx%d (%.1f MB)", out_w, out_h, os.path.getsize(clip_out) / 1e6)
    return ok(ctx, clipPath=clip_out, clipIndex=clip_index, title=title,
              start=start, end=end, sourceResolution=f"{w}x{h}", client=client,
              totalClips=total_clips, shortTitle=short_title, description=description)

@app.route("/download_full", methods=["POST"])
@timed
def download_full():
    data = request.json or {}
    ctx  = _base_ctx(data)
    vid  = ctx["videoId"]
    if not vid:
        return fail(ctx, "Missing videoId")
    url      = f"https://www.youtube.com/watch?v={vid}"
    out_base = f"{TMP_DIR}/{vid}"
    out_tpl  = f"{out_base}.%(ext)s"
    out_path, client = _download_best_quality(url, out_tpl, DOWNLOAD_TIMEOUT)
    if not out_path:
        return fail(ctx, "Download failed for all clients")
    w, h = _probe_resolution(out_path)
    log.info("Full download: %dx%d via %s", w, h, client)
    return ok(ctx, videoPath=out_path, resolution=f"{w}x{h}", client=client)

@app.route("/transcribe_chunked", methods=["POST"])
@timed
def transcribe_chunked():
    data       = request.json or {}
    ctx        = _base_ctx(data)
    vid        = ctx["videoId"]
    video_path = data.get("videoPath", "")
    if not vid:
        return fail(ctx, "Missing videoId")
    if not video_path or not os.path.exists(video_path):
        return fail(ctx, f"videoPath not found: {video_path}")
    try:
        _run([
            sys.executable, "-m", "whisper", video_path,
            "--output_format", "json", "--output_dir", TMP_DIR,
            "--model", WHISPER_MODEL, "--fp16", "False",
        ], timeout=WHISPER_TIMEOUT, retries=0)
    except RuntimeError as e:
        return fail(ctx, f"Whisper failed: {e}", videoPath=video_path)
    base_name    = os.path.splitext(os.path.basename(video_path))[0]
    transcript_p = f"{TMP_DIR}/{base_name}.json"
    if not os.path.exists(transcript_p):
        return fail(ctx, "Whisper produced no output", videoPath=video_path)
    try:
        with open(transcript_p, encoding="utf-8") as f:
            td = json.load(f)
    except Exception as e:
        return fail(ctx, f"Parse Whisper JSON failed: {e}", videoPath=video_path)
    finally:
        _safe_remove(transcript_p)
    segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()}
            for s in td.get("segments", []) if s.get("text", "").strip()]
    if not segs:
        return fail(ctx, "Whisper empty transcript", videoPath=video_path)
    return ok(ctx, segments=segs, segmentCount=len(segs), videoPath=video_path,
              detectedLanguage=td.get("language", "en"))

@app.route("/clip", methods=["POST"])
@timed
def clip():
    data       = request.json or {}
    ctx        = _base_ctx(data)
    vid        = ctx["videoId"]
    video_path = data.get("videoPath", "")
    start      = data.get("start",     "00:00")
    end        = data.get("end",       "01:00")
    clip_index  = int(data.get("clipIndex", 1))
    title       = data.get("title",       f"clip{clip_index}")
    total_clips = data.get("totalClips",  1)
    short_title = data.get("shortTitle",  "")
    description = data.get("description", "#Shorts #YouTubeShorts")
    if not vid:
        return fail(ctx, "Missing videoId")
    if not video_path or not os.path.exists(video_path):
        return fail(ctx, f"videoPath not found: {video_path}")

    def _ts(ts):
        p = [float(x) for x in ts.strip().split(":")]
        return p[0]*3600+p[1]*60+p[2] if len(p)==3 else p[0]*60+p[1]

    clip_out = f"{TMP_DIR}/{vid}_clip{clip_index}.mp4"
    ok_ff, ff_err = _ffmpeg_crop_to_short(video_path, clip_out, _ts(start), _ts(end) - _ts(start))
    if not ok_ff:
        return fail(ctx, f"ffmpeg clip failed: {ff_err}", clipIndex=clip_index, title=title,
                    videoPath=video_path, totalClips=total_clips, shortTitle=short_title, description=description)
    return ok(ctx, clipPath=clip_out, clipIndex=clip_index, title=title, videoPath=video_path,
              start=start, end=end,
              totalClips=total_clips, shortTitle=short_title, description=description)

@app.route("/fetch_comment_timestamps", methods=["POST"])
@timed
def fetch_comment_timestamps():
    data = request.json or {}
    ctx  = _base_ctx(data)
    vid  = ctx["videoId"]
    if not vid:
        return fail(ctx, "Missing videoId")
    comments: list[str] = []
    if YT_API_KEY:
        try:
            pt = None
            for _ in range(3):
                params = {"part": "snippet", "videoId": vid, "maxResults": 100, "order": "relevance", "key": YT_API_KEY}
                if pt: params["pageToken"] = pt
                r = http_requests.get("https://www.googleapis.com/youtube/v3/commentThreads", params=params, timeout=12)
                if not r.ok: break
                for item in r.json().get("items", []):
                    t = item.get("snippet",{}).get("topLevelComment",{}).get("snippet",{}).get("textDisplay","")
                    if t: comments.append(t)
                pt = r.json().get("nextPageToken")
                if not pt: break
        except Exception as e:
            log.warning("YT API comments error: %s", e)
    if not comments:
        out_base  = f"{TMP_DIR}/{vid}_cmt"
        info_file = f"{out_base}.info.json"
        try:
            _run(_yt_base_cmd("web", "--write-comments", "--skip-download", "--output", out_base,
                              f"https://www.youtube.com/watch?v={vid}"), timeout=90)
            if os.path.exists(info_file):
                with open(info_file, encoding="utf-8") as f:
                    for c in json.load(f).get("comments", []):
                        if isinstance(c, dict): comments.append(c.get("text",""))
        except Exception as e:
            log.warning("yt-dlp comments fallback failed: %s", e)
        finally:
            _safe_remove(info_file)
    if not comments:
        return fail(ctx, "No comments retrievable")
    pat    = r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b"
    all_ts = []
    for c in comments:
        for h, mi, s in re.findall(pat, c):
            total = int(h or 0)*3600 + int(mi)*60 + int(s)
            if total > 10: all_ts.append(total)
    if not all_ts:
        return fail(ctx, "No timestamps in comments")
    counter: Counter = Counter((ts // 15) * 15 for ts in all_ts)
    clips = [{"clipIndex": i+1, "title": f"Top moment at {_s2ts(ts)}", "start": _s2ts(ts), "end": _s2ts(ts+50), "mention_count": cnt, **ctx}
             for i, (ts, cnt) in enumerate(counter.most_common(4))]
    return ok(ctx, clips=clips, totalComments=len(comments))

@app.route("/generate_title", methods=["POST"])
@timed
def generate_title():
    data       = request.json or {}
    ctx        = _base_ctx(data)
    clip_title = data.get("clipTitle", "")
    clip_text  = data.get("clipText",  "")
    lang       = data.get("detectedLanguage", "en")
    fallback   = (clip_title or ctx["videoTitle"] or "Watch This")[:80].strip()
    if not fallback.lower().endswith("#shorts"):
        fallback = f"{fallback} #Shorts"
    if LOCAL_ONLY or not (GEMINI_API_KEY or OPENAI_API_KEY):
        return ok(ctx, title=fallback, fallback=True)
    lang_note = "Write in Hinglish (Roman script Hindi-English mix)." if lang in ("hi","ur") else "Write in English."
    prompt = (
        f"Generate ONE YouTube Shorts title (max 80 chars, no hashtags).\n"
        f"Requirements: curiosity-gap hook, not clickbait, punchy, present-tense.\n"
        f"Make it specific to what actually happens — never generic like 'Clip 1'.\n"
        f"{lang_note}\n\n"
        f"Original video: \"{ctx['videoTitle']}\"\n"
        f"Clip topic: \"{clip_title}\"\n"
        + (f"Transcript: \"{clip_text[:300]}\"\n" if clip_text else "")
        + "\nReturn ONLY the title text, nothing else."
    )
    result = _ai_call(prompt)
    if not result:
        return ok(ctx, title=fallback, fallback=True)
    title = result.strip('"\'` \n').split("\n")[0][:80]
    return ok(ctx, title=title, fallback=False)

@app.route("/generate_hook", methods=["POST"])
@timed
def generate_hook():
    data       = request.json or {}
    ctx        = _base_ctx(data)
    clip_title = data.get("clipTitle", "")
    clip_text  = data.get("clipText",  "")
    start      = data.get("start",     "")
    lang       = data.get("detectedLanguage", "en")
    fallback   = (f"You won't believe what happened at {start} 👀" if start
                  else f"This moment from \"{ctx['videoTitle']}\" is wild 🔥")
    if LOCAL_ONLY or not (GEMINI_API_KEY or OPENAI_API_KEY):
        return ok(ctx, hook=fallback, fallback=True)
    lang_note = "Write in Hinglish (Roman script Hindi-English mix)." if lang in ("hi","ur") else "Write in English."
    prompt = (
        f"Generate ONE YouTube Shorts description hook (1 sentence, max 120 chars).\n"
        f"Must create curiosity. Add 2 relevant emojis. No hashtags.\n"
        f"{lang_note}\n\n"
        f"Video: \"{ctx['videoTitle']}\"\nClip: \"{clip_title}\"\n"
        + (f"Transcript: \"{clip_text[:200]}\"\n" if clip_text else "")
        + "\nReturn ONLY the hook sentence."
    )
    result = _ai_call(prompt)
    if not result:
        return ok(ctx, hook=fallback, fallback=True)
    return ok(ctx, hook=result.strip('"\'` \n').split("\n")[0][:120], fallback=False)

@app.route("/mark_processed", methods=["POST"])
@timed
def mark_processed():
    data       = request.json or {}
    ctx        = _base_ctx(data)
    vid        = ctx["videoId"]
    chan       = ctx["channelId"]
    to_delete  = data.get("pathsToDelete", [])
    clip_index = data.get("clipIndex")
    status     = data.get("status", "completed")
    if not vid or not chan:
        return fail(ctx, "Missing videoId or channelId")
    # Lock prevents race condition when 4 clips call this simultaneously
    with _processed_lock:
        processed = {}
        try:
            if os.path.exists(PROCESSED_FILE):
                with open(PROCESSED_FILE, encoding="utf-8") as f:
                    processed = json.load(f)
        except Exception:
            pass
        existing = processed.get(chan, {})
        if existing.get("videoId") != vid:
            existing = {"videoId": vid, "processedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "clips": [], "status": status}
        else:
            existing["status"] = status
        if clip_index is not None:
            clips = existing.get("clips", [])
            if not any(c.get("clipIndex") == clip_index for c in clips):
                clips.append({"clipIndex": clip_index, "start": data.get("start",""), "end": data.get("end",""), "uploadedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            existing["clips"] = clips
        processed[chan] = existing
        try:
            with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
                json.dump(processed, f, indent=2)
        except Exception as e:
            return fail(ctx, f"Could not write processed file: {e}")
    for p in (to_delete or []):
        if p and isinstance(p, str): _safe_remove(p)
    # Echo back all clip fields so n8n's Read Clip Binary node can use items[0].json directly
    return ok(ctx,
        message=f"Marked {vid} clip {clip_index} as {status}",
        clipIndex=clip_index,
        totalClips=data.get("totalClips", 1),
        clipPath=data.get("clipPath"),
        videoPath=data.get("videoPath"),
        shortTitle=data.get("shortTitle", ""),
        description=data.get("description", "#Shorts #YouTubeShorts"),
        title=data.get("title", ""),
        start=data.get("start", ""),
        end=data.get("end", ""),
    )

@app.route("/cleanup", methods=["POST"])
@timed
def cleanup():
    now = time.time()
    removed, errors = [], []
    try:
        for fname in os.listdir(TMP_DIR):
            fpath = os.path.join(TMP_DIR, fname)
            if not os.path.isfile(fpath) or fname == "processed_videos.json":
                continue
            if not fname.endswith((".mp4", ".part", ".ytdl", ".json3", ".vtt", ".wav")):
                continue
            try:
                if now - os.path.getmtime(fpath) > 7200:
                    os.remove(fpath)
                    removed.append(fname)
            except Exception as e:
                errors.append(str(e))
    except Exception as e:
        return jsonify({"success": False, "error": f"Cleanup failed: {e}"}), 200
    log.info("Cleanup: removed %d files", len(removed))
    return jsonify({"success": True, "removed": removed, "removedCount": len(removed), "errors": errors}), 200

@app.route("/read_file", methods=["POST"])
def read_file():
    data      = request.json or {}
    file_path = data.get("filePath", "")
    if not file_path or not os.path.exists(file_path):
        return jsonify({"success": False, "error": f"Not found: {file_path}"}), 200
    return send_file(file_path, mimetype="video/mp4", as_attachment=False,
                     download_name=os.path.basename(file_path))

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

def _startup_checks():
    log.info("=" * 55)
    log.info("YouTube Shorts Backend v12")
    log.info("=" * 55)
    for binary in ["yt-dlp", "ffmpeg", "ffprobe"]:
        log.info("  %s  %s", "✓" if shutil.which(binary) else "✗", binary)
    log.info("  Quality clients: %s", " → ".join(QUALITY_CLIENTS))
    log.info("  AI: %s (%s)", AI_PROVIDER, "key set" if (GEMINI_API_KEY or OPENAI_API_KEY) else "NO KEY")
    log.info("  TMP: %s", TMP_DIR)
    log.info("=" * 55)

if __name__ == "__main__":
    import signal, traceback
    def _handle_sigterm(sig, frame):
        log.info("Received signal %s — shutting down", sig)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    _startup_checks()
    while True:
        try:
            app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
        except SystemExit:
            break
        except Exception as e:
            log.error("Flask crashed: %s\n%s", e, traceback.format_exc())
            log.info("Restarting in 3s...")
            time.sleep(3)