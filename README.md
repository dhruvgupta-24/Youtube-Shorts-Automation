# YouTube Shorts Automation

A Flask-based backend that automates the end-to-end pipeline for turning long-form YouTube videos into short-form clips. It handles video discovery, transcript scoring, high-quality downloading, AI-powered clipping, and title/hook generation.

## Features

- **Multi-client downloading** — tries `tv_embedded → web → ios → android_vr` in order to obtain the highest quality (up to 1080p)
- **Transcript scoring** — uses AI (Gemini or OpenAI) to identify the top 4 most engaging clips from a video transcript
- **Whisper transcription** — falls back to local Whisper when YouTube captions are unavailable
- **AI title & hook generation** — generates short-form video titles and opening hooks via Gemini or OpenAI
- **Comment-timestamp mining** — extracts timestamps mentioned in YouTube comments for clip candidates
- **Processed-video tracking** — persists a JSON list of already-processed video IDs to avoid duplicates
- **Configurable via environment variables** — no hardcoded secrets

## Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/) (expected at `C:\ffmpeg\bin` or on `PATH`)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [Whisper](https://github.com/openai/whisper) (`openai-whisper`)

Install Python dependencies:

```bash
pip install flask requests openai-whisper yt-dlp
```

## Configuration

Set the following environment variables before starting the server:

| Variable | Description | Default |
|---|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key | `""` |
| `GEMINI_API_KEY` | Google Gemini API key | `""` |
| `OPENAI_API_KEY` | OpenAI API key | `""` |
| `AI_PROVIDER` | `gemini` or `openai` | `gemini` |
| `GEMINI_MODEL` | Gemini model name | `gemini-2.0-flash` |
| `OPENAI_MODEL` | OpenAI model name | `gpt-4o-mini` |
| `WHISPER_MODEL` | Whisper model size | `tiny` |
| `TMP_DIR` | Directory for temp files and logs | `C:/tmp` |
| `LOCAL_ONLY` | Disable external API calls | `false` |

## Running the Server

```bash
python server.py
```

The server starts on `http://localhost:5000` by default.

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/detect_strategy` | Detect best clipping strategy for a video |
| `POST` | `/fetch_captions` | Fetch YouTube captions/subtitles |
| `POST` | `/score_transcript` | AI-score transcript and return top 4 clip candidates |
| `POST` | `/download_section` | Download a specific time range of a video |
| `POST` | `/download_full` | Download the full video |
| `POST` | `/transcribe_chunked` | Transcribe audio using Whisper (chunked) |
| `POST` | `/clip` | Cut a clip from a downloaded video using FFmpeg |
| `POST` | `/fetch_comment_timestamps` | Extract timestamps from video comments |
| `POST` | `/generate_title` | Generate a short-form video title via AI |
| `POST` | `/generate_hook` | Generate an opening hook via AI |
| `POST` | `/mark_processed` | Mark a video ID as processed |
| `POST` | `/cleanup` | Delete temporary files for a video |
| `POST` | `/read_file` | Read a file from the server's filesystem |

## Project Structure

```
.
├── server.py                        # Flask backend (main entry point)
├── Youtube Shorts Automation.json   # n8n / workflow automation config
├── yt_cookies.txt                   # YouTube cookies for authenticated downloads
└── README.md
```

## Notes

- `yt_cookies.txt` is used by `yt-dlp` for age-restricted or member-only content. Keep it private and do not share it publicly.
- Logs are written to `{TMP_DIR}/server.log`.
- Processed video IDs are stored in `{TMP_DIR}/processed_videos.json`.
