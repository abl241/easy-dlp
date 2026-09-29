"""Offline scheduling benchmark; simulated I/O, not a YouTube speed claim.

Run from the repository: .venv/bin/python scripts/benchmark_pipeline.py
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ytdlp_app import downloader, jobs, music_postprocess


def benchmark(tracks: int, download_s: float, tag_s: float) -> dict:
    def serial_track():
        time.sleep(download_s)
        time.sleep(tag_s)

    baselines = {}
    for workers in (1, 2):
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda _: serial_track(), range(tracks)))
        baselines[str(workers)] = time.perf_counter() - started

    condition = threading.Condition()
    completed = {}

    def notify(job):
        if job.is_terminal:
            with condition:
                completed[job.id] = job.state
                condition.notify_all()

    def download(urls, out_dir, **kwargs):
        time.sleep(download_s)
        return downloader.MusicDownloadResult(True, output_paths=[f'{urls[0]}.mp3'])

    def tag(path, **kwargs):
        time.sleep(tag_s)
        return music_postprocess.PostprocessResult(final_path=path)

    queue = jobs.JobQueue(2, notify)
    try:
        with patch.object(jobs.dl, 'download_music', side_effect=download), patch.object(jobs.mp, 'process_track', side_effect=tag):
            started = time.perf_counter()
            for i in range(tracks):
                queue.enqueue('music', str(i), url=str(i), output_dir='unused', source_title=str(i))
            with condition:
                if not condition.wait_for(lambda: len(completed) == tracks, timeout=max(10, tracks * (download_s + tag_s) * 2)):
                    raise RuntimeError('Benchmark timed out')
            elapsed = time.perf_counter() - started
            if any(state != jobs.DONE for state in completed.values()):
                raise RuntimeError(f'Unexpected job states: {completed}')
    finally:
        queue.shutdown(wait=True)
    return {
        'measurement': 'synthetic scheduling only; no network, ffmpeg, or disk I/O',
        'tracks': tracks,
        'download_ms_per_track': download_s * 1000,
        'tag_ms_per_track': tag_s * 1000,
        'previous_playlist_one_worker_s': round(baselines['1'], 3),
        'combined_two_workers_s': round(baselines['2'], 3),
        'pipeline_two_download_two_tag_workers_s': round(elapsed, 3),
        'speedup_vs_previous_playlist': round(baselines['1'] / elapsed, 2),
        'speedup_vs_combined_two_workers': round(baselines['2'] / elapsed, 2),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tracks', type=int, default=24)
    parser.add_argument('--download-ms', type=float, default=40)
    parser.add_argument('--tag-ms', type=float, default=80)
    args = parser.parse_args()
    if args.tracks < 1 or min(args.download_ms, args.tag_ms) < 0:
        parser.error('tracks must be positive and delays must be nonnegative')
    print(json.dumps(benchmark(args.tracks, args.download_ms / 1000, args.tag_ms / 1000), indent=2))
