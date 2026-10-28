# PSYTMusic Google Drive Music Server (Render Cloud)

High-performance private Google Drive audio streaming and search server with native Linux **FFmpeg** hardware MP3 transcoding for Sony PSP.

- **Direct Google Drive Integration:** Streams FLAC (lossless), MP4/M4A, MP3, WAV, and OGG files directly from Google Cloud CDN.
- **Zero Bot-Checks or IP Locks:** Authenticates cleanly with Google Drive API v3 via Service Account key (`service_account.json` or `GOOGLE_SERVICE_ACCOUNT_JSON` env var).
- **On-the-Fly Hardware MP3 Transcoding:** Linux FFmpeg transcodes audio in real-time to 192kbps 44.1kHz stereo CBR MP3 specifically formatted for the Sony PSP Media Engine (`sceMp3`), keeping the PSP CPU at 0% load.
- **Persistent HTTP Audio Streaming:** Native Docker deployment on Render with zero execution timeouts or stream disconnects.

## API Endpoints

- `GET /health` or `GET /status` - Server health, Service Account status, and total songs indexed.
- `GET /search_txt?q=<query>` - Compact line-delimited format (`id|title|artist|duration\n`) for Sony PSP.
- `GET /search?q=<query>` - Full JSON search results for web frontend.
- `GET /rescan` - Triggers an immediate in-memory rescan of Google Drive.
- `GET /stream/<file_id>.mp3` - Live 192kbps CBR MP3 audio stream for Sony PSP.
- `GET /` - Interactive web management dashboard with HTML5 audio player and live status.
