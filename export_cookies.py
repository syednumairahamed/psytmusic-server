"""
Helper script to export YouTube cookies from your local PC browser (Brave, Edge, Chrome, Firefox)
and save them directly to render_server/cookies.txt for Render deployment.
"""
import os
import sys
import yt_dlp

def export():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_file = os.path.join(script_dir, "cookies.txt")
    browsers = ["brave", "edge", "chrome", "firefox"]

    for b in browsers:
        print(f"Checking for YouTube login session in: {b}...")
        try:
            ydl_opts = {
                'cookiesfrombrowser': (b, None, None, None),
                'cookiefile': out_file,
                'quiet': True,
                'skip_download': True
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info("https://www.youtube.com/watch?v=0wTOi2TCSSk", download=False)

            if os.path.exists(out_file) and os.path.getsize(out_file) > 100:
                print(f"*** SUCCESS: Exported {os.path.getsize(out_file)} bytes of cookies from {b} to cookies.txt! ***")
                return True
        except Exception as e:
            print(f"  Could not extract from {b}: {e}")

    print("\nTip: If your browser is open, close it or use the 'Get cookies.txt LOCALLY' extension.")
    return False

if __name__ == "__main__":
    export()
