import io
import os
import sys
import json
import time
import subprocess
import threading
import urllib.parse
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import requests

app = FastAPI(title="PSP Google Drive Music Streamer", version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# FFmpeg Detection
# ---------------------------------------------------------
def find_ffmpeg() -> str:
    try:
        res = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0:
            return "ffmpeg"
    except Exception:
        pass
    return "ffmpeg"

FFMPEG_BIN = find_ffmpeg()
print(f"--- Detected FFmpeg Binary: {FFMPEG_BIN} ---")

# ---------------------------------------------------------
# Google Drive Authentication
# ---------------------------------------------------------
SERVICE_ACCOUNT_FILES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "service_account.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json"),
    "service_account.json",
    "credentials.json"
]

drive_service = None
drive_creds = None
service_account_email = ""

def load_google_credentials():
    global drive_service, drive_creds, service_account_email
    creds_dict = None

    env_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if env_json:
        try:
            creds_dict = json.loads(env_json)
            print("--- Loaded Google Service Account from GOOGLE_SERVICE_ACCOUNT_JSON ---")
        except Exception as e:
            print(f"Warning: Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON: {e}")

    if not creds_dict:
        for fpath in SERVICE_ACCOUNT_FILES:
            if os.path.exists(fpath) and os.path.getsize(fpath) > 20:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        creds_dict = json.load(f)
                    print(f"--- Loaded Google Service Account from: {fpath} ---")
                    break
                except Exception as e:
                    print(f"Warning: Could not read {fpath}: {e}")

    if not creds_dict:
        print("--- NOTICE: No service_account.json found yet. Waiting for user setup ---")
        return None

    try:
        service_account_email = creds_dict.get("client_email", "")
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/drive.readonly"]
        drive_creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=scopes)
        drive_service = build("drive", "v3", credentials=drive_creds, cache_discovery=False)
        print(f"--- Google Drive API Authenticated! Service Email: {service_account_email} ---")
        return drive_service
    except ImportError:
        print("ERROR: Google API libraries not installed. Run: pip install google-api-python-client google-auth")
        return None
    except Exception as e:
        print(f"ERROR initializing Google Drive API: {e}")
        return None

load_google_credentials()

# ---------------------------------------------------------
# Music Library In-Memory Cache & Recursive Folder Indexer
# ---------------------------------------------------------
music_library: List[Dict] = []
folder_library: List[Dict] = []
last_scan_time = 0
is_scanning = False

AUDIO_EXTENSIONS = (
    '.flac', '.mp4', '.m4a', '.mp3', '.wav', '.ogg', '.aac',
    '.opus', '.alac', '.aiff', '.wma', '.m4v', '.ape', '.dsf',
    '.dff', '.flv', '.mka', '.webm'
)

def parse_filename_meta(filename: str, folder_name: str = "") -> (str, str):
    base, _ = os.path.splitext(filename)
    base = base.replace("_", " ").strip()
    fallback_artist = folder_name if folder_name else "Google Drive"

    if " - " in base:
        parts = base.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip()
        if len(title) > 3 and title[:2].isdigit() and title[2] in " .-_":
            title = title[3:].strip()
        return title[:64], artist[:48]

    if len(base) > 3 and base[:2].isdigit() and base[2] in " .-_":
        title = base[3:].strip()
        return title[:64], fallback_artist[:48]

    return base[:64], fallback_artist[:48]

def format_duration(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return "3:30"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"

def scan_google_drive():
    global music_library, folder_library, last_scan_time, is_scanning
    if is_scanning:
        return
    if not drive_service:
        load_google_credentials()
        if not drive_service:
            return

    is_scanning = True
    print("--- Scanning Google Drive for Music across all Folders & Subfolders ---")
    new_tracks = []
    folder_map = {}

    try:
        folder_query = "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        page_token = None
        while True:
            resp = drive_service.files().list(
                q=folder_query,
                spaces='drive',
                fields='nextPageToken, files(id, name, parents)',
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token
            ).execute()
            for f in resp.get('files', []):
                fid = f['id']
                parents = f.get('parents', [])
                parent_id = parents[0] if parents else None
                folder_map[fid] = {
                    "id": fid,
                    "name": f.get('name', 'Folder'),
                    "parent": parent_id
                }
            page_token = resp.get('nextPageToken')
            if not page_token:
                break

        for fid, f_info in folder_map.items():
            name = f_info['name']
            curr = f_info
            path_parts = [name]
            depth = 0
            while curr.get('parent') and curr['parent'] in folder_map and depth < 6:
                curr = folder_map[curr['parent']]
                path_parts.insert(0, curr['name'])
                depth += 1
            f_info['path'] = " / ".join(path_parts)

        seen_ids = set()

        def process_item(item, default_parent=""):
            file_id = item['id']
            if file_id in seen_ids:
                return
            seen_ids.add(file_id)

            name = item.get('name', 'Unknown')
            lower = name.lower()
            mime = item.get('mimeType', '').lower()

            # Catch all audio formats, video/mp4, or explicit audio extensions
            is_audio = (
                any(lower.endswith(ext) for ext in AUDIO_EXTENSIONS) or
                mime.startswith('audio/') or
                mime == 'video/mp4'
            )
            if not is_audio:
                return

            parents = item.get('parents', [])
            parent_folder_id = parents[0] if parents else (default_parent or "")
            folder_info = folder_map.get(parent_folder_id, {})
            folder_name = folder_info.get('name', '')
            folder_path = folder_info.get('path', folder_name)

            title, artist = parse_filename_meta(name, folder_name)
            ext = os.path.splitext(name)[1].lower().replace('.', '').upper()
            if not ext:
                if mime.startswith('audio/'):
                    ext = mime.split('/')[-1].upper()
                elif mime == 'video/mp4':
                    ext = "MP4"
                else:
                    ext = "AUDIO"

            dur_ms = 0
            vmeta = item.get('videoMediaMetadata')
            if vmeta and 'durationMillis' in vmeta:
                dur_ms = int(vmeta['durationMillis'])
            duration_str = format_duration(dur_ms / 1000.0) if dur_ms > 0 else "3:45"

            new_tracks.append({
                "id": file_id,
                "name": name,
                "title": title.replace("|", " "),
                "artist": f"[{ext}] {artist}".replace("|", " "),
                "duration": duration_str,
                "format": ext,
                "size": int(item.get('size', 0)),
                "mime": item.get('mimeType', ''),
                "folder_id": parent_folder_id,
                "folder_name": folder_name,
                "folder_path": folder_path
            })

        # Step 2: Global non-folder scan across all accessible items
        # Avoids Google Drive's broken "name contains '.flac'" query tokenization bug
        file_query = "mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        page_token = None
        while True:
            try:
                resp = drive_service.files().list(
                    q=file_query,
                    spaces='drive',
                    fields='nextPageToken, files(id, name, mimeType, size, videoMediaMetadata, parents)',
                    pageSize=1000,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    pageToken=page_token
                ).execute()

                for item in resp.get('files', []):
                    process_item(item)

                page_token = resp.get('nextPageToken')
                if not page_token:
                    break
            except Exception as ge:
                print(f"Notice during global file list: {ge}")
                break

        # Step 3: Deep inspection of all discovered folders and subfolders
        # Guarantees files inside shared subfolders that weren't surfaced in global list are crawled
        for fid in list(folder_map.keys()):
            sub_page = None
            while True:
                try:
                    sresp = drive_service.files().list(
                        q=f"'{fid}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false",
                        spaces='drive',
                        fields='nextPageToken, files(id, name, mimeType, size, videoMediaMetadata, parents)',
                        pageSize=1000,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        pageToken=sub_page
                    ).execute()

                    for item in sresp.get('files', []):
                        process_item(item, default_parent=fid)

                    sub_page = sresp.get('nextPageToken')
                    if not sub_page:
                        break
                except Exception as se:
                    break

        folder_counts = {}
        for t in new_tracks:
            fid = t.get('folder_id')
            if fid:
                folder_counts[fid] = folder_counts.get(fid, 0) + 1

        active_folders = []
        for fid, count in folder_counts.items():
            f_info = folder_map.get(fid, {"id": fid, "name": "Folder", "path": "Folder"})
            active_folders.append({
                "id": fid,
                "name": f_info.get('name', 'Folder'),
                "path": f_info.get('path', 'Folder'),
                "count": count
            })
        active_folders.sort(key=lambda x: x['name'].lower())

        music_library = new_tracks
        folder_library = active_folders
        last_scan_time = time.time()
        print(f"--- Scan Complete! Found {len(music_library)} audio tracks across {len(folder_library)} folders! ---")
    except Exception as e:
        print(f"Error scanning Google Drive: {e}")
    finally:
        is_scanning = False

threading.Thread(target=scan_google_drive, daemon=True).start()

# ---------------------------------------------------------
# Google Drive Stream -> FFmpeg MP3 Transcoder
# ---------------------------------------------------------
def get_drive_auth_token() -> str:
    global drive_creds
    if not drive_creds:
        load_google_credentials()
    if not drive_creds:
        raise HTTPException(status_code=500, detail="Google Drive not authenticated")

    from google.auth.transport.requests import Request
    if not drive_creds.valid:
        drive_creds.refresh(Request())
    return drive_creds.token

def build_hardware_mp3_stream(file_id: str):
    token = get_drive_auth_token()
    download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    headers = {"Authorization": f"Bearer {token}"}

    drive_resp = requests.get(download_url, headers=headers, stream=True, timeout=15)
    if drive_resp.status_code != 200:
        raise HTTPException(status_code=drive_resp.status_code, detail=f"Google Drive returned error {drive_resp.status_code}")

    ffmpeg_cmd = [
        FFMPEG_BIN,
        "-loglevel", "error",
        "-i", "pipe:0",
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        "-f", "mp3",
        "pipe:1"
    ]

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=65536
    )

    def feeder():
        try:
            for chunk in drive_resp.iter_content(chunk_size=65536):
                if not chunk:
                    break
                if proc.poll() is not None:
                    break
                try:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    feed_thread = threading.Thread(target=feeder, daemon=True)
    feed_thread.start()

    try:
        first_chunk = proc.stdout.read(8192)
    except Exception:
        first_chunk = b""

    if not first_chunk:
        if proc.poll() is None:
            proc.terminate()
        raise HTTPException(status_code=502, detail="FFmpeg failed to transcode audio from Google Drive")

    def itermp3():
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
        itermp3(),
        media_type="audio/mpeg",
        headers={
            "Content-Type": "audio/mpeg",
            "Accept-Ranges": "none",
            "Access-Control-Allow-Origin": "*"
        }
    )

# ---------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------
@app.get("/health")
@app.get("/status")
def health():
    return {
        "status": "online",
        "authenticated": drive_service is not None,
        "service_email": service_account_email,
        "tracks_indexed": len(music_library),
        "folders_found": len(folder_library),
        "last_scan": last_scan_time
    }

@app.get("/rescan")
def rescan():
    threading.Thread(target=scan_google_drive, daemon=True).start()
    return {"status": "scanning", "message": "Rescanning Google Drive for music files and subfolders..."}

@app.get("/folders")
def api_folders():
    return folder_library

@app.get("/folders_txt", response_class=PlainTextResponse)
def api_folders_txt():
    """
    Returns line-delimited folders for Sony PSP Playlists tab:
    id|Folder Name|track_count
    """
    if not folder_library:
        return "all|All Songs (Library)|0"
    lines = ["all|All Songs (All Folders)|" + str(len(music_library))]
    for f in folder_library[:25]:
        lines.append(f"{f['id']}|{f['name']}|{f['count']} songs")
    return "\n".join(lines)

@app.get("/search_txt", response_class=PlainTextResponse)
def api_search_txt(q: str = "", folder: str = "", limit: int = 250):
    if not music_library:
        if not drive_service:
            return "none|No Service Account Key|Add service_account.json|0:00"
        return "none|Library Empty or Scanning|Upload FLAC/MP4 & click Rescan|0:00"

    items = music_library
    folder_filter = folder.strip()
    if folder_filter and folder_filter != "all":
        items = [s for s in items if s.get('folder_id') == folder_filter or folder_filter.lower() in s.get('folder_name', '').lower()]

    results = []
    query = q.strip().lower()
    if query:
        for item in items:
            if (query in item['title'].lower() or
                query in item['artist'].lower() or
                query in item['name'].lower() or
                query in item.get('folder_name', '').lower()):
                results.append(item)
    else:
        results = items

    if not results:
        return f"none|No songs matching: {q[:20]}|Check spelling or Rescan|0:00"

    if limit > 0:
        results = results[:limit]
    lines = [f"{s['id']}|{s['title']}|{s['artist']}|{s['duration']}" for s in results]
    return "\n".join(lines)

@app.get("/search")
def api_search(q: str = "", folder: str = "", limit: int = 0):
    items = music_library
    if folder and folder != "all":
        items = [s for s in items if s.get('folder_id') == folder or folder.lower() in s.get('folder_name', '').lower()]
    query = q.strip().lower()
    if query:
        items = [s for s in items if query in s['title'].lower() or query in s['artist'].lower() or query in s['name'].lower() or query in s.get('folder_name', '').lower()]
    if limit > 0:
        return items[:limit]
    return items

@app.get("/stream/{file_id}.mp3")
@app.get("/stream/{file_id}")
def stream_audio(file_id: str):
    clean_id = file_id.replace(".mp3", "").replace(".pcm", "").replace(".flac", "").replace(".mp4", "")
    print(f"--- Audio stream requested for Google Drive file: {clean_id} ---")
    return build_hardware_mp3_stream(clean_id)

@app.get("/", response_class=HTMLResponse)
def index():
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>PSYTMusic Google Drive Streamer</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 960px; margin: 30px auto; padding: 0 20px; background: #0c0d10; color: #f0f0f0; }}
        h1 {{ color: #0070d1; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; }}
        .badge {{ background: #1b5e20; color: #a5d6a7; padding: 4px 10px; border-radius: 12px; font-size: 13px; font-weight: bold; }}
        .card {{ background: #16181f; padding: 22px; border-radius: 10px; margin: 20px 0; border: 1px solid #272a38; }}
        input[type=text], select {{ padding: 12px 16px; font-size: 15px; border-radius: 6px; border: 1px solid #444; background: #222; color: #fff; box-sizing: border-box; }}
        input[type=text] {{ flex-grow: 1; }}
        button {{ padding: 12px 24px; font-size: 15px; border-radius: 6px; border: none; background: #0070d1; color: #fff; cursor: pointer; font-weight: bold; transition: background 0.2s; }}
        button:hover {{ background: #0088ff; }}
        .btn-green {{ background: #10b981; }}
        .btn-green:hover {{ background: #059669; }}
        .code-box {{ font-family: monospace; background: #060709; border: 1px solid #333; color: #64b5f6; padding: 10px 14px; border-radius: 6px; display: inline-block; word-break: break-all; }}
        .item {{ display: flex; align-items: center; justify-content: space-between; margin: 10px 0; padding: 12px 16px; background: #1f222e; border-radius: 8px; border: 1px solid #2e3347; }}
        .title {{ font-weight: bold; margin-bottom: 4px; font-size: 15px; color: #fff; }}
        .artist {{ color: #94a3b8; font-size: 13px; }}
        .folder-tag {{ background: #1e293b; color: #cbd5e1; padding: 2px 8px; border-radius: 4px; font-size: 11px; margin-left: 8px; border: 1px solid #334155; }}
        .format-badge {{ background: #3b82f6; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-right: 8px; }}
        .raw-box {{ background: #060709; border: 1px solid #333; padding: 14px; border-radius: 6px; font-family: monospace; font-size: 13px; white-space: pre-wrap; color: #4af626; max-height: 180px; overflow-y: auto; }}
        audio {{ width: 100%; margin-top: 15px; }}
    </style>
</head>
<body>
    <h1>PSP Google Drive Music Streamer <span class="badge">ONLINE</span></h1>
    <p>High-fidelity FLAC & MP4 on-the-fly hardware MP3 transcoding bridge for Sony PSP.</p>

    <!-- Google Drive Connection Status Card -->
    <div class="card" style="border-left: 4px solid #10b981;">
        <h3 style="margin-top:0;">Google Drive Service Account Status</h3>
        <div id="authStatusBox">
            <p><strong>Service Account Email:</strong> <span class="code-box" id="emailBox">{service_account_email or 'No key found yet'}</span></p>
            <p style="color:#aaa; font-size:14px;">
                <strong>Next Step:</strong> Open <a href="https://drive.google.com" target="_blank" style="color:#64b5f6;">Google Drive</a>, right-click your main Music folder &rarr; <strong>Share</strong> &rarr; paste the email above as <strong>Viewer</strong>. All subfolders and nested files are automatically included!
            </p>
            <div style="display:flex; gap:12px; margin-top:14px;">
                <button class="btn-green" onclick="doRescan()">&#128260; Rescan Google Drive</button>
                <span id="scanStatus" style="display:flex; align-items:center; color:#10b981; font-weight:bold;"></span>
            </div>
        </div>
    </div>

    <!-- Music Library Browser & Audio Player -->
    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
            <h3 style="margin:0;">Music Library (<span id="songCount">0</span> songs across <span id="folderCount">0</span> folders)</h3>
            <span id="displayedCount" style="color:#10b981; font-weight:bold; font-size:14px;"></span>
        </div>

        <div style="display: flex; gap: 10px; margin: 20px 0; flex-wrap: wrap;">
            <select id="folderSelect" onchange="filterSongs()" style="min-width: 220px;">
                <option value="all">📁 All Folders</option>
            </select>
            <input type="text" id="query" placeholder="Filter songs by title, artist, or folder..." oninput="filterSongs()">
            <button onclick="filterSongs()">Filter</button>
        </div>

        <h4>Raw PSP Response (<code>/search_txt</code> format &bull; <span id="pspSongCount">0</span> tracks sent to PSP):</h4>
        <div id="rawOutput" class="raw-box">Loading songs...</div>

        <h4 style="margin-top:25px;">Web Audio Player (Verify Transcoder):</h4>
        <audio id="audioPlayer" controls style="display:none;"></audio>

        <div id="results" style="margin-top:15px;"></div>
    </div>

    <script>
        let allSongs = [];
        let allFolders = [];

        async function loadStatus() {{
            const res = await fetch('/status');
            const data = await res.json();
            document.getElementById('emailBox').textContent = data.service_email || 'service_account.json missing';
            document.getElementById('songCount').textContent = data.tracks_indexed;
            document.getElementById('folderCount').textContent = data.folders_found;
        }}

        async function doRescan() {{
            document.getElementById('scanStatus').textContent = "Scanning folders & subfolders...";
            await fetch('/rescan');
            setTimeout(loadSongs, 3000);
        }}

        async function loadFolders() {{
            const res = await fetch('/folders');
            allFolders = await res.json();
            const sel = document.getElementById('folderSelect');
            sel.innerHTML = '<option value="all">📁 All Folders (' + allSongs.length + ' songs)</option>' +
                allFolders.map(f => `<option value="${{f.id}}">📁 ${{f.name}} (${{f.count}} songs)</option>`).join('');
        }}

        async function loadSongs() {{
            await loadStatus();
            const res = await fetch('/search');
            allSongs = await res.json();
            document.getElementById('songCount').textContent = allSongs.length;
            await loadFolders();
            filterSongs();

            const txtRes = await fetch('/search_txt');
            const txt = await txtRes.text();
            document.getElementById('rawOutput').textContent = txt;
            const pspLines = txt.trim() ? txt.trim().split('\\n').filter(l => !l.startsWith('none|')) : [];
            document.getElementById('pspSongCount').textContent = pspLines.length;
            document.getElementById('scanStatus').textContent = "Library up to date!";
        }}

        function filterSongs() {{
            const q = document.getElementById('query').value.toLowerCase();
            const selectedFolder = document.getElementById('folderSelect').value;

            const filtered = allSongs.filter(s => {{
                const matchesFolder = (selectedFolder === 'all') || (s.folder_id === selectedFolder);
                const matchesQuery = !q ||
                    s.title.toLowerCase().includes(q) ||
                    s.artist.toLowerCase().includes(q) ||
                    s.name.toLowerCase().includes(q) ||
                    (s.folder_name && s.folder_name.toLowerCase().includes(q));
                return matchesFolder && matchesQuery;
            }});

            document.getElementById('displayedCount').textContent = `Showing ${{filtered.length}} of ${{allSongs.length}} songs`;
            renderSongs(filtered);
        }}

            renderSongs(filtered);
        }}

        function renderSongs(list) {{
            const div = document.getElementById('results');
            if (list.length === 0) {{
                div.innerHTML = "<p style='color:#888;'>No tracks found in this selection. Make sure your Music folder is shared with the Service Account email and click Rescan.</p>";
                return;
            }}
            div.innerHTML = list.map(s => `
                <div class="item">
                    <div>
                        <div class="title">
                            <span class="format-badge">${{s.format}}</span>${{s.title}}
                            ${{s.folder_name ? `<span class="folder-tag">📁 ${{s.folder_name}}</span>` : ''}}
                        </div>
                        <div class="artist">${{s.artist}} &bull; ${{s.duration}} &bull; ${{s.name}}</div>
                    </div>
                    <button onclick="playSong('${{s.id}}', '${{s.title.replace(/'/g, "\\\\'")}}')">&#9654; Test Stream</button>
                </div>
            `).join('');
        }}

        function playSong(id, title) {{
            const player = document.getElementById('audioPlayer');
            player.style.display = 'block';
            player.src = '/stream/' + id + '.mp3';
            player.play();
        }}

        window.onload = loadSongs;
    </script>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    print(f"--- Starting Render Google Drive Streaming Server on Port {port} ---")
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
