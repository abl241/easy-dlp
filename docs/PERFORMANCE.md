# Download pipeline performance

The `codex/download-performance` branch removes avoidable serial work without changing match scoring, audio quality, or metadata preferences.

## Scheduling

Music downloads now hand successful files to a separate two-worker tagging pool. The download slot becomes available while metadata, lyrics, and Apple Music import run. Jobs remain active through the final stage; failures and cancellation still finish the same job. An output filename stays reserved until tagging/import finishes, preventing a second music job from overwriting it in the meantime.

Fast and Balanced playlists now allow up to two download workers, respecting a lower user setting. Accurate already allowed two. Matching uses a shared two-worker pool with at most two outstanding matches per playlist. Results can arrive out of order but are stored at their original indexes. The existing inter-track delay now spaces submission times rather than adding a sleep after every completed request. The rate-limit backoff remains shared; request-delay preferences are scoped to each operation and copied into enrichment threads.

## Timings

Each job exposes `timings` in seconds. Music jobs record queue wait, preferred-audio selection, download (including extraction and MP3 conversion), tagging queue wait, tagging, optional import, and total elapsed time. Playlist matching records matching elapsed time. `work` measures the initial dispatch and overlaps its component timings; do not add it to those components. Successful completion messages include the timing breakdown. These timings are in memory, not persisted to a log file.

## Request reuse

Successful iTunes results are cached for five minutes, with a 128-entry bound and defensive copies. Failed and empty responses are retried normally. Concurrent first requests for the same URL can still duplicate a fetch; the cache lock does not serialize network traffic. Lyrics search reuses lyrics present in the selected response. Download percentage and text callbacks are both throttled to reduce UI events.

## Reproducible verification

Run the offline regressions:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*performance.py' -v
```

Run the scheduling benchmark:

```sh
.venv/bin/python scripts/benchmark_pipeline.py
```

The benchmark simulates 24 tracks, each with 40 ms of downloading and 80 ms of tagging. One local run measured 3.147 s for the previous one-worker playlist model, 1.620 s for two combined download/tag workers, and 1.083 s for two download workers plus two tag workers. This demonstrates scheduling overlap only; it excludes real network, disk, ffmpeg, matching, and Apple Music operations. It is not a promise of equivalent real-world speedup.

For a real comparison, use the same playlist, quality settings, cookies, output format, and duplicate policy. Compare total wall time, completion/failure counts, rate-limit messages, and stage timings. Warm metadata caches affect repeated runs. Optional slow lyrics can still dominate tagging; increased concurrency can trigger service throttling. Native-format audio and shorter lyrics timeouts remain separate future changes.
