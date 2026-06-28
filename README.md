# YTDLP — Desktop GUI for yt-dlp

A small Python + customtkinter desktop app that wraps
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) for four common workflows:

- Download audio as **MP3** with embedded thumbnail + metadata
- Download video as **MP4** (H.264/AAC where possible) with embedded thumbnail
- Download **thumbnails only** (JPG)
- Embed a new thumbnail into an existing MP3 (single file or batch folder)

> This is a rewrite of an earlier JavaFX/Maven implementation. The Java code is
> preserved on the `java-final` git tag.

## Run it (easy path)

The fastest way to launch — handles its own Python virtualenv:

```bash
./run.sh
```

On first run this creates `.venv`, installs `yt-dlp` and `customtkinter`,
and launches the GUI. On subsequent runs it just launches.

Other commands:

```bash
./run.sh --update     # upgrade yt-dlp and customtkinter inside the venv
./run.sh --reset      # delete .venv and rebuild from scratch
./run.sh --doctor     # show diagnostics: which Pythons have tkinter, ffmpeg, deps
./run.sh --help       # print this list
```

## Requirements

The launcher will tell you in `--doctor` if any of these are missing:

- **Python 3.10+** with the `tkinter` stdlib module.
  - macOS: `brew install python-tk@3.12` if your Homebrew Python doesn't bundle Tk.
  - Debian/Ubuntu: `sudo apt install python3-tk`.
  - Windows: install Python from python.org with "tcl/tk and IDLE" checked.
- **ffmpeg** on `PATH`, or set `FFMPEG_BINARY=/full/path/to/ffmpeg`.
  - macOS: `brew install ffmpeg`.
  - The app also checks `/opt/homebrew/bin/ffmpeg`, `/usr/local/bin/ffmpeg`,
    and similar locations automatically.

## Manual install (if `run.sh` doesn't fit your workflow)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## How to use

1. (Optional) Point the **Cookies file** field at a Netscape-format cookies
   file if you need to download private / age-restricted videos. See
   [`cookies.txt.example`](./cookies.txt.example) for how to obtain one.
2. Pick a tab: **Audio**, **Video**, **Thumbnails**, or **Embed Thumbnail**.
3. Paste one or more YouTube URLs — one per line. Playlists, channel URLs,
   and single videos all work.
4. Choose an output directory.
5. Click the action button. The window stays responsive during the download;
   progress lines appear in the log pane and status bar at the bottom.
6. When a job finishes, click **Open output folder** to reveal the results.

Settings are persisted to your OS-appropriate config dir:

- macOS: `~/Library/Application Support/ytdlp-app/settings.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/ytdlp-app/settings.json`
- Windows: `%APPDATA%\ytdlp-app\settings.json`

## Project layout

```
ytdlp-app/
├── main.py                # convenience launcher
├── run.sh                 # one-command launcher (auto-builds .venv)
├── pyproject.toml         # packaging + `ytdlp-app` entry point
├── requirements.txt       # pinned-floor dependencies
├── cookies.txt.example    # template (real cookies.txt is gitignored)
├── README.md
└── ytdlp_app/
    ├── __init__.py
    ├── __main__.py        # `python -m ytdlp_app`
    ├── gui.py             # customtkinter UI + threading plumbing
    ├── downloader.py      # yt-dlp wrappers (audio/video/thumbs)
    ├── embed.py           # ffmpeg thumbnail embedding (single + folder)
    ├── runtime.py         # ffmpeg discovery across PATH/Homebrew/bundled
    └── settings.py        # persistent JSON key/value store
```

## Distribute to non-technical users (no Python install required)

Once everything works, you can bundle a single double-clickable app with
[PyInstaller](https://pyinstaller.org/):

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller --windowed --onefile --name YTDLP main.py
# Result: dist/YTDLP.app on macOS, dist/YTDLP.exe on Windows.
```

A few extra notes when bundling for friends:

1. The bundled app launches from Finder/Spotlight with a sanitized `PATH`.
   The runtime checks `/opt/homebrew/bin/ffmpeg` and other common locations,
   so as long as the user has Homebrew ffmpeg, the app will find it.
2. To make the bundle fully self-contained, place an `ffmpeg` binary next
   to the executable (`dist/YTDLP/ffmpeg` for folder mode), and the app will
   discover it automatically via `runtime.py`.
3. The first launch on macOS may require right-click → Open to bypass
   Gatekeeper since the bundle isn't code-signed.

## Why a virtualenv?

You can skip the venv and `pip install yt-dlp customtkinter` globally if you
prefer — nothing in the app requires isolation. The venv exists to keep
this project's dependencies from clashing with other Python projects on
your machine. For a single-user personal tool both approaches are fine;
`./run.sh` chooses the venv path because it's the most reproducible and
doesn't pollute your system Python.

## License

Personal-use project. yt-dlp is licensed under the
[Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE); ffmpeg
under LGPL/GPL depending on build.
