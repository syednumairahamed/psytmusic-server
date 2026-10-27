# Deploying PSYTMusic Server to Render.com (100% Free & Fast)

Render provides a **free persistent web service** with full native **FFmpeg** support, allowing YouTube audio to be transcoded on-the-fly directly to standard 128kbps CBR MP3 for the Sony PSP's hardware Media Engine (`sceMp3`) without stream timeouts or cuts.

---

### Step 1: Create a GitHub Repo & Upload Files (No Git installation needed!)

1. Go to [GitHub.com](https://github.com) and click **New Repository** (e.g. name it `psytmusic-server`, keep it Public or Private).
2. On your empty repository page, click the link: **"uploading an existing file"**.
3. Open your computer folder:
   `C:\Users\syedn\Desktop\psptube\render_server\`
4. Drag and drop these **4 files** into GitHub:
   - `Dockerfile`
   - `requirements.txt`
   - `server.py`
   - `render.yaml`
5. Click **Commit changes**.

*(Alternatively, if you have Git installed, you can `git push` the `render_server` folder).*

---

### Step 2: Deploy on Render.com

1. Go to [dashboard.render.com](https://dashboard.render.com) and log in (sign in with your GitHub account).
2. Click the blue **New +** button at the top right, then select **Web Service**.
3. Select **Build and deploy from a Git repository**, find your `psytmusic-server` repository, and click **Connect**.
4. Configure the service:
   - **Name:** `psytmusic-server` (or any custom name)
   - **Runtime:** `Docker` *(Render will automatically detect your `Dockerfile`)*
   - **Instance Type:** `Free`
5. Click **Deploy Web Service** (or **Create Web Service**).

Render will build the Docker container with Linux FFmpeg and launch your streaming server. In about 2 minutes, your URL will be live at:
`https://your-service-name.onrender.com`

---

### Step 3: Test in Your Web Browser

Open your new Render URL (`https://your-service-name.onrender.com`) in your web browser:
1. You will see the **PSYTMusic Server** dashboard with a live search bar.
2. Type any song (e.g. `Daft Punk`) and click **Search**.
3. Click **▶ Stream MP3** to verify that FFmpeg transcodes and streams audio instantly.

---

### Step 4: Configure Your Sony PSP

You can configure your Render server either by editing `server.txt` on your Memory Stick or directly inside the PSP's Settings menu:

#### Option A: Editing `server.txt`
On your Memory Stick (`ms0:/PSP/GAME/PSYTMUSIC/server.txt`), enter your Render domain:
```
your-service-name.onrender.com
```

#### Option B: From the PSP Interface
1. Launch **PSYTMusic** on your PSP.
2. Go to **Settings** -> **Server Address**.
3. Press **X** to open the on-screen keyboard, enter your Render domain (e.g. `your-service-name.onrender.com`), and press Enter.

Your PSP will now search YouTube Music over HTTPS and decode the live 128kbps CBR MP3 stream natively with its hardware Media Engine!
