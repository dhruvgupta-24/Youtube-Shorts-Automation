# YouTube Shorts Automation

An end-to-end automation system that monitors YouTube channels, identifies the most engaging moments in long-form videos, clips them to vertical 9:16 format, and uploads them as YouTube Shorts — fully automatically on a schedule.

Built with a **Python Flask backend** (`server.py`) and an **n8n workflow** (`Youtube Shorts Automation.json`) that orchestrates the full pipeline.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        n8n Workflow                              │
│                                                                  │
│  Schedule Trigger                                                │
│       │                                                          │
│       ▼                                                          │
│  Set Channels ──► Fetch Latest Video ──► Filter Shorts           │
│                          │                                       │
│                          ▼                                       │
│                  Check Processed (C:/tmp/processed_videos.json)  │
│                          │                                       │
│                          ▼                                       │
│              ┌── Detect Strategy ──┐                             │
│              │                     │                             │
│         Strategy 1            Strategy 2            Strategy 3   │
│     (has captions)       (needs Whisper)       (comment times)   │
│           │                    │                     │           │
│     Fetch Captions      Download Full          Comment           │
│     Score Transcript    Transcribe (Whisper)   Timestamps        │
│           │             Score Transcript                         │
│           └────────────────────┴──────────────────┘             │
│                                │                                 │
│                          Split Clips (1–4)                       │
│                                │  (parallel items)               │
│                                ▼                                 │
│                    Download Section / Clip                       │
│                          (FFmpeg 9:16 crop)                      │
│                                │                                 │
│                                ▼                                 │
│                    Mark Upload Started                           │
│                    Read Clip Binary                              │
│                    Upload to YouTube                             │
│                    Mark Processed / Cleanup                      │
└──────────────────────────────────────────────────────────────────┘
                              │
              HTTP POST to localhost:8000
                              │
┌─────────────────────────────▼────────────────────────────────────┐
│                    Flask Backend (server.py)                     │
│                                                                  │
│  /detect_strategy   /fetch_captions    /score_transcript         │
│  /download_section  /download_full     /transcribe_chunked       │
│  /clip              /mark_processed    /cleanup                  │
│  /generate_title    /generate_hook     /fetch_comment_timestamps │
└──────────────────────────────────────────────────────────────────┘
              │                │                │
           yt-dlp           FFmpeg           Gemini / OpenAI / Whisper
```

---

## Full Pipeline

| Step | Node | Description |
|------|------|-------------|
| 1 | Schedule Trigger | Fires every N minutes |
| 2 | Set / Parse Channels | Defines YouTube channels to monitor |
| 3 | Fetch Latest Video | YouTube Data API v3 — newest upload per channel |
| 4 | Fetch Video Duration | Filters videos < 5 min (already Shorts) |
| 5 | Filter Shorts | Skips `#shorts` tagged videos |
| 6 | Check Processed | Reads `processed_videos.json`; skips only if `status === 'completed'` |
| 7 | Detect Strategy | Determines strategy 1/2/3 based on caption availability |
| 8–10 | Strategy 1 | Fetch captions → Score transcript (AI) |
| 11–13 | Strategy 2 | Download full video → Whisper transcription → Score transcript |
| 14 | Strategy 3 | Extract timestamps from YouTube comments |
| 15 | Split Clips | Expands top 4 clips into parallel n8n items |
| 16 | Download / Clip | `yt-dlp` section download or FFmpeg clip from full video |
| 17 | Upload to YouTube | n8n YouTube node with OAuth2 |
| 18 | Mark Processed | Persists status to `processed_videos.json` |
| 19 | Cleanup | Removes temp files older than 2 hours |

---

## Requirements

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.11+ | |
| [FFmpeg](https://ffmpeg.org/) | Any recent | Must be on PATH or set `FFMPEG_BIN` |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Latest | `pip install yt-dlp` or standalone binary |
| [n8n](https://n8n.io/) | Self-hosted | Any recent version |
| Whisper | Latest | `pip install openai-whisper` |

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/dhruvgupta-24/Youtube-Shorts-Automation.git
cd Youtube-Shorts-Automation
```

### 2. Install Python dependencies

```bash
pip install flask requests openai-whisper yt-dlp
```

### 3. Install FFmpeg

Download from [ffmpeg.org](https://ffmpeg.org/download.html) and place the binaries at `C:\ffmpeg\bin` (or set `FFMPEG_BIN` in `.env` to your install path).

Verify:
```bash
ffmpeg -version
yt-dlp --version
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your API keys (see [Configuration](#configuration) below).

### 5. Set up YouTube cookies (optional but recommended)

Export your browser cookies for YouTube into `yt_cookies.txt` using the [cookies.txt extension](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc). This allows yt-dlp to access age-restricted or higher-quality streams.

> **Important:** `yt_cookies.txt` is gitignored and must never be committed.

### 6. Run the Flask server

```bash
python server.py
```

The server starts on `http://localhost:8000` by default.

### 7. Import the n8n workflow

1. Open your n8n instance
2. Go to **Workflows → Import from file**
3. Select `Youtube Shorts Automation.json`
4. Update all HTTP Request nodes: replace `YOUR_SERVER_IP` with your machine's local IP (or `localhost` if n8n runs on the same machine)
5. Update the YouTube API key in the **Fetch Latest Video** and **Fetch Video Duration** nodes
6. Connect your YouTube OAuth2 credential to the **Upload to YouTube** node
7. Edit the **Set Channels** node to add the YouTube channel IDs you want to monitor
8. Activate the workflow

---

## Configuration

All configuration is done via environment variables. Copy `.env.example` to `.env` and fill in:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `YOUTUBE_API_KEY` | Yes | — | YouTube Data API v3 key ([get one](https://console.cloud.google.com)) |
| `GEMINI_API_KEY` | Recommended | — | Google Gemini API key ([get one](https://aistudio.google.com/app/apikey)) |
| `OPENAI_API_KEY` | Optional | — | OpenAI API key (alternative to Gemini) |
| `AI_PROVIDER` | No | `gemini` | `"gemini"` or `"openai"` |
| `GEMINI_MODEL` | No | `gemini-2.0-flash` | Gemini model name |
| `OPENAI_MODEL` | No | `gpt-4o-mini` | OpenAI model name |
| `WHISPER_MODEL` | No | `tiny` | Whisper model size (`tiny`/`base`/`small`/`medium`/`large`) |
| `TMP_DIR` | No | `C:/tmp` | Directory for temp files and logs |
| `FFMPEG_BIN` | No | `C:/ffmpeg/bin` | Path to FFmpeg `bin/` directory |
| `LOCAL_ONLY` | No | `false` | Set `true` to disable all external AI calls |
| `WHISPER_TIMEOUT` | No | `1800` | Whisper transcription timeout (seconds) |
| `DOWNLOAD_TIMEOUT` | No | `900` | Full video download timeout (seconds) |
| `SECTION_TIMEOUT` | No | `600` | Section download timeout (seconds) |
| `AI_TIMEOUT` | No | `20` | AI API call timeout (seconds) |

---

## API Endpoints

All endpoints accept/return JSON. Every response includes `success`, `error`, `videoId`, `channelId`, `videoTitle`, `channelTitle` for data flow.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check — verifies yt-dlp, FFmpeg, tmp dir |
| `POST` | `/detect_strategy` | Probe video for captions; returns strategy 1/2/3 |
| `POST` | `/fetch_captions` | Fetch English captions (timedtext API → yt-dlp fallback) |
| `POST` | `/score_transcript` | AI-score segments; returns top 4 clip candidates |
| `POST` | `/download_section` | Download time-range via yt-dlp; crop to 9:16 via FFmpeg |
| `POST` | `/download_full` | Download entire video (strategy 2) |
| `POST` | `/transcribe_chunked` | Transcribe audio locally with Whisper |
| `POST` | `/clip` | Cut clip from pre-downloaded full video via FFmpeg |
| `POST` | `/fetch_comment_timestamps` | Extract timestamps from YouTube comments |
| `POST` | `/generate_title` | Generate a Shorts-optimised title via AI |
| `POST` | `/generate_hook` | Generate an opening hook via AI |
| `POST` | `/mark_processed` | Read/write `processed_videos.json` (thread-safe) |
| `POST` | `/cleanup` | Delete temp files older than 2 hours |
| `POST` | `/read_file` | Read a file from the server's filesystem |

---

## Download Quality Strategy

`/download_section` and `/download_full` try multiple yt-dlp player clients in order until ≥720p is obtained:

```
tv_embedded → web → ios → android_vr (HLS fallback, ~480p max)
```

---

## Known Issue — Multi-Clip Upload

Currently only 1 of 4 generated clips uploads successfully per run. All 4 video files are created in `C:/tmp`, but the n8n upload chain appears to process only the first item. Root cause is suspected to be an n8n execution model issue with parallel items in the upload chain. Under investigation.

---

## Project Structure

```
Youtube Automation/
├── server.py                        # Flask backend (main entry point)
├── Youtube Shorts Automation.json   # n8n workflow export
├── .env                             # Local secrets — DO NOT COMMIT
├── .env.example                     # Environment variable template
├── .gitignore
└── README.md
```

---

## Tech Stack

- **Python 3.11+** — Flask, requests, openai-whisper, yt-dlp
- **FFmpeg** — 9:16 crop with blurred background fill
- **yt-dlp** — YouTube downloading with multi-client quality fallback
- **Google Gemini / OpenAI GPT** — clip ranking, title and hook generation
- **OpenAI Whisper** (local) — transcription for videos without captions
- **n8n** (self-hosted) — workflow orchestration and scheduling
- **YouTube Data API v3** — video metadata and upload
- **Windows** primary dev environment (paths use forward slashes for cross-platform compat)

---

## Security Notes

- API keys are loaded exclusively from environment variables — no secrets in code
- `yt_cookies.txt` and `.env` are gitignored
- The n8n workflow previously contained a hardcoded YouTube API key — this has been replaced with a placeholder (`YOUR_YOUTUBE_API_KEY`). Update it in the n8n HTTP nodes after importing
- The workflow uses a local IP address (`172.16.218.236:8000`) for Flask server calls — update this to your machine's IP or `localhost` after importing