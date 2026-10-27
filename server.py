import io
import os
import subprocess
import urllib.parse
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from ytmusicapi import YTMusic
import yt_dlp
import requests
from PIL import Image

app = FastAPI(title="PSYTMusic Render Streaming Server", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize anonymous YouTube Music client
try:
    ytmusic = YTMusic()
except Exception:
    ytmusic = None

# In-memory cache for resolved direct audio URLs (expires after 2 hours)
import time
url_cache = {}  # video_id -> (url, timestamp)
CACHE_TTL = 7200

def get_cached_url(video_id: str) -> str:
    now = time.time()
    if video_id in url_cache:
        url, ts = url_cache[video_id]
        if now - ts < CACHE_TTL:
            return url
    return None

def set_cached_url(video_id: str, url: str):
    url_cache[video_id] = (url, time.time())
    # Keep cache under 500 items
    if len(url_cache) > 500:
        oldest = min(url_cache.keys(), key=lambda k: url_cache[k][1])
        del url_cache[oldest]

def extract_audio_url(video_id: str) -> str:
    cached = get_cached_url(video_id)
    if cached:
        return cached

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
        'nocheckcertificate': True,
        'source_address': '0.0.0.0',
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        audio_url = info.get('url')
        if not audio_url:
            formats = info.get('formats', [])
            for f in reversed(formats):
                if f.get('vcodec') == 'none' and f.get('url'):
                    audio_url = f.get('url')
                    break
        if not audio_url:
            raise HTTPException(status_code=404, detail="Could not resolve direct audio stream")
        set_cached_url(video_id, audio_url)
        return audio_url

def search_songs(query: str):
    """Searches YouTube Music, with yt-dlp fallback."""
    results = []
    if ytmusic:
        try:
            raw = ytmusic.search(query, filter="songs")
            for item in raw[:25]:
                vid = item.get("videoId")
                if not vid:
                    continue
                title = item.get("title", "Unknown Title")
                artists = item.get("artists", [])
                artist_name = artists[0]["name"] if artists else "Unknown Artist"
                duration = item.get("duration", "0:00")
                thumbs = item.get("thumbnails", [])
                thumb_url = thumbs[-1]["url"] if thumbs else ""

                results.append({
                    "id": vid,
                    "title": title[:48].replace("|", " "),
                    "artist": artist_name[:36].replace("|", " "),
                    "duration": duration,
                    "thumb": thumb_url
                })
        except Exception as e:
            print(f"ytmusic search error: {e}")

    # Fallback to yt-dlp search if ytmusic failed or returned nothing
    if not results:
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extract_flat': 'in_playlist',
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch20:{query}", download=False)
                for entry in info.get('entries', []):
                    vid = entry.get('id')
                    if not vid:
                        continue
                    dur_sec = entry.get('duration') or 0
                    m = int(dur_sec // 60)
                    s = int(dur_sec % 60)
                    dur_str = f"{m}:{s:02d}"
                    results.append({
                        "id": vid,
                        "title": entry.get('title', 'Unknown')[:48].replace("|", " "),
                        "artist": (entry.get('uploader') or 'Unknown')[:36].replace("|", " "),
                        "duration": dur_str,
                        "thumb": entry.get('thumbnail') or ""
                    })
        except Exception as e:
            print(f"yt-dlp search error: {e}")

    return results

@app.get("/health")
@app.get("/status")
def health():
    return {
        "status": "online",
        "service": "PSYTMusic Render Streaming Server",
        "ffmpeg": bool(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0) if os.name != "nt" else True,
        "cached_streams": len(url_cache)
    }

@app.get("/", response_class=HTMLResponse)
def index():
    return """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>PSYTMusic Cloud Server</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 900px; margin: 30px auto; padding: 0 20px; background: #0a0a0a; color: #f0f0f0; }
        h1 { color: #0070d1; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; }
        .badge { background: #1b5e20; color: #a5d6a7; padding: 4px 10px; border-radius: 12px; font-size: 13px; font-weight: bold; }
        .card { background: #161616; padding: 20px; border-radius: 10px; margin: 20px 0; border: 1px solid #282828; }
        input[type=text] { width: 70%; padding: 12px 16px; font-size: 16px; border-radius: 6px; border: 1px solid #444; background: #222; color: #fff; box-sizing: border-box; }
        button { padding: 12px 24px; font-size: 15px; border-radius: 6px; border: none; background: #0070d1; color: #fff; cursor: pointer; font-weight: bold; }
        button:hover { background: #0088ff; }
        .item { display: flex; align-items: center; gap: 15px; margin: 10px 0; padding: 12px; background: #1f1f1f; border-radius: 8px; border: 1px solid #333; }
        .item img { width: 52px; height: 52px; border-radius: 6px; object-fit: cover; background: #333; }
        .info { flex-grow: 1; }
        .title { font-weight: bold; margin-bottom: 4px; font-size: 15px; }
        .artist { color: #aaa; font-size: 13px; }
        .raw-box { background: #0d0d0d; border: 1px solid #333; padding: 14px; border-radius: 6px; font-family: monospace; font-size: 13px; white-space: pre-wrap; color: #4af626; max-height: 180px; overflow-y: auto; }
        audio { width: 100%; margin-top: 15px; }
    </style>
</head>
<body>
    <h1>PSYTMusic Cloud Server <span class="badge">ONLINE</span></h1>
    <p>High-performance audio streaming & search server with native FFmpeg transcoding for Sony PSP.</p>

    <div class="card">
        <h3>Live Search & Transcode Test:</h3>
        <div style="display: flex; gap: 10px; margin-bottom: 20px;">
            <input type="text" id="query" value="Daft Punk" placeholder="Search songs or artists...">
            <button onclick="doSearch()">Search</button>
        </div>

        <h4>Raw Response to PSP (<code>/search_txt</code> format):</h4>
        <div id="rawOutput" class="raw-box">Type a song and click Search...</div>

        <h4 style="margin-top:25px;">Parsed Results & Browser Audio Player:</h4>
        <div id="results"></div>
        <audio id="audioPlayer" controls style="display:none;"></audio>
    </div>

    <script>
        async function doSearch() {
            const q = document.getElementById('query').value.trim();
            if (!q) return;
            document.getElementById('rawOutput').textContent = "Searching YouTube Music and converting for PSP...";
            document.getElementById('results').innerHTML = "";

            try {
                const resp = await fetch('/search_txt?q=' + encodeURIComponent(q));
                const text = await resp.text();
                document.getElementById('rawOutput').textContent = text;

                const lines = text.split('\\n');
                const resultsDiv = document.getElementById('results');
                resultsDiv.innerHTML = "";

                lines.forEach(line => {
                    if (!line.trim()) return;
                    const parts = line.split('|');
                    if (parts.length >= 4) {
                        const [id, title, artist, dur] = parts;
                        const div = document.createElement('div');
                        div.className = 'item';
                        div.innerHTML = `
                            <div class="info">
                                <div class="title">${title}</div>
                                <div class="artist">${artist} &bull; ${dur} (<code>${id}</code>)</div>
                            </div>
                            <button onclick="playSong('${id}', '${title.replace(/'/g, '')}')">&#9654; Stream MP3</button>
                        `;
                        resultsDiv.appendChild(div);
                    }
                });
            } catch (e) {
                document.getElementById('rawOutput').textContent = "Error: " + e;
            }
        }

        function playSong(id, title) {
            const player = document.getElementById('audioPlayer');
            player.style.display = 'block';
            player.src = '/stream/' + id + '.mp3';
            player.play();
        }

        window.onload = () => { doSearch(); };
    </script>
</body>
</html>"""

@app.get("/search")
def api_search(q: str):
    if not q or not q.strip():
        return []
    return search_songs(q.strip())

@app.get("/search_txt", response_class=PlainTextResponse)
def api_search_txt(q: str):
    """
    Returns line-delimited plain-text for Sony PSP:
    id|title|artist|duration
    """
    if not q or not q.strip():
        return ""
    songs = search_songs(q.strip())
    if not songs:
        return f"none|No songs found for: {q[:20]}|Check spelling|0:00"
    lines = [f"{s['id']}|{s['title']}|{s['artist']}|{s['duration']}" for s in songs]
    return "\n".join(lines)

def build_mp3_stream(audio_url: str):
    """
    Transcodes any YouTube audio stream on-the-fly to strict CBR 128kbps 44.1kHz stereo MP3.
    This guarantees 100% compatibility with Sony PSP hardware Media Engine / sceMp3 decoder.
    """
    ffmpeg_cmd = [
        "ffmpeg",
        "-loglevel", "quiet",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", audio_url,
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-f", "mp3",
        "pipe:1"
    ]

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=65536
    )

    def iterfile():
        try:
            while True:
                data = proc.stdout.read(8192)
                if not data:
                    break
                yield data
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()

    return StreamingResponse(
        iterfile(),
        media_type="audio/mpeg",
        headers={
            "Content-Type": "audio/mpeg",
            "Accept-Ranges": "none",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600"
        }
    )

@app.get("/stream/{video_id}.mp3")
def stream_mp3_by_path(video_id: str):
    audio_url = extract_audio_url(video_id)
    return build_mp3_stream(audio_url)

@app.get("/stream")
def stream_by_query(id: str = None, video_id: str = None, format: str = "mp3"):
    vid = id or video_id
    if not vid:
        raise HTTPException(status_code=400, detail="Missing id parameter")
    # Clean id if passed with extension
    vid = vid.replace(".mp3", "").replace(".pcm", "").replace(".m4a", "")
    audio_url = extract_audio_url(vid)

    if format == "pcm":
        # Raw signed 16-bit LE stereo 44.1kHz PCM for sceAudioOutputBlocking with 0% CPU decode
        ffmpeg_cmd = [
            "ffmpeg",
            "-loglevel", "quiet",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", audio_url,
            "-vn",
            "-c:a", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            "-f", "s16le",
            "pipe:1"
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=65536)
        def iterpcm():
            try:
                while True:
                    data = proc.stdout.read(8192)
                    if not data:
                        break
                    yield data
            finally:
                if proc.poll() is None:
                    proc.terminate()
        return StreamingResponse(iterpcm(), media_type="application/octet-stream", headers={"Access-Control-Allow-Origin": "*"})

    return build_mp3_stream(audio_url)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
