# Deploying Google Drive Music Server to Render.com (100% Free)

Render provides a **free, persistent cloud container** with full native **Linux FFmpeg** support. It pulls your FLAC and MP4 songs directly from Google Drive and transcodes them on-the-fly into 192kbps CBR MP3 for your Sony PSP's hardware Media Engine (`sceMp3`), letting your PSP stream your music library from anywhere over Wi-Fi without needing a PC on!

---

### Step 1: Create a GitHub Repository

1. Go to [GitHub.com](https://github.com) and click **New Repository**.
2. Name it `psytmusic-drive-server` (set it to **Private** or Public).
3. On your empty repository page, click the link: **"uploading an existing file"**.
4. Open the folder on your computer:
   ```
   C:\Users\syedn\Desktop\psptube\render_server\
   ```
5. Drag and drop these **4 files** into GitHub:
   - `Dockerfile`
   - `requirements.txt`
   - `server.py`
   - `render.yaml`
6. Click **Commit changes**.

*(Note: Never upload your `service_account.json` to a public repository! In Step 3 below, you can safely paste your key into Render's Environment Variables).*

---

### Step 2: Deploy on Render.com

1. Go to [dashboard.render.com](https://dashboard.render.com) and sign in with your GitHub account.
2. Click the blue **New +** button at the top right &rarr; select **Web Service**.
3. Select **Build and deploy from a Git repository**, choose your `psytmusic-drive-server` repository, and click **Connect**.
4. Configure the service:
   - **Name:** `psytmusic-server` (or any custom name)
   - **Runtime:** `Docker` *(Render auto-detects `Dockerfile`)*
   - **Instance Type:** `Free`
5. Click **Create Web Service** (or **Deploy Web Service**).

---

### Step 3: Add Your Google Drive Key (Safe & Encrypted)

You don't need to commit your key to GitHub! You can paste it directly into Render:

1. In your Render Dashboard, click on your service (`psytmusic-server`).
2. In the left menu, click **Environment**.
3. Click **Add Environment Variable**:
   - **Key:** `GOOGLE_SERVICE_ACCOUNT_JSON`
   - **Value:** Open your `service_account.json` file in Notepad, copy all text, and paste it here.
4. Click **Save Changes**.

Render will automatically restart the server with your Google Drive credentials loaded!

---

### Step 4: Share Your Google Drive Music Folder

1. Open your Render Web Service page and click on your live URL (e.g. `https://psytmusic-server.onrender.com`).
2. You will see the **PSP Google Drive Music Streamer** dashboard.
3. It will display your **Service Account Email** (e.g. `psp-streamer@...iam.gserviceaccount.com`).
4. In [Google Drive](https://drive.google.com), right-click your Music folder (with your FLAC & MP4 files) &rarr; **Share** &rarr; paste the email as **Viewer** &rarr; click **Send**.
5. Back on the dashboard, click **🔄 Rescan Google Drive**.
6. All your songs will immediately appear! Click **▶ Test Stream** to verify audio playback.

---

### Step 5: Configure Your Sony PSP

#### Option A: Editing `server.txt`
On your PSP Memory Stick (`ms0:/PSP/GAME/PSYTMUSIC/server.txt`), enter your Render domain:
```
your-service-name.onrender.com
```

#### Option B: From the PSP Interface
1. Launch **PSYTMusic** on your PSP.
2. Go to **Settings** &rarr; **Server**.
3. Press **✕** to open the on-screen keyboard, enter your Render domain (e.g. `your-service-name.onrender.com`), and press Enter.

Your PSP will now stream your personal FLAC & MP4 music library over HTTPS and decode the live 192kbps MP3 stream using hardware acceleration!
