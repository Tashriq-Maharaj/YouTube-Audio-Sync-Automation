# YouTube Audio Sync

A Python-based automation tool for synchronizing audio from a configured YouTube playlist to a local media library.

The application downloads new items as MP3 files, keeps track of previously processed videos, records download information in Microsoft SQL Server, and sends HTML email notifications with a summary of each transfer.

## Features

* Downloads the best available audio from YouTube and converts it to MP3 using FFmpeg.
* Automatically skips videos that have already been processed.
* Records downloaded media and metadata in Microsoft SQL Server.
* Sends HTML email notifications when a transfer starts and finishes.
* Reports failed or unavailable items in the completion email.
* Provides a summary of newly downloaded, archived, and failed items.
* Uses environment variables for configuration instead of hard-coded credentials.
* Uses separate HTML templates for email notifications and reports.

## How It Works

The application follows a simple automated workflow:

1. Loads configuration from a `.env` file.
2. Validates the required playlist configuration.
3. Reads the local download archive to identify previously processed videos.
4. Extracts the playlist metadata using `yt-dlp`.
5. Compares each video against the existing archive.
6. Downloads new videos as audio.
7. Converts the audio to MP3 using FFmpeg.
8. Embeds available metadata and thumbnails.
9. Records successful downloads in Microsoft SQL Server.
10. Generates an HTML transfer report.
11. Sends the report through an SMTP server.

Previously processed videos are skipped using `yt-dlp`'s download archive functionality, helping prevent duplicate downloads.

## Technologies Used

* **Python** – Automation and application logic
* **yt-dlp** – YouTube media extraction and downloading
* **FFmpeg** – Audio extraction and MP3 conversion
* **Microsoft SQL Server** – Download metadata and history
* **pyodbc** – Python-to-SQL Server connectivity
* **SMTP** – Email notifications
* **python-dotenv** – Environment-based configuration
* **HTML/CSS** – Email reporting templates

## Project Structure

```text
YouTube-Audio-Sync/
│
├── main.py
├── email_start.html
├── email_report.html
├── archive.txt
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

> The actual project may contain additional deployment or configuration files depending on the environment where the application is running.

## Requirements

Before running the application, install:

* Python 3.10+
* FFmpeg
* Microsoft ODBC Driver for SQL Server
* A Microsoft SQL Server instance
* An SMTP account capable of sending email
* Access to the YouTube playlist being processed

## Installation

### 1. Clone the repository

```bash
git clone <your-repository-url>
cd YouTube-Audio-Sync
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
```

Activate it on Linux/macOS:

```bash
source .venv/bin/activate
```

On Windows:

```powershell
.venv\Scripts\activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Install FFmpeg

FFmpeg must be installed separately because it is an external system dependency used by `yt-dlp` for audio conversion.

Verify the installation with:

```bash
ffmpeg -version
```

## Configuration

The application uses environment variables so that credentials and environment-specific settings are not stored in the source code.

Create a `.env` file in the project directory based on `.env.example`.

Example:

```env
PLAYLIST_URL=https://www.youtube.com/playlist?list=YOUR_PLAYLIST_ID

STAGING_DIR=/tmp/yt_staging
DESTINATION_DIR=/path/to/media

ARCHIVE_FILE=/path/to/archive.txt

MSSQL_SERVER=your-server
MSSQL_DATABASE=your-database
MSSQL_USER=your-user
MSSQL_PASSWORD=your-password
MSSQL_DRIVER=ODBC Driver 18 for SQL Server

BREVO_SMTP_HOST=smtp-relay.brevo.com
BREVO_SMTP_PORT=587
BREVO_USER=your-smtp-user
BREVO_PASS=your-smtp-password

SENDER_EMAIL=sender@example.com
RECIPIENT_EMAIL=recipient@example.com
```

**Do not commit the `.env` file to GitHub.**

The repository should contain `.env.example` with placeholder values instead.

## Database

The application stores information about successfully downloaded media in a SQL Server table.

The recorded information includes:

* YouTube video ID
* Title
* Artist/uploader
* Album
* File name
* File size
* File path
* Download timestamp

The database connection is configured through environment variables, allowing the same application to be used with different database environments without changing the Python source code.

## Email Reporting

The application sends two types of notifications:

### Transfer Started

A notification is sent when a synchronization run begins.

### Transfer Report

At the end of the run, an HTML report provides:

* Total items found
* Newly downloaded items
* Previously archived items
* Failed items
* Processing time
* Error details where applicable

This provides a simple way to monitor an automated job without needing to manually inspect the server after every run.

## Archive Handling

The application maintains an archive of previously processed YouTube videos.

When the playlist is scanned, the application checks the video ID against the archive before attempting a download.

This allows the same playlist to be processed repeatedly while only downloading new content.

## Error Handling

The application tracks failures during processing rather than allowing one unavailable video to stop the entire synchronization process.

Failures can include:

* Videos that are unavailable
* Download errors
* Playlist extraction failures
* Database logging failures
* Email delivery failures

Failed items are included in the final HTML report when applicable.

## Running the Application

After configuration is complete:

```bash
python main.py
```

A typical run will:

```text
Initializing YoutubeSync Engine...
Extracting playlist metadata...
Found X items to process.

[DOWNLOADING] Example Track
[DOWNLOADED] Example Track

FINISHED TRANSMISSION SUMMARY
Processed X new / X archived / X failed out of X items.
```

## Security

Sensitive configuration is intentionally kept outside the source code.

The project uses environment variables for:

* Database credentials
* SMTP credentials
* Email addresses
* Playlist configuration
* Environment-specific filesystem paths

The `.env` file should never be committed to source control.

## Intended Use

This project was created as a personal automation and software engineering project.

Users are responsible for ensuring that any media they download is content they own or are authorized to download, and for complying with applicable copyright laws and the terms of the services they use.

## Future Improvements

Potential improvements include:

* Automated scheduled execution
* More comprehensive automated testing
* Improved structured logging
* Configuration validation
* Additional database reporting
* Improved retry handling for temporary failures
* Separation of downloading, reporting, and database functionality into dedicated modules

## Author

**Tashriq Maharaj**

Software Engineering student interested in Python automation, infrastructure, systems operations, and DevOps.
