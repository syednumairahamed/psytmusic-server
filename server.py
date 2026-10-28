import io
import os
import sys
import json
import uuid
import time
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
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")

OAUTH_CLIENT_ID = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
OAUTH_CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
OAUTH_SCOPES = "https://gdata.youtube.com https://www.googleapis.com/auth/youtube"

def load_oauth_token() -> dict:
    if os.environ.get("YOUTUBE_OAUTH"):
        try:
            return json.loads(os.environ["YOUTUBE_OAUTH"])
        except Exception:
            pass
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_oauth_token(data: dict):
    if not data:
        return
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving {TOKEN_FILE}: {e}")
    try:
        ydl = yt_dlp.YoutubeDL()
        ydl.cache.store('youtube-oauth2', 'token_data', data)
    except Exception as e:
        print(f"Error syncing to yt-dlp cache: {e}")

# Load initial token if present in environment
if load_oauth_token():
    save_oauth_token(load_oauth_token())
    print("--- Loaded YouTube OAuth credentials ---")

def has_oauth() -> bool:
    token = load_oauth_token()
    return bool(token and token.get("access_token") and token.get("refresh_token"))

def refresh_oauth_token():
    token = load_oauth_token()
    if not token or not token.get("refresh_token"):
        return None
    try:
        resp = requests.post(
            "https://www.youtube.com/o/oauth2/token",
            data=json.dumps({
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "refresh_token": token["refresh_token"],
                "grant_type": "refresh_token"
            }),
            headers={"Content-Type": "application/json", "__youtube_oauth__": "true"},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            token["access_token"] = data["access_token"]
            token["expires"] = time.time() + data.get("expires_in", 3600)
            if "refresh_token" in data:
                token["refresh_token"] = data["refresh_token"]
            save_oauth_token(token)
            return token
    except Exception as e:
        print(f"OAuth refresh error: {e}")
    return None

def has_cookies() -> bool:
    return os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 30

def get_auth_args() -> list:
    if has_oauth():
        token = load_oauth_token()
        if token and token.get("expires", 0) < time.time() + 60:
            token = refresh_oauth_token()
        if token:
            save_oauth_token(token)
            return ["--username", "oauth2", "--password", ""]
    if has_cookies():
        return ["--cookies", COOKIE_FILE]
    return []

def get_cookie_args() -> list:
    return get_auth_args()

# In-memory cache for resolved direct audio URLs (expires after 2 hours)
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
    if has_oauth():
        clients = [None, ['tv_embedded'], ['android_creator']]
    else:
        clients = [['visionos'], ['android_creator'], ['ios_creator'], ['tv_embedded'], ['android'], None]
    for client_list in clients:
        try:
            ydl_opts = {
                'format': 'ba/b',
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extract_flat': False,
                'nocheckcertificate': True,
                'socket_timeout': 6,
            }
            if has_oauth():
                ydl_opts['username'] = 'oauth2'
                ydl_opts['password'] = ''
            elif has_cookies():
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
        .card { background: #161616; padding: 22px; border-radius: 10px; margin: 20px 0; border: 1px solid #282828; }
        input[type=text] { width: 70%; padding: 12px 16px; font-size: 16px; border-radius: 6px; border: 1px solid #444; background: #222; color: #fff; box-sizing: border-box; }
        button { padding: 12px 24px; font-size: 15px; border-radius: 6px; border: none; background: #0070d1; color: #fff; cursor: pointer; font-weight: bold; transition: background 0.2s; }
        button:hover { background: #0088ff; }
        .btn-google { background: #4285F4; display: inline-flex; align-items: center; gap: 10px; }
        .btn-google:hover { background: #3367D6; }
        .btn-danger { background: #c62828; }
        .btn-danger:hover { background: #b71c1c; }
        .code-box { font-size: 26px; font-family: monospace; font-weight: bold; background: #000; border: 2px dashed #4285F4; color: #64b5f6; padding: 10px 20px; border-radius: 8px; letter-spacing: 3px; display: inline-block; margin: 10px 0; }
        .step-box { background: #1f1f1f; border-left: 4px solid #4285F4; padding: 14px 18px; border-radius: 6px; margin: 12px 0; }
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

    <!-- YouTube Device OAuth Authentication Card -->
    <div class="card" style="border-left: 4px solid #4285F4;">
        <h3 style="margin-top:0; display:flex; align-items:center; gap:8px;">
            <span>YouTube Authentication</span>
            <span style="font-size:12px; background:#333; color:#aaa; padding:2px 8px; border-radius:10px;">Bypasses Datacenter Bot-Checks</span>
        </h3>
        <p style="color: #bbb; font-size: 14px; margin-bottom: 14px;">
            Render's AWS datacenter IP requires YouTube authentication to stream music. Sign in once with Google to authorize this server.
        </p>

        <div id="authStatusBox" style="margin-bottom: 16px; font-size: 15px; font-weight: bold;">
            Checking authentication status...
        </div>

        <div id="authActions">
            <button id="signInBtn" class="btn-google" onclick="startOAuth()">
                <svg width="18" height="18" viewBox="0 0 24 24"><path fill="#fff" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#fff" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#fff" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"/><path fill="#fff" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"/></svg>
                Sign In with YouTube / Google
            </button>
        </div>

        <div id="deviceCodeFlow" style="display:none; margin-top:16px;">
            <div class="step-box">
                <div style="font-size: 15px; margin-bottom: 8px;"><strong>Follow these steps to link your server:</strong></div>
                <div style="margin: 8px 0;">1. Open: <a id="deviceLink" href="https://www.google.com/device" target="_blank" style="color:#64b5f6; font-weight:bold; font-size:16px;">https://www.google.com/device &nearr;</a></div>
                <div>2. Enter this code:</div>
                <div><span id="userCodeDisplay" class="code-box">XXXX-YYYY</span> <button onclick="copyCode()" style="padding:6px 14px; font-size:13px; margin-left:10px;">Copy Code</button></div>
                <div style="margin-top: 8px;">3. Click <strong>Allow</strong> on your Google account.</div>
                <div id="pollStatus" style="margin-top:14px; color:#ffb74d; font-size:14px; display:flex; align-items:center; gap:8px;">
                    <span style="display:inline-block; animation:spin 1s linear infinite;">&#9696;</span>
                    Waiting for approval on Google (polling every 5 seconds)...
                </div>
            </div>
        </div>

        <div id="authBackupBox" style="display:none; margin-top: 15px;">
            <details style="background:#0d0d0d; border:1px solid #333; border-radius:6px; padding:10px;">
                <summary style="cursor:pointer; color:#aaa; font-size:13px; font-weight:bold;">
                    &#128190; Save token to Render Environment Variables (Optional - keeps login permanent across redeploys)
                </summary>
                <div style="margin-top:10px; font-size:12px; color:#bbb;">
                    In your Render Dashboard &rarr; <strong>Environment</strong> &rarr; Add:
                    <div style="margin:6px 0;">Key: <code>YOUTUBE_OAUTH</code></div>
                    <div>Value:</div>
                    <textarea id="tokenJsonDisplay" readonly style="width:100%; height:70px; background:#1a1a1a; color:#a5d6a7; border:1px solid #444; border-radius:4px; font-family:monospace; font-size:11px; padding:6px; box-sizing:border-box;"></textarea>
                    <button onclick="copyTokenJson()" style="padding:6px 12px; font-size:12px; margin-top:6px;">Copy Token JSON</button>
                </div>
            </details>
        </div>
    </div>

    <!-- Live Search & Transcode Test Card -->
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

    <style>
        @keyframes spin { 100% { transform: rotate(360deg); } }
    </style>

    <script>
        let pollTimer = null;
        let currentDeviceCode = null;

        async function checkAuthStatus() {
            try {
                const resp = await fetch('/api/oauth/status');
                const data = await resp.json();
                const box = document.getElementById('authStatusBox');
                const actions = document.getElementById('authActions');
                const backupBox = document.getElementById('authBackupBox');

                if (data.authenticated) {
                    box.innerHTML = '<span style="color: #4af626;">&#10004; ' + data.message + '</span>';
                    actions.innerHTML = `
                        <button class="btn-danger" onclick="logoutOAuth()">Sign Out</button>
                        <button onclick="startOAuth()" style="margin-left: 10px; background:#333;">Re-authenticate</button>
                    `;
                    document.getElementById('deviceCodeFlow').style.display = 'none';
                    if (data.token_json) {
                        backupBox.style.display = 'block';
                        document.getElementById('tokenJsonDisplay').value = data.token_json;
                    }
                } else {
                    box.innerHTML = '<span style="color: #ff5252;">&#9888; ' + data.message + '</span>';
                    actions.innerHTML = `
                        <button class="btn-google" onclick="startOAuth()">
                            <svg width="18" height="18" viewBox="0 0 24 24"><path fill="#fff" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#fff" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#fff" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"/><path fill="#fff" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"/></svg>
                            Sign In with YouTube / Google
                        </button>
                    `;
                    backupBox.style.display = 'none';
                }
            } catch (e) {
                document.getElementById('authStatusBox').textContent = "Status check error: " + e;
            }
        }

        async function startOAuth() {
            if (pollTimer) clearInterval(pollTimer);
            const btn = document.getElementById('signInBtn');
            if (btn) btn.disabled = true;

            try {
                const resp = await fetch('/api/oauth/start', { method: 'POST' });
                const data = await resp.json();
                if (data.status !== 'ok') {
                    alert('Error starting OAuth: ' + (data.detail || JSON.stringify(data)));
                    return;
                }

                currentDeviceCode = data.device_code;
                document.getElementById('userCodeDisplay').textContent = data.user_code;
                document.getElementById('deviceLink').href = data.verification_url;
                document.getElementById('deviceCodeFlow').style.display = 'block';

                const interval = (data.interval || 5) * 1000;
                pollTimer = setInterval(pollOAuthToken, interval);
            } catch (e) {
                alert('Connection error: ' + e);
            } finally {
                if (btn) btn.disabled = false;
            }
        }

        async function pollOAuthToken() {
            if (!currentDeviceCode) return;
            try {
                const resp = await fetch('/api/oauth/poll', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ device_code: currentDeviceCode })
                });
                const data = await resp.json();

                if (data.status === 'success') {
                    clearInterval(pollTimer);
                    pollTimer = null;
                    currentDeviceCode = null;
                    alert('🎉 ' + data.message);
                    checkAuthStatus();
                } else if (data.status === 'expired') {
                    clearInterval(pollTimer);
                    pollTimer = null;
                    document.getElementById('pollStatus').textContent = 'Code expired. Click Sign In to generate a new code.';
                }
            } catch (e) {
                console.error("Poll error:", e);
            }
        }

        async function logoutOAuth() {
            if (!confirm('Are you sure you want to sign out?')) return;
            try {
                await fetch('/api/oauth/logout', { method: 'POST' });
                checkAuthStatus();
            } catch (e) {
                alert('Logout error: ' + e);
            }
        }

        function copyCode() {
            const code = document.getElementById('userCodeDisplay').textContent;
            navigator.clipboard.writeText(code).then(() => alert('Code copied to clipboard: ' + code));
        }

        function copyTokenJson() {
            const json = document.getElementById('tokenJsonDisplay').value;
            navigator.clipboard.writeText(json).then(() => alert('Token JSON copied to clipboard! Paste it into Render Environment Variables as YOUTUBE_OAUTH'));
        }

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

        window.onload = () => {
            checkAuthStatus();
            doSearch();
        };
    </script>
</body>
</html>"""

@app.get("/api/oauth/status")
def api_oauth_status():
    if has_oauth():
        token = load_oauth_token()
        return {
            "authenticated": True,
            "method": "oauth",
            "message": "YouTube Account Connected (OAuth2 Active). Datacenter bot-check bypassed!",
            "token_json": json.dumps(token)
        }
    if has_cookies():
        size = os.path.getsize(COOKIE_FILE)
        return {
            "authenticated": True,
            "method": "cookies",
            "message": f"YouTube cookies active ({size} bytes).",
            "token_json": ""
        }
    return {
        "authenticated": False,
        "method": "none",
        "message": "Not authenticated. Sign in with Google to bypass datacenter bot detection.",
        "token_json": ""
    }

@app.post("/api/oauth/start")
def api_oauth_start():
    try:
        resp = requests.post(
            "https://www.youtube.com/o/oauth2/device/code",
            data=json.dumps({
                "client_id": OAUTH_CLIENT_ID,
                "scope": OAUTH_SCOPES,
                "device_id": uuid.uuid4().hex,
                "device_model": "ytlr::"
            }),
            headers={"Content-Type": "application/json", "__youtube_oauth__": "true"},
            timeout=10
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Google OAuth rejected request: {resp.text}")
        data = resp.json()
        return {
            "status": "ok",
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_url": data.get("verification_url", "https://www.google.com/device"),
            "expires_in": data.get("expires_in", 1800),
            "interval": data.get("interval", 5)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/oauth/poll")
async def api_oauth_poll(request: Request):
    try:
        body = await request.json()
        device_code = body.get("device_code")
        if not device_code:
            raise HTTPException(status_code=400, detail="Missing device_code")

        resp = requests.post(
            "https://www.youtube.com/o/oauth2/token",
            data=json.dumps({
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
            }),
            headers={"Content-Type": "application/json", "__youtube_oauth__": "true"},
            timeout=10
        )
        data = resp.json()
        if "access_token" in data:
            token_data = {
                "access_token": data["access_token"],
                "expires": time.time() + data.get("expires_in", 3600),
                "refresh_token": data.get("refresh_token", ""),
                "token_type": data.get("token_type", "Bearer")
            }
            save_oauth_token(token_data)
            return {
                "status": "success",
                "message": "YouTube account successfully linked! Bot-check bypassed.",
                "token_json": json.dumps(token_data)
            }

        error = data.get("error", "")
        if error == "authorization_pending":
            return {"status": "pending"}
        elif error == "expired_token":
            return {"status": "expired", "message": "Code expired. Please generate a new code."}
        else:
            return {"status": "error", "message": data.get("error_description", error)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/oauth/logout")
def api_oauth_logout():
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
        cache_file = os.path.expanduser("~/.cache/yt-dlp/youtube-oauth2/token_data.json")
        if os.path.exists(cache_file):
            os.remove(cache_file)
        return {"status": "ok", "message": "Logged out successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    headers = "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36\r\n"
    if "googlevideo.com" in audio_url or "youtube.com" in audio_url:
        headers += "Referer: https://music.youtube.com/\r\n"

    ffmpeg_cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-headers", headers,
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

def build_pcm_stream(audio_url: str):
    """
    Transcodes resolved YouTube audio URL to raw signed 16-bit LE 44.1kHz stereo PCM for PSP bridge mode.
    """
    headers = "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
    ffmpeg_cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-headers", headers,
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

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=65536
    )

    try:
        first_chunk = proc.stdout.read(8192)
    except Exception:
        first_chunk = b""

    if not first_chunk:
        if proc.poll() is None:
            proc.terminate()
        return None

    def iterpcm():
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
        iterpcm(),
        media_type="application/octet-stream",
        headers={"Access-Control-Allow-Origin": "*"}
    )

def get_proxied_audio_url(video_id: str) -> str:
    """
    Finds a working proxied audio stream URL from Piped or Invidious API instances.
    Never IP-locked, never blocked by datacenter bot checks.
    """
    cached = get_cached_url(video_id)
    if cached:
        return cached

    # 1. Piped API proxied streams (hosted on external mirrors, never IP-locked)
    piped_mirrors = [
        "https://pipedapi.kavin.rocks",
        "https://api.piped.privacy.com.de",
        "https://piped-api.lunar.icu",
        "https://pipedapi.leptons.xyz",
        "https://pipedapi.r4fo.com",
        "https://pa.il.ax",
        "https://api-piped.mha.fi",
        "https://pipedapi.drgns.space",
        "https://pipedapi.tokhmi.xyz"
    ]
    for m in piped_mirrors:
        try:
            r = requests.get(f"{m}/streams/{video_id}", timeout=2.5)
            if r.status_code == 200:
                data = r.json()
                for s in data.get("audioStreams", []):
                    proxy_url = s.get("proxyUrl") or s.get("url")
                    if proxy_url:
                        set_cached_url(video_id, proxy_url)
                        print(f"Found proxied stream for {video_id} via {m}")
                        return proxy_url
        except Exception:
            continue

    # 2. Invidious API audio streams
    invidious_mirrors = [
        "https://inv.nadeko.net",
        "https://invidious.nerdvpn.de",
        "https://invidious.private.coffee",
        "https://invidious.drgns.space",
        "https://invidious.einfachzocken.eu",
        "https://yewtu.be",
        "https://iv.ggtyler.dev"
    ]
    for m in invidious_mirrors:
        try:
            r = requests.get(f"{m}/api/v1/videos/{video_id}", timeout=2.5)
            if r.status_code == 200:
                data = r.json()
                formats = data.get("adaptiveFormats", []) + data.get("formatStreams", [])
                for f in formats:
                    if "audio" in f.get("type", "") and f.get("url"):
                        stream_url = f["url"]
                        if stream_url.startswith("/"):
                            stream_url = f"{m}{stream_url}"
                        set_cached_url(video_id, stream_url)
                        print(f"Found Invidious stream for {video_id} via {m}")
                        return stream_url
        except Exception:
            continue

    return None

def stream_mp3_invidious_proxy(video_id: str):
    """
    Proxies stream directly from public Piped and Invidious API instances.
    Extracts direct or proxied audio URLs and pipes them to FFmpeg.
    Bypasses datacenter bot-checks and IP locks entirely.
    """
    url = get_proxied_audio_url(video_id)
    if url:
        return build_mp3_stream(url)
    return None

def stream_mp3_ytdlp_pipe(video_id: str):
    """
    Direct pipe from yt-dlp to FFmpeg.
    Uses unblocked mobile and creator clients (ios, visionos, android_creator, tv_embedded)
    which bypass datacenter bot-checks. FFmpeg reads pipe:0, eliminating external CDN IP-locks.
    """
    attempts = [
        ["--extractor-args", "youtube:player_client=ios"],
        ["--extractor-args", "youtube:player_client=visionos"],
        ["--extractor-args", "youtube:player_client=android_creator"],
        ["--extractor-args", "youtube:player_client=ios_creator"],
        ["--extractor-args", "youtube:player_client=tv_embedded"],
        ["--extractor-args", "youtube:player_client=android"],
        []
    ]

    if has_cookies():
        attempts.append(["--cookies", COOKIE_FILE, "--extractor-args", "youtube:player_client=web"])

    for extra_args in attempts:
        ytdlp_cmd = [
            sys.executable, "-m", "yt_dlp",
            "--quiet",
            "--no-warnings",
            "--no-playlist"
        ] + extra_args + [
            "-f", "ba/b",
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
    attempts = [
        ["--extractor-args", "youtube:player_client=ios"],
        ["--extractor-args", "youtube:player_client=visionos"],
        ["--extractor-args", "youtube:player_client=android_creator"],
        ["--extractor-args", "youtube:player_client=ios_creator"],
        ["--extractor-args", "youtube:player_client=tv_embedded"],
        ["--extractor-args", "youtube:player_client=android"],
        []
    ]

    if has_cookies():
        attempts.append(["--cookies", COOKIE_FILE, "--extractor-args", "youtube:player_client=web"])

    for extra_args in attempts:
        ytdlp_cmd = [
            sys.executable, "-m", "yt_dlp",
            "--quiet",
            "--no-warnings",
            "--no-playlist"
        ] + extra_args + [
            "-f", "ba/b",
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

    # Strategy 1: Proxied Audio Stream via Piped / Invidious (Fastest, zero datacenter bot-checks, zero IP locks)
    try:
        resp = stream_mp3_invidious_proxy(vid)
        if resp:
            print(f"Streaming {vid} via Audio Proxy (SUCCESS)")
            return resp
    except Exception as e:
        print(f"Strategy 1 Proxy error for {vid}: {e}")

    # Strategy 2: Direct yt-dlp -> FFmpeg process pipe (Zero IP-lock, cloud container native)
    try:
        resp = stream_mp3_ytdlp_pipe(vid)
        if resp:
            print(f"Streaming {vid} via yt-dlp pipe (SUCCESS)")
            return resp
    except Exception as e:
        print(f"Strategy 2 yt-dlp error for {vid}: {e}")

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
    print(f"--- PCM stream requested for {vid} ---")

    # Strategy 1: Proxied Audio -> FFmpeg PCM (Zero bot check)
    try:
        url = get_proxied_audio_url(vid)
        if url:
            resp = build_pcm_stream(url)
            if resp:
                print(f"Streaming PCM for {vid} via proxy (SUCCESS)")
                return resp
    except Exception as e:
        print(f"Strategy 1 PCM proxy error for {vid}: {e}")

    # Strategy 2: Direct yt-dlp -> FFmpeg PCM pipe
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
