# easy-dlp

**A polished desktop app that makes YouTube downloading simple — built with Python, customtkinter, and yt-dlp.**

easy-dlp wraps the power of [yt-dlp](https://github.com/yt-dlp/yt-dlp) in a modern, responsive GUI. Search YouTube, pick what you want, and download audio, video, or thumbnails — or use **Music mode** to get properly tagged MP3s with cover art and synced lyrics. No terminal required.

> **For recruiters & reviewers:** This is a solo full-stack desktop project — UI, job orchestration, metadata pipeline, and packaging. It demonstrates threading/concurrency, API integration (iTunes Search), media post-processing (ffmpeg), persistent settings, and thoughtful UX (infinite scroll, thumbnail cache, macOS trackpad handling). See [Tech stack](#tech-stack) and [Project structure](#project-structure).

---

## Table of contents

1. [Installation](#installation)
2. [Quick start (after install)](#quick-start-after-install)
3. [Features](#features)
4. [How to use](#how-to-use)
5. [Requirements](#requirements)
6. [Troubleshooting](#troubleshooting)
7. [Tech stack](#tech-stack)
8. [Project structure](#project-structure)
9. [Manual install (developers)](#manual-install-developers)
10. [Distributing a standalone app](#distributing-a-standalone-app)
11. [License](#license)

---

## Installation

This section walks through getting easy-dlp running from scratch. You do **not** need prior Python experience — just follow the steps for your operating system.

### What you'll need

| Requirement | Why |
|---|---|
| **Git** (to download the project) | Clones the repository to your computer |
| **Python 3.10+** with **tkinter** | Runs the app; tkinter draws the GUI |
| **ffmpeg** | Converts and muxes audio/video (the app finds it automatically on most systems) |

The included `run.sh` launcher creates a private Python environment and installs everything else for you on first launch.

---

### Step 0 — Check whether you already have Python

Open a terminal and run:

```bash
python3 --version
```

- If you see `Python 3.10` or higher → skip to [Step 1](#step-1--download-the-project).
- If you get `command not found` or a version below 3.10 → install Python first (see below).

<details>
<summary><strong>macOS — install Python</strong></summary>

**Option A — Homebrew (recommended for developers)**

1. Install [Homebrew](https://brew.sh) if you don't have it.
2. Run:
   ```bash
   brew install python@3.12 python-tk@3.12
   ```
3. Verify:
   ```bash
   python3.12 --version
   python3.12 -c "import tkinter; print('tkinter OK')"
   ```

**Option B — python.org installer**

1. Go to [https://www.python.org/downloads/macos/](https://www.python.org/downloads/macos/).
2. Download the latest **Python 3.12** (or newer) macOS installer.
3. Run the `.pkg` and follow the prompts. Leave all default components checked.
4. Open a **new** Terminal window and run `python3 --version`.

</details>

<details>
<summary><strong>Windows — install Python</strong></summary>

1. Go to [https://www.python.org/downloads/windows/](https://www.python.org/downloads/windows/).
2. Download the latest **Python 3.12** (or newer) installer.
3. Run the installer. On the first screen, **check "Add python.exe to PATH"**.
4. Click **Customize installation** and make sure **tcl/tk and IDLE** is checked (this provides tkinter).
5. Finish installation, open **Command Prompt** or **PowerShell**, and run:
   ```bat
   python --version
   python -c "import tkinter; print('tkinter OK')"
   ```

</details>

<details>
<summary><strong>Linux (Debian/Ubuntu) — install Python</strong></summary>

```bash
sudo apt update
sudo apt install python3 python3-venv python3-tk
python3 --version
python3 -c "import tkinter; print('tkinter OK')"
```

</details>

---

### Step 1 — Download the project

**Option A — Git clone (recommended)**

```bash
git clone https://github.com/abl241/ytldp-app.git
cd ytldp-app
```

**Option B — Download ZIP**

1. On GitHub, click **Code → Download ZIP**.
2. Unzip the archive.
3. Open a terminal in the unzipped folder (on macOS: right-click the folder in Finder → **New Terminal at Folder**).

---

### Step 2 — Install ffmpeg

ffmpeg is required for audio/video conversion. The app searches common install locations automatically.

<details>
<summary><strong>macOS</strong></summary>

```bash
brew install ffmpeg
```

</details>

<details>
<summary><strong>Windows</strong></summary>

1. Download a build from [https://www.gyan.dev/ffmpeg/builds/](https://www.gyan.dev/ffmpeg/builds/) (choose *ffmpeg-release-essentials*).
2. Extract the ZIP and add the `bin` folder to your system **PATH**, **or** note the full path to `ffmpeg.exe` and set `FFMPEG_BINARY` to it before launching.

</details>

<details>
<summary><strong>Linux (Debian/Ubuntu)</strong></summary>

```bash
sudo apt install ffmpeg
```

</details>

---

### Step 3 — Launch easy-dlp

From the project folder:

**macOS / Linux**

```bash
chmod +x run.sh    # only needed once
./run.sh
```

**Windows**

Use Git Bash or WSL, or run manually:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On first run, `run.sh` will:

1. Find a Python 3.10+ interpreter with tkinter
2. Create a `.venv` folder (isolated dependency environment)
3. Install `yt-dlp`, `customtkinter`, `Pillow`, and `mutagen`
4. Open the GUI

Subsequent launches skip setup and open immediately.

---

### Step 4 — Verify everything works

Run the built-in diagnostics:

```bash
./run.sh --doctor
```

You should see at least one Python marked **OK** (tkinter present), ffmpeg found, and the venv dependencies installed.

**Other launcher commands:**

| Command | What it does |
|---|---|
| `./run.sh` | Launch the app |
| `./run.sh --update` | Upgrade dependencies inside `.venv` |
| `./run.sh --reset` | Delete `.venv` and rebuild from scratch |
| `./run.sh --help` | Print command list |

---

## Quick start (after install)

1. Open easy-dlp (`./run.sh`).
2. Pick a tab — **Download**, **Music**, or **Embed Thumbnail**.
3. Search YouTube or paste URLs.
4. Select results and click **Download**, or use **Download all**.
5. Watch progress in the **Active downloads** panel; finished jobs appear under **Recent jobs**.
6. Click **Open output folder** when a job completes.

---

## Features

### Download tab

| Feature | Description |
|---|---|
| **Multi-format downloads** | Download the same selection as **MP3** (audio), **MP4** (H.264/AAC video), and/or **JPG** (thumbnail only) in one pass |
| **YouTube search** | Type a query; results appear with thumbnails, duration, and channel name |
| **Paste URLs** | Paste video, playlist, or channel URLs — playlists/channels expand into individual videos |
| **Infinite scroll** | Scroll to the bottom of search results to load more automatically |
| **Smart filters** | Channels and playlists are always hidden from search; optional "prefer audio" filter skips music videos and live streams |
| **Per-result actions** | Download one item, pick an output folder override (📁), or batch-download the full list |
| **Concurrent jobs** | Configurable parallel download limit (default: 2) with live progress bars |
| **Cookies support** | Point to a Netscape-format cookies file for age-restricted or private content |

### Music tab

| Feature | Description |
|---|---|
| **Purpose-built music workflow** | Downloads MP3s with clean, title-based filenames |
| **iTunes metadata enrichment** | Matches tracks against the iTunes Search API for artist, album, year, genre, track/disc numbers |
| **Embedded cover art** | High-resolution album artwork written into the MP3 tags |
| **Synced lyrics** | Optional `.lrc` lyric files downloaded and embedded when available |
| **Prefer audio** | Automatically finds an official-audio or Topic-channel upload instead of a music video |
| **Audio-only search filter** | Optional filter to hide music videos and live performances from search results |
| **Search or paste** | Same search/paste workflow as the Download tab, tuned for music |

### Embed Thumbnail tab

| Feature | Description |
|---|---|
| **Single-file embed** | Pick one MP3 and one image; ffmpeg muxes the cover in-place |
| **Batch folder mode** | Point at a folder of audio + a folder of images; pairs and embeds automatically |

### App-wide UX

| Feature | Description |
|---|---|
| **Responsive UI** | All downloads run on background threads — the window never freezes |
| **Active / Recent panels** | Always-visible job queue with per-job progress, cancel, and reveal-in-finder |
| **Collapsible panels** | Collapse Active, Recent, or Log panels to save screen space |
| **Persistent settings** | Output folders, theme, filters, and UI state survive restarts |
| **Light / dark / system theme** | Follows your OS appearance or force light/dark |
| **Scroll direction control** | Auto-detect macOS natural scrolling, or force natural/inverted |
| **Thumbnail cache** | Search-result thumbnails are cached on disk for fast re-renders |
| **ffmpeg auto-discovery** | Finds ffmpeg on PATH, Homebrew paths, and bundled locations |

---

## How to use

### Download tab

1. Check one or more formats: **Audio (MP3)**, **Video (MP4)**, **Thumbnail (JPG)**.
2. Use **Search YouTube** or **Paste URLs**.
3. Review results, then click **Download** on individual rows or **Download all**.
4. Output goes to the folders configured in **Settings** (defaults: `~/Music`, `~/Movies`, `~/Pictures`).

### Music tab

1. Toggle **Download lyrics** and **Prefer audio** as desired.
2. Search or paste a URL.
3. Download — files land in your Music output folder with full ID3 tags and cover art.

### Cookies (optional)

For private or age-restricted videos, export a Netscape cookies file from your browser and set the path in **Settings → Cookies file**. See [`cookies.txt.example`](./cookies.txt.example) for format guidance.

### Settings location

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/easy-dlp/settings.json` |
| Linux | `~/.config/easy-dlp/settings.json` |
| Windows | `%APPDATA%\easy-dlp\settings.json` |

---

## Requirements

| Component | Version / notes |
|---|---|
| Python | 3.10+ with `tkinter` and `venv` stdlib modules |
| ffmpeg | Any recent build; must be on PATH or in a known location |
| yt-dlp | Installed automatically by `run.sh` |
| customtkinter | Installed automatically by `run.sh` |
| Pillow | Installed automatically by `run.sh` |
| mutagen | Installed automatically by `run.sh` |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Could not find a Python interpreter with tkinter` | Install `python-tk` / `python3-tk` for your Python version (see [Step 0](#step-0--check-whether-you-already-have-python)) |
| Downloads fail with ffmpeg errors | Run `ffmpeg -version` in a terminal; install ffmpeg if missing |
| `run.sh: Permission denied` | Run `chmod +x run.sh` once |
| Blank window on launch | Your Python may lack tkinter — run `./run.sh --doctor` |
| Age-restricted video fails | Add a cookies file in Settings |
| Stale yt-dlp / broken downloads | Run `./run.sh --update` or `./run.sh --reset` |

---

## Tech stack

| Layer | Technology | Role |
|---|---|---|
| Language | Python 3.10+ | Application logic |
| GUI | customtkinter (Tk) | Cross-platform desktop UI |
| Downloader | yt-dlp | YouTube extraction and download |
| Media | ffmpeg | Transcode, mux, embed thumbnails |
| Metadata | mutagen | ID3 tag read/write for MP3s |
| Images | Pillow | Thumbnail decode/resize for the UI |
| Music data | iTunes Search API | Album art, track metadata, duration matching |
| Lyrics | LRCLIB (via lyrics module) | Synced lyric fetch |
| Concurrency | `threading` + `queue` | Non-blocking UI with a worker job queue |
| Settings | JSON on disk | Persistent, OS-appropriate config directory |
| Packaging | `run.sh` + venv | One-command setup for non-developers |

**Design highlights for reviewers:**

- **Job queue with cancellation** — downloads, searches, and metadata enrichment are discrete job kinds with shared progress/cancel plumbing
- **UI thread safety** — worker threads post updates through a `queue.Queue`; the main thread polls and renders
- **Fuzzy audio matching** — Music mode scores YouTube audio candidates by token overlap, artist match, duration proximity, and Topic-channel heuristics
- **Infinite scroll without duplicate fetches** — search pagination state is tracked per-tab with exhaustion flags
- **macOS scroll handling** — detects Tk version and system scroll preference to avoid trackpad snap-back bugs

---

## Project structure

```
easy-dlp/
├── main.py                 # Convenience launcher (`python main.py`)
├── run.sh                  # One-command setup + launch script
├── pyproject.toml          # Package metadata and entry point
├── requirements.txt        # Pinned-floor dependencies
├── cookies.txt.example     # Template for browser cookie export
└── ytdlp_app/
    ├── __main__.py         # `python -m ytdlp_app` entry
    ├── gui.py              # customtkinter UI, tabs, scroll, job wiring
    ├── jobs.py             # Background job queue (search / download / music)
    ├── downloader.py       # yt-dlp wrappers for audio, video, thumbs, music
    ├── search.py           # YouTube search, URL resolve, audio candidate scoring
    ├── embed.py            # ffmpeg thumbnail embedding (single + batch)
    ├── music_postprocess.py# Cover art + lyrics write-back after download
    ├── settings.py         # Persistent JSON settings store
    ├── runtime.py          # ffmpeg discovery across PATH / Homebrew / bundled
    ├── thumbcache.py       # Disk cache for search-result thumbnails
    └── metadata/
        ├── itunes.py       # iTunes Search API client + fuzzy track matching
        ├── lyrics.py       # Synced lyric fetch
        ├── parse.py        # YouTube title → artist/track parsing
        └── tagger.py       # mutagen ID3 tagging
```

---

## Manual install (developers)

If you prefer managing the environment yourself:

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Or install as a package:

```bash
pip install -e .
easy-dlp
```

---

## Distributing a standalone app

Bundle a double-clickable app with [PyInstaller](https://pyinstaller.org/) — no Python install required for end users:

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller --windowed --onefile --name easy-dlp main.py
# macOS: dist/easy-dlp.app   Windows: dist/easy-dlp.exe
```

**Notes for distribution:**

1. The bundled app checks `/opt/homebrew/bin/ffmpeg` and other common paths, so Homebrew ffmpeg on macOS is usually enough.
2. For a fully self-contained bundle, place an `ffmpeg` binary next to the executable.
3. Unsigned macOS builds may require right-click → **Open** on first launch to bypass Gatekeeper.

---

## License

Personal-use project. [yt-dlp](https://github.com/yt-dlp/yt-dlp) is licensed under the [Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE); ffmpeg under LGPL/GPL depending on build.
