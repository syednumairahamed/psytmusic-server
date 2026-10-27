# PSYTMusic Streaming Server

High-performance YouTube Music streaming and search backend for Sony PSP (`PSYTMusic`).

- **Live FFmpeg Transcoding:** On-the-fly conversion of YouTube audio to strict 128kbps CBR MP3 (44.1kHz, 16-bit stereo) for Sony PSP hardware Media Engine (`sceMp3`).
- **YouTube Music API:** Fast search queries via `ytmusicapi` with automatic `yt-dlp` fallback.
- **Persistent HTTP Audio Streaming:** Native Docker deployment on Render with zero execution timeouts.

## Endpoints

- `GET /health` - Server health and FFmpeg status.
- `GET /search_txt?q=<query>` - Compact line-delimited format (`id|title|artist|duration`) for PSP.
- `GET /stream/<video_id>.mp3` - Live 128kbps CBR MP3 audio stream for PSP.
- `GET /` - Web dashboard for searching and streaming test.
