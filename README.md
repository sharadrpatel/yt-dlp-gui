# yt-dlp GUI (macOS)

A feature-rich macOS GUI for yt-dlp.

## Features
- Queue-based downloads
- Format selection
- Audio extraction
- Subtitles
- Metadata embedding
- Progress + logs

## Download
Grab the latest macOS app from **Releases**.

If macOS blocks it:
Right-click → Open → Open

## Requirements
For best results:
brew install ffmpeg

## Development
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python ytdlp_gui.py
