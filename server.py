import io
import os
import sys
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

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

# Load cookies from YOUTUBE_COOKIES environment variable if provided
if os.environ.get("YOUTUBE_COOKIES"):
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(os.environ["YOUTUBE_COOKIES"].strip())
        print(f"--- Successfully loaded YouTube cookies from environment variable ({os.path.getsize(COOKIE_FILE)} bytes) ---")
    except Exception as e:
        print(f"Error loading YOUTUBE_COOKIES env var: {e}")

def has_cookies() -> bool:
    return os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 30

def get_cookie_args() -> list:
    if has_cookies():
        return ["--cookies", COOKIE_FILE]
    return []

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

def extract_piped_proxy_url(video_id: str) -> str:
    """Extract proxied audio URL from Piped instances (never IP-locked)."""
    piped_mirrors = [
        "https://pipedapi.kavin.rocks",
        "https://api.piped.privacy.com.de",
        "https://pipedapi.tokhmi.xyz",
        "https://piped-api.lunar.icu"
    ]
    for mirror in piped_mirrors:
        try:
            resp = requests.get(f"{mirror}/streams/{video_id}", timeout=2.5)
            if resp.status_code == 200:
                data = resp.json()
                for stream in data.get("audioStreams", []):
                    proxy_url = stream.get("proxyUrl")
                    if proxy_url:
                        return proxy_url
        except Exception:
            continue
    return None

def extract_audio_url(video_id: str) -> str:
    cached = get_cached_url(video_id)
    if cached:
        return cached

    # Tier 1: YouTube Music InnerTube player API (uses ytmusic's active auth session)
    if ytmusic:
        try:
            player_data = ytmusic._send_request("player", {"videoId": video_id})
            streaming_data = player_data.get("streamingData", {})
            formats = streaming_data.get("formats", []) + streaming_data.get("adaptiveFormats", [])
            for f in formats:
                if "audio" in f.get("mimeType", "") and f.get("url"):
                    audio_url = f["url"]
                    set_cached_url(video_id, audio_url)
                    return audio_url
        except Exception as e:
            print(f"ytmusic InnerTube extract error: {e}")

    # Tier 2: Piped Proxy Stream (proxied on mirror, zero Google IP-lock)
    try:
        proxy_url = extract_piped_proxy_url(video_id)
        if proxy_url:
            set_cached_url(video_id, proxy_url)
            return proxy_url
    except Exception as e:
        print(f"Piped proxy extract error: {e}")

    # Tier 3: yt-dlp client extraction
    clients = [None, ['mweb']] if has_cookies() else [['android'], ['ios'], None]
    for client_list in clients:
        try:
            ydl_opts = {
                'format': 'ba/140/251/250/249/18/b',
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extract_flat': False,
                'nocheckcertificate': True,
                'socket_timeout': 6,
            }
            if has_cookies() and (not client_list or client_list[0] not in ['android', 'ios']):
                ydl_opts['cookiefile'] = COOKIE_FILE
            if client_list:
                ydl_opts['extractor_args'] = {'youtube': {'player_client': client_list}}

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                audio_url = info.get('url')
                if not audio_url:
                    formats = info.get('formats', [])
                    for f in reversed(formats):
                        if f.get('url'):
                            audio_url = f.get('url')
                            break
                if audio_url:
                    set_cached_url(video_id, audio_url)
                    return audio_url
        except Exception as e:
            print(f"yt-dlp extract error ({client_list}): {e}")

    return None

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

    <div class="card" style="border-left: 4px solid #0070d1;">
        <h3>YouTube Authentication (Bypass Bot Check on Render)</h3>
        <p style="color: #bbb; font-size: 14px; margin-bottom: 12px;">
            Render's cloud IP requires YouTube cookies to bypass <code>Sign in to confirm you're not a bot</code>.
        </p>
        <div id="cookieStatus" style="font-weight: bold; margin-bottom: 12px; font-size: 15px;">Checking cookie status...</div>
        <textarea id="cookieInput" style="width: 100%; height: 90px; background: #222; color: #fff; border: 1px solid #444; border-radius: 6px; padding: 10px; font-family: monospace; font-size: 12px; box-sizing: border-box;" placeholder="Paste exported Netscape cookies.txt here..."></textarea>
        <div style="margin-top: 10px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
            <button onclick="saveCookies()">Save YouTube Cookies</button>
            <span style="font-size: 13px; color: #888;">Tip: Use browser extension <strong>Get cookies.txt LOCALLY</strong> on youtube.com, click Export, and paste here.</span>
        </div>
    </div>

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

        async function checkCookieStatus() {
            try {
                const resp = await fetch('/cookies_status');
                const data = await resp.json();
                const el = document.getElementById('cookieStatus');
                if (data.authenticated) {
                    el.innerHTML = '<span style="color: #4af626;">&#10004; ' + data.message + '</span>';
                } else {
                    el.innerHTML = '<span style="color: #ff5252;">&#9888; ' + data.message + '</span>';
                }
            } catch (e) {
                document.getElementById('cookieStatus').textContent = "Status: offline";
            }
        }

        async function saveCookies() {
            const text = document.getElementById('cookieInput').value.trim();
            if (!text) {
                alert('Please paste exported YouTube cookies first.');
                return;
            }
            try {
                const resp = await fetch('/save_cookies', {
                    method: 'POST',
                    headers: { 'Content-Type': 'text/plain' },
                    body: text
                });
                const res = await resp.json();
                alert(res.message || 'Cookies saved successfully!');
                document.getElementById('cookieInput').value = '';
                checkCookieStatus();
            } catch (e) {
                alert('Error saving cookies: ' + e);
            }
        }

        window.onload = () => {
            checkCookieStatus();
            doSearch();
        };
    </script>
</body>
</html>"""

@app.get("/cookies_status")
def api_cookies_status():
    if has_cookies():
        size = os.path.getsize(COOKIE_FILE)
        return {
            "authenticated": True,
            "size": size,
            "message": f"YouTube authentication active ({size} bytes). Bot check bypassed!"
        }
    return {
        "authenticated": False,
        "size": 0,
        "message": "No YouTube cookies found. Bot check active on Render."
    }

@app.post("/save_cookies")
async def api_save_cookies(request: Request):
    try:
        body = await request.body()
        content = body.decode("utf-8").strip()
        if not content:
            raise HTTPException(status_code=400, detail="Empty cookies content")
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        return {
            "status": "ok",
            "message": f"Successfully installed YouTube cookies ({len(content)} bytes). Bot check bypassed!"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    Transcodes resolved YouTube audio URL to strict CBR 128kbps 44.1kHz stereo MP3 for PSP.
    Pre-checks initial bytes to ensure FFmpeg successfully connected.
    """
    ffmpeg_cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36\r\nReferer: https://music.youtube.com/\r\n",
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
        stderr=subprocess.PIPE,
        bufsize=65536
    )

    try:
        first_chunk = proc.stdout.read(8192)
    except Exception as e:
        print(f"Error reading first chunk from FFmpeg URL stream: {e}")
        first_chunk = b""

    if not first_chunk:
        err = proc.stderr.read().decode(errors="ignore")
        print(f"FFmpeg URL stream failed (0 bytes): {err.strip()}")
        if proc.poll() is None:
            proc.terminate()
        return None

    def iterfile():
        yield first_chunk
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

def stream_mp3_invidious_proxy(video_id: str):
    """
    Proxies stream directly from public Invidious instances using local=true.
    This routes the audio bytes through Invidious instead of Google's datacenter IP-locked CDN,
    bypassing all bot checks and IP locks.
    """
    mirrors = [
        "https://inv.nadeko.net",
        "https://invidious.nerdvpn.de",
        "https://invidious.drgns.space",
        "https://invidious.lunar.icu",
        "https://yewtu.be",
        "https://invidious.private.coffee"
    ]
    for itag in ["140", "251", "18"]:
        for m in mirrors:
            try:
                url = f"{m}/latest_version?id={video_id}&itag={itag}&local=true"
                resp = requests.head(url, timeout=2.5, allow_redirects=True, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                if resp.status_code == 200:
                    print(f"Invidious proxy succeeded for {video_id} via {m} (itag {itag})")
                    return build_mp3_stream(url)
            except Exception:
                continue
    return None

def stream_mp3_ytdlp_pipe(video_id: str):
    """
    Direct pipe from yt-dlp to FFmpeg.
    When cookies exist, prioritizes authenticated Web/MWeb clients.
    FFmpeg reads pipe:0, eliminating external CDN IP-locks.
    """
    if has_cookies():
        client_configs = [
            [],  # Web client with cookies
            ["--extractor-args", "youtube:player_client=mweb"],
            ["--extractor-args", "youtube:player_client=web"]
        ]
    else:
        client_configs = [
            ["--extractor-args", "youtube:player_client=android"],
            ["--extractor-args", "youtube:player_client=ios"],
            []
        ]

    for extra_args in client_configs:
        is_mobile = any("android" in str(a) or "ios" in str(a) for a in extra_args)
        cookie_args = [] if is_mobile else get_cookie_args()

        ytdlp_cmd = [
            sys.executable, "-m", "yt_dlp",
            "--quiet",
            "--no-warnings",
            "--no-playlist"
        ] + cookie_args + extra_args + [
            "-f", "ba/140/251/250/249/18/b",
            "-o", "-",
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        proc_ytdlp = subprocess.Popen(
            ytdlp_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        ffmpeg_cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-i", "pipe:0",
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-f", "mp3",
            "pipe:1"
        ]
        proc_ffmpeg = subprocess.Popen(
            ffmpeg_cmd,
            stdin=proc_ytdlp.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=65536
        )
        if proc_ytdlp.stdout:
            proc_ytdlp.stdout.close()

        try:
            first_chunk = proc_ffmpeg.stdout.read(8192)
        except Exception as e:
            print(f"Error reading first chunk from yt-dlp pipe: {e}")
            first_chunk = b""

        if first_chunk:
            def iterpipe():
                yield first_chunk
                try:
                    while True:
                        data = proc_ffmpeg.stdout.read(8192)
                        if not data:
                            break
                        yield data
                finally:
                    if proc_ffmpeg.poll() is None:
                        proc_ffmpeg.terminate()
                        try:
                            proc_ffmpeg.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            proc_ffmpeg.kill()
                    if proc_ytdlp.poll() is None:
                        proc_ytdlp.terminate()
                        try:
                            proc_ytdlp.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            proc_ytdlp.kill()

            return StreamingResponse(
                iterpipe(),
                media_type="audio/mpeg",
                headers={
                    "Content-Type": "audio/mpeg",
                    "Accept-Ranges": "none",
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "public, max-age=3600"
                }
            )

        err_yt = proc_ytdlp.stderr.read().decode(errors="ignore") if proc_ytdlp.stderr else ""
        print(f"yt-dlp pipe ({extra_args}) failed for {video_id}: {err_yt.strip()[:120]}")
        if proc_ffmpeg.poll() is None:
            proc_ffmpeg.terminate()
        if proc_ytdlp.poll() is None:
            proc_ytdlp.terminate()

    return None

def stream_pcm_ytdlp_pipe(video_id: str):
    """
    Direct pipe from yt-dlp to FFmpeg raw PCM (16-bit signed LE 44.1kHz stereo) for PSP bridge mode.
    """
    if has_cookies():
        client_configs = [
            [],
            ["--extractor-args", "youtube:player_client=mweb"],
            ["--extractor-args", "youtube:player_client=web"]
        ]
    else:
        client_configs = [
            ["--extractor-args", "youtube:player_client=android"],
            ["--extractor-args", "youtube:player_client=ios"],
            []
        ]

    for extra_args in client_configs:
        is_mobile = any("android" in str(a) or "ios" in str(a) for a in extra_args)
        cookie_args = [] if is_mobile else get_cookie_args()

        ytdlp_cmd = [
            sys.executable, "-m", "yt_dlp",
            "--quiet",
            "--no-warnings",
            "--no-playlist"
        ] + cookie_args + extra_args + [
            "-f", "ba/140/251/250/249/18/b",
            "-o", "-",
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        proc_ytdlp = subprocess.Popen(
            ytdlp_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        ffmpeg_cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-i", "pipe:0",
            "-vn",
            "-c:a", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            "-f", "s16le",
            "pipe:1"
        ]
        proc_ffmpeg = subprocess.Popen(
            ffmpeg_cmd,
            stdin=proc_ytdlp.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=65536
        )
        if proc_ytdlp.stdout:
            proc_ytdlp.stdout.close()

        try:
            first_chunk = proc_ffmpeg.stdout.read(8192)
        except Exception as e:
            first_chunk = b""

        if first_chunk:
            def iterpcm():
                yield first_chunk
                try:
                    while True:
                        data = proc_ffmpeg.stdout.read(8192)
                        if not data:
                            break
                        yield data
                finally:
                    if proc_ffmpeg.poll() is None:
                        proc_ffmpeg.terminate()
                    if proc_ytdlp.poll() is None:
                        proc_ytdlp.terminate()

            return StreamingResponse(
                iterpcm(),
                media_type="application/octet-stream",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        if proc_ffmpeg.poll() is None:
            proc_ffmpeg.terminate()
        if proc_ytdlp.poll() is None:
            proc_ytdlp.terminate()

    return None

@app.get("/stream/{video_id}.mp3")
def stream_mp3_by_path(video_id: str):
    vid = video_id.replace(".mp3", "")
    print(f"--- Audio stream requested for {vid} ---")

    # Strategy 1: Direct yt-dlp -> FFmpeg process pipe (Zero IP-lock, cloud container native)
    resp = stream_mp3_ytdlp_pipe(vid)
    if resp:
        print(f"Streaming {vid} via yt-dlp pipe (SUCCESS)")
        return resp

    # Strategy 2: Invidious local=true proxy stream (Zero datacenter block)
    try:
        resp = stream_mp3_invidious_proxy(vid)
        if resp:
            print(f"Streaming {vid} via Invidious proxy (SUCCESS)")
            return resp
    except Exception as e:
        print(f"Strategy 2 Invidious error for {vid}: {e}")

    # Strategy 3: Extract direct audio URL via yt-dlp on Render and feed to FFmpeg
    try:
        audio_url = extract_audio_url(vid)
        if audio_url:
            resp = build_mp3_stream(audio_url)
            if resp:
                print(f"Streaming {vid} via resolved URL (SUCCESS)")
                return resp
    except Exception as e:
        print(f"Strategy 3 URL transcode error for {vid}: {e}")

    # If all strategies fail, return 502 Bad Gateway so PSP immediately triggers PC Bridge fallback
    print(f"CRITICAL: Failed to stream {vid} across all strategies -> Returning HTTP 502")
    raise HTTPException(status_code=502, detail=f"Audio stream unavailable for {vid}")

@app.get("/stream/{video_id}.pcm")
def stream_pcm_by_path(video_id: str):
    vid = video_id.replace(".pcm", "")
    resp = stream_pcm_ytdlp_pipe(vid)
    if resp:
        return resp
    raise HTTPException(status_code=502, detail=f"PCM stream unavailable for {vid}")

@app.get("/stream")
def stream_by_query(id: str = None, video_id: str = None, format: str = "mp3"):
    vid = id or video_id
    if not vid:
        raise HTTPException(status_code=400, detail="Missing id parameter")
    vid = vid.replace(".mp3", "").replace(".pcm", "").replace(".m4a", "")

    if format == "pcm":
        return stream_pcm_by_path(vid)

    return stream_mp3_by_path(vid)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
