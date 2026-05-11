# YTDLP — Desktop GUI for yt-dlp

A small **Python + customtkinter** desktop app that wraps
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) for four common workflows:

- Download audio as **MP3** with embedded thumbnail + metadata
- Download video as **MP4** (H.264/AAC where possible) with embedded thumbnail
- Download **thumbnails only** (JPG)
- Embed a new thumbnail into an existing MP3 (single file or batch from a folder)

> This is a rewrite of an earlier JavaFX/Maven implementation. The Java code
> is preserved on the `java-final` git tag and on the `main` branch in
> earlier history if you ever want to look at it.

## Requirements

- Python **3.10+**
- [`ffmpeg`](https://ffmpeg.org/) on your `PATH`
  (macOS: `brew install ffmpeg`)

## Install

```bash
# Clone and enter
git clone https://github.com/abl241/ytldp-app.git ytdlp-app
cd ytdlp-app

# (Recommended) create a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Run

```bash
python main.py
# or, if you ran `pip install -e .`:
ytdlp-app
```

## How to use

1. (Optional) Point the "Cookies file" field at a Netscape-format cookies file
   if you need to download private/age-restricted videos.
   See [`cookies.txt.example`](./cookies.txt.example) for how to obtain one.
2. Pick a tab (Audio / Video / Thumbnails / Embed Thumbnail).
3. Paste one or more YouTube URLs (one per line — playlists, channel URLs,
   and single videos all work).
4. Choose an output directory.
5. Click the action button. The window stays responsive during the download
   thanks to a background worker thread; progress lines appear in the log
   pane and status bar at the bottom.

Settings are persisted to `~/.config/ytdlp-app/settings.json` (macOS/Linux)
or `%APPDATA%/ytdlp-app/settings.json` (Windows) so the app remembers your
URLs and output directories between runs.

## Project layout

```
ytdlp-app/
├── main.py                # convenience launcher
├── pyproject.toml         # packaging metadata + `ytdlp-app` entry point
├── requirements.txt       # pinned-floor dependencies
├── cookies.txt.example    # template (real cookies.txt is gitignored)
├── README.md
└── ytdlp_app/
    ├── __init__.py
    ├── __main__.py        # `python -m ytdlp_app` entry point
    ├── gui.py             # customtkinter UI
    ├── downloader.py      # yt-dlp wrappers (audio/video/thumbs)
    ├── embed.py           # ffmpeg thumbnail embedding
    └── settings.py        # persistent JSON key/value store
```

## Packaging into a standalone .app

After everything works to your satisfaction:

```bash
pip install pyinstaller
pyinstaller --windowed --onefile --name YTDLP main.py
# Output: dist/YTDLP (or dist/YTDLP.app on macOS)
```

Note: PyInstaller bundles your installed `yt-dlp` version. To update, rebuild.

## License

Personal-use project. yt-dlp is licensed under the
[Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE); ffmpeg under
LGPL/GPL depending on build.
