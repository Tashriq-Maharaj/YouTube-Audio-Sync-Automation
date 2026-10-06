import datetime
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dotenv import load_dotenv
import pyodbc
import yt_dlp

# ==============================================================================
# PATHS AND CONFIGURATION
# ==============================================================================
# Dynamically resolves the script directory (works for both audio & video syncs)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

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

# Telegram Bot Settings
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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
# HELPER: TELEGRAM NOTIFIER
# ==============================================================================
def chunk_message(text, max_length=4000):
    """Splits message into chunks under max_length without breaking lines where possible."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_chunk = []
    current_len = 0

    for line in text.split("\n"):
        line_len = len(line) + 1  # include newline length
        if current_len + line_len > max_length:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_len = line_len
            else:
                chunks.append(line[:max_length])
                current_chunk = [line[max_length:]]
                current_len = len(line[max_length:]) + 1
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks

def send_telegram_notification(message, parse_mode="HTML"):
    """Sends a message via the Telegram Bot API using Python's standard library."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    headers = {"Content-Type": "application/json"}
    chunks = chunk_message(message)

    all_success = True
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": parse_mode,
            "link_preview_options": {"is_disabled": True},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status != 200:
                    print(f"⚠️ Telegram API returned status {response.status}")
                    all_success = False
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            print(f"⚠️ Failed to send Telegram message (HTTP {e.code}): {error_body}")
            all_success = False
        except Exception as e:
            print(f"⚠️ Failed to send Telegram message: {e}")
            all_success = False

    if all_success:
        print("📱 Telegram notification sent successfully.")
    return all_success

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

    conn = None
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
            ext = info.get("ext", "mp3")
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
        print(f"    ✅ [DB LOG SUCCESS] Saved to MS SQL: {title}")

    except Exception as e:
        print(f"    ❌ [DB LOG ERROR] Failed to log '{info.get('title')}': {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

# ==============================================================================
# MAIN ENGINE
# ==============================================================================
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
    start_time = time.time()
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(STAGING_DIR, exist_ok=True)

    print("🚀 Initializing YoutubeSync Engine...")

    # 1. Send Transfer Started Notification
    start_msg = (
        "🚀 <b>YouTube Audio Sync: TRANSFER STARTED</b>\n\n"
        f"🔗 <b>Playlist:</b> <a href=\"{html.escape(PLAYLIST_URL)}\">{html.escape(PLAYLIST_URL)}</a>"
        "\n\n"
        f"📁 <b>Destination:</b> <code>{html.escape(DOWNLOAD_DIR)}</code>\n"
        "\n"
        "🎵 <b>Format:</b> MP3 Audio (V0 VBR)"
    )
    send_telegram_notification(start_msg)

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

    # 5. Format & Send Summary Telegram Notification
    elapsed_minutes = round((time.time() - start_time) / 60, 2)
    total_new = len(successful_tracks)
    total_archived = len(archived_tracks)
    total_failed = len(failed_tracks)

    if total_failed > 0:
        status_header = "⚠️ <b>YouTube Audio Sync: TRANSFER PARTIALLY COMPLETED</b>"
    else:
        status_header = "✅ <b>YouTube Audio Sync: TRANSFER COMPLETED</b>"

    report_lines = [
        status_header,
        "",
        "📊 <b>Summary:</b>",
        f"• <b>Total in playlist:</b> {total_found}",
        f"• <b>Newly downloaded:</b> {total_new}",
        f"• <b>Already in archive:</b> {total_archived}",
        f"• <b>Failed / errors:</b> {total_failed}",
        f"• <b>Execution time:</b> {elapsed_minutes} minutes",
    ]

    if successful_tracks:
        report_lines.append("")
        report_lines.append("📥 <b>Newly Downloaded:</b>")
        for track in successful_tracks:
            report_lines.append(f"• {html.escape(track)}")

    if failed_tracks:
        report_lines.append("")
        report_lines.append("❌ <b>Failed Tracks & Errors:</b>")
        for item in failed_tracks:
            reason = item["reason"][:120] + "..." if len(item["reason"]) > 120 else item["reason"]
            report_lines.append(f"• <b>{html.escape(item['title'])}</b>\n  ↳ <i>{html.escape(reason)}</i>")

    if not successful_tracks and not failed_tracks:
        report_lines.append("")
        report_lines.append("ℹ️ <i>All tracks are already up to date in the archive.</i>")

    report_msg = "\n".join(report_lines)
    send_telegram_notification(report_msg)

    print("\n" + "=" * 50)
    print("FINISHED TRANSMISSION SUMMARY")
    print(f"Processed {total_new} new / {total_archived} archived / {total_failed} failed out of {total_found} items.")
    print("=" * 50)

if __name__ == "__main__":
    main()