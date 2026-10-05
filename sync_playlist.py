import datetime
import os
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
import pyodbc
import yt_dlp

# ==============================================================================
# PATHS AND CONFIGURATION
# ==============================================================================
# Dynamically resolves the script directory (works for both audio & video syncs)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
START_TEMPLATE_PATH = os.path.join(BASE_DIR, "email_start.html")
REPORT_TEMPLATE_PATH = os.path.join(BASE_DIR, "email_report.html")

if not os.path.exists(ENV_PATH):
    print(f"❌ Error: .env file not found at {ENV_PATH}")
    sys.exit(1)

load_dotenv(dotenv_path=ENV_PATH)

PLAYLIST_URL = os.getenv("PLAYLIST_URL")
STAGING_DIR = os.getenv("STAGING_DIR", "/tmp/yt_staging")
DOWNLOAD_DIR = os.getenv("DESTINATION_DIR", "/DATA/Media/YtVids")
ARCHIVE_FILE = os.getenv("ARCHIVE_FILE", os.path.join(BASE_DIR, "archive.txt"))

# MSSQL Database Settings
DB_SERVER = os.getenv("MSSQL_SERVER")
DB_NAME = os.getenv("MSSQL_DATABASE")
DB_USER = os.getenv("MSSQL_USER")
DB_PASS = os.getenv("MSSQL_PASSWORD")
DB_DRIVER = os.getenv("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")

# SMTP Settings
SMTP_SERVER = os.getenv("BREVO_SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT = int(os.getenv("BREVO_SMTP_PORT", 587))
SMTP_USER = os.getenv("BREVO_USER")
SMTP_PASS = os.getenv("BREVO_PASS")

SENDER_EMAIL = os.getenv("SENDER_EMAIL") or os.getenv("BREVO_USER")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

if not PLAYLIST_URL:
    print("❌ Error: PLAYLIST_URL missing from .env file")
    sys.exit(1)

# Global trackers
successful_tracks = []
archived_tracks = []
failed_tracks = []  # Stores dicts: {"title": ..., "reason": ...}
current_processing_title = "Unknown Track"

# ==============================================================================
# HELPER: ARCHIVE READER
# ==============================================================================
def get_archived_ids():
    """Reads the archive file and returns a set of YouTube Video IDs."""
    archived_ids = set()
    if os.path.exists(ARCHIVE_FILE):
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split()
                    archived_ids.add(parts[-1])
    return archived_ids

# ==============================================================================
# HELPER: TEMPLATE ENGINE & MAILER
# ==============================================================================
def render_template(template_path, context):
    """Loads an HTML file and replaces {{ KEY }} placeholders with context values."""
    if not os.path.exists(template_path):
        print(f"⚠️ Template not found at {template_path}.")
        return "<p>Missing Template File</p>"
    
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    for key, value in context.items():
        placeholder = f"{{{{ {key} }}}}"
        html = html.replace(placeholder, str(value))
        
    return html

def send_email(subject, html_content):
    """Sends an HTML email via SMTP."""
    if not RECIPIENT_EMAIL or not SMTP_USER or not SMTP_PASS:
        print("⚠️ Email skipped: Missing recipient or SMTP credentials.")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SENDER_EMAIL
        msg["To"] = RECIPIENT_EMAIL

        recipients = [e.strip() for e in RECIPIENT_EMAIL.split(",") if e.strip()]
        msg.attach(MIMEText(html_content, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SENDER_EMAIL, recipients, msg.as_string())
        print(f"📧 Notification sent to {RECIPIENT_EMAIL}: [{subject}]")
    except Exception as e:
        print(f"⚠️ Failed to send email: {e}")

# ==============================================================================
# DATABASE LOGGING
# ==============================================================================
DB_CONN_STR = (
    f"DRIVER={{{DB_DRIVER}}};"
    f"SERVER={DB_SERVER};"
    f"DATABASE={DB_NAME};"
    f"UID={DB_USER};"
    f"PWD={DB_PASS};"
    "TrustServerCertificate=yes;"
)

def log_to_database(info):
    if not DB_SERVER or not DB_NAME:
        return

    try:
        conn = pyodbc.connect(DB_CONN_STR, timeout=5)
        cursor = conn.cursor()

        video_id = str(info.get("id", "UNKNOWN"))[:255]
        raw_title = info.get("title", "Unknown Title")
        title = str(raw_title)[:255]

        artist = str(info.get("artist") or info.get("uploader") or "Unknown Artist")[:255]
        album = str(info.get("album") or "YouTube Single")[:255]

        filepath = ""
        req_downloads = info.get("requested_downloads")
        if req_downloads and isinstance(req_downloads, list) and len(req_downloads) > 0:
            filepath = req_downloads[0].get("filepath", "")

        if not filepath:
            ext = info.get("ext", "mp4")
            filepath = os.path.join(DOWNLOAD_DIR, f"{title}.{ext}")

        file_name = str(os.path.basename(filepath))[:255]
        file_path = str(filepath)[:500]

        file_size_bytes = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        downloaded_at = datetime.datetime.now()

        query = """
            INSERT INTO dbo.Downloads 
            (youtube_id, title, artist, album, file_name, file_size_bytes, file_path, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """

        cursor.execute(query, (video_id, title, artist, album, file_name, file_size_bytes, file_path, downloaded_at))
        conn.commit()
        conn.close()
        print(f"    ✅ [DB LOG SUCCESS] Saved to MS SQL: {title}")

    except Exception as e:
        print(f"    ❌ [DB LOG ERROR] Failed to log '{info.get('title')}': {e}")

# ==============================================================================
# MAIN ENGINE
# ==============================================================================
start_time = time.time()

class YtLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg):
        if "Deprecated Feature:" in msg: return
        print(f"❌ [YT-DLP ERROR] {msg}")
        if any(err_kw in msg for err_kw in ["ERROR:", "HTTP Error", "unable to download", "not available"]):
            if not any(f["title"] == current_processing_title for f in failed_tracks):
                failed_tracks.append({
                    "title": current_processing_title,
                    "reason": msg.strip()
                })


def on_postprocessor_hook(d):
    if d["status"] == "finished" and d.get("postprocessor") == "MoveFiles":
        info = d.get("info_dict", {})
        title = info.get("title", "Unknown Track")
        if title not in successful_tracks:
            print(f"    ✅ [DOWNLOADED] {title}")
            successful_tracks.append(title)
            log_to_database(info)

def main():
    global current_processing_title
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(STAGING_DIR, exist_ok=True)

    print("🚀 Initializing YoutubeSync Engine...")

    # 1. Send Transfer Started Notification
    start_html = render_template(
        START_TEMPLATE_PATH, 
        {"PLAYLIST_URL": PLAYLIST_URL, "DOWNLOAD_DIR": DOWNLOAD_DIR}
    )
    send_email("Status: TRANSFER STARTED", start_html)

    # 2. Get Known Archived Video IDs
    existing_archive_ids = get_archived_ids()

    ydl_opts = {
    # Best available audio stream (no video)
    "format": "bestaudio/best",
    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
    "download_archive": ARCHIVE_FILE,
    "sleep_interval": 1,
    "max_sleep_interval": 3,
    "ignoreerrors": True,
    "no_warnings": True,
    "postprocessor_hooks": [on_postprocessor_hook],
    "logger": YtLogger(),
    "remote_components": ["ejs:github"],
    "writethumbnail": True,
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",   # 0 = best VBR (~245 kbps, V0). Use "320" for constant 320 kbps
        },
        {"key": "FFmpegMetadata", "add_metadata": True},
        {"key": "EmbedThumbnail"},
    ],
    "extractor_args": {
        "youtube": {"player_client": ["web", "mweb"]},
    },
}

    flat_opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
        "quiet": True,
        "remote_components": ["ejs:github"],  # Enables YouTube JS challenge solver
    }

    # 4. Extract Items & Process
    entries_to_process = []
    print("Extracting playlist metadata...")
    
    with yt_dlp.YoutubeDL(flat_opts) as ydl_flat:
        try:
            playlist_dict = ydl_flat.extract_info(PLAYLIST_URL, download=False)
            if playlist_dict:
                if "entries" in playlist_dict and playlist_dict["entries"] is not None:
                    # Multi-video playlist or mix
                    entries_to_process = [e for e in playlist_dict["entries"] if e is not None]
                else:
                    # Single video URL or standalone entry
                    entries_to_process = [playlist_dict]
        except Exception as err:
            print(f"⚠️ Critical Playlist Extraction Error: {err}")
            failed_tracks.append({
                "title": "Playlist Extraction Failure",
                "reason": str(err)
            })

    total_found = len(entries_to_process)
    print(f"📋 Found {total_found} items to process.\n")

    if entries_to_process:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl_downloader:
            for index, entry in enumerate(entries_to_process, start=1):
                # Extract Video ID properly across all format types
                video_id = entry.get("id") or entry.get("url") or entry.get("webpage_url_basename")
                
                if not video_id:
                    print(f"⚠️ Could not resolve Video ID for item #{index}")
                    continue

                # Strip out full URL if present to leave clean ID
                if "watch?v=" in video_id:
                    video_id = video_id.split("watch?v=")[-1].split("&")[0]

                video_title = entry.get("title") or f"Video ID: {video_id}"
                current_processing_title = video_title

                # Match against local archive file
                if video_id in existing_archive_ids:
                    print(f"ℹ️  ({index}/{total_found}) [ARCHIVED - SKIPPED]: {video_title}")
                    archived_tracks.append(f"{video_title} (ID: {video_id})")
                    continue

                video_url = f"https://www.youtube.com/watch?v={video_id}"
                print(f"▶️ ({index}/{total_found}) [DOWNLOADING]: {video_title}")

                try:
                    ydl_downloader.download([video_url])
                except Exception as item_err:
                    print(f"❌ [SKIP ERROR] {item_err}")
                    if not any(f["title"] == video_title for f in failed_tracks):
                        failed_tracks.append({
                            "title": video_title,
                            "reason": str(item_err)
                        })

    # 5. Format & Send Summary Email
    elapsed_minutes = round((time.time() - start_time) / 60, 2)
    total_new = len(successful_tracks)
    total_archived = len(archived_tracks)
    total_failed = len(failed_tracks)

    # Format Track Lists for Email
    success_list_html = "<br>".join([f"• {t}" for t in successful_tracks]) if successful_tracks else "No new tracks downloaded."
    archived_list_html = "<br>".join([f"• {t}" for t in archived_tracks]) if archived_tracks else "No archived items in this run."

    if total_failed > 0:
        status_badge = "TRANSFER PARTIALLY COMPLETED"
        status_color = "#dc2626"
        subject = "Status: TRANSFER PARTIALLY COMPLETED"
        alert_box_html = f"""
        <div class="alert-box">
            <strong>⚠️ PARTIAL FAILURE DETECTED:</strong><br>
            {total_failed} item(s) failed or were unavailable on YouTube.
        </div>
        """
        
        failed_formatted = "<br>".join([
            f"• <strong>{item['title']}</strong><br>&nbsp;&nbsp;&nbsp;&nbsp;<em>{item['reason'][:120]}...</em>" 
            for item in failed_tracks
        ])
        
        error_section_html = f"""
        <div class="section-title" style="color: #f87171;">Failed Tracks & Errors:</div>
        <div class="track-list" style="border-color: #991b1b;">{failed_formatted}</div>
        """
    else:
        status_badge = "TRANSFER COMPLETED"
        status_color = "#16a34a"
        subject = "Status: TRANSFER COMPLETED"
        alert_box_html = ""
        error_section_html = ""

    report_context = {
        "STATUS_BADGE": status_badge,
        "STATUS_COLOR": status_color,
        "ALERT_BOX_HTML": alert_box_html,
        "TOTAL_FOUND": total_found,
        "TOTAL_NEW": total_new,
        "TOTAL_ARCHIVED": total_archived,
        "TOTAL_FAILED": total_failed,
        "ELAPSED_MINUTES": elapsed_minutes,
        "SUCCESS_LIST_HTML": success_list_html,
        "ARCHIVED_LIST_HTML": archived_list_html,
        "ERROR_SECTION_HTML": error_section_html,
    }

    report_html = render_template(REPORT_TEMPLATE_PATH, report_context)
    send_email(subject, report_html)

    print("\n" + "=" * 50)
    print("FINISHED TRANSMISSION SUMMARY")
    print(f"Processed {total_new} new / {total_archived} archived / {total_failed} failed out of {total_found} items.")
    print("=" * 50)

if __name__ == "__main__":
    main()