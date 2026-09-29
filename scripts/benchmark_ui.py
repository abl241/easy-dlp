"""Compare native row rendering across checkouts without network or downloads.

Usage: .venv/bin/python scripts/benchmark_ui.py [path-to-checkout]
Requires a graphical desktop. Uses temporary settings and 100 synthetic rows.
"""
import sys, time, json, tempfile, statistics, faulthandler
from pathlib import Path
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1]))
faulthandler.dump_traceback_later(40,exit=True)
from ytdlp_app.settings import Settings
from ytdlp_app.gui import App
from ytdlp_app.search import SearchResult
with tempfile.TemporaryDirectory() as folder:
    started=time.monotonic()
    app=App(Settings(Path(folder)/'settings.json'))
    app.title('easy-dlp — Performance check')
    startup=time.monotonic()-started
    ticks=[]
    last=[time.monotonic()]
    render=[0.0]
    def heartbeat():
        now=time.monotonic()
        ticks.append((now-last[0])*1000)
        last[0]=now
        app.after(10,heartbeat)
    def begin():
        ticks.clear()
        last[0]=time.monotonic()
        render[0]=time.monotonic()
        app.music_results=[SearchResult(url=f'https://example.test/{i}',title=f'Performance test track {i}',uploader='Test artist',duration_s=200,view_count=None,upload_date=None,thumbnail_url=None) for i in range(100)]
        app._music_render_results()
        app.after(10,check)
    def check():
        if len(app._music_result_rows)<100:
            app.after(10,check)
            return
        print(json.dumps({'startup_ms':round(startup*1000),'render_100_ms':round((time.monotonic()-render[0])*1000),'heartbeat_max_ms':round(max(ticks)),'heartbeat_median_ms':round(statistics.median(ticks))}),flush=True)
        app._on_close()
    app.after(10,heartbeat)
    app.after(300,begin)
    app.mainloop()
faulthandler.cancel_dump_traceback_later()
