#!/usr/bin/env python3
"""Evaluate YouTube match accuracy for a Spotify playlist."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ytdlp_app.match_config import get_match_config
from ytdlp_app.search import (
    SearchResult,
    _artist_overlap,
    _normalize_tokens,
    _score_audio_candidate,
    find_youtube_match_for_track,
)
from ytdlp_app.metadata.parse import ParsedTrack
from ytdlp_app.sources.spotify import SpotifySource

PLAYLIST = "https://open.spotify.com/playlist/7AwA0qhGLm6bG4dGkElJuM"
COOKIES = str(ROOT / "cookies.txt")
QUALITY = sys.argv[1] if len(sys.argv) > 1 else "fast"


def _token_overlap(a: str, b: str) -> float:
    ta = set(_normalize_tokens(a).split())
    tb = set(_normalize_tokens(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _classify(
    track_artist: str,
    track_title: str,
    match: SearchResult | None,
    score: float,
) -> str:
    if match is None:
        return "FAILED"
    artist_ov = _artist_overlap(match.uploader or "", track_artist)
    title_ov = _token_overlap(match.title, track_title)
    if score >= 0.75 and artist_ov >= 0.7 and title_ov >= 0.45:
        return "GOOD"
    if score >= 0.55 and title_ov >= 0.4:
        return "OK"
    if artist_ov < 0.35 and title_ov >= 0.4:
        return "WRONG_ARTIST"
    if score < 0.55:
        return "WEAK"
    return "REVIEW"


def main() -> None:
    cfg = get_match_config(QUALITY)
    print(f"Playlist: {PLAYLIST}")
    print(f"Match quality: {QUALITY} (limit={cfg.search_limit}, enrich={cfg.enrich_top_n})")
    print()

    src = SpotifySource()
    tracks = src.resolve_urls([PLAYLIST], progress=lambda m: None)
    print(f"Resolved {len(tracks)} Spotify tracks\n")

    rows: list[dict] = []
    for i, track in enumerate(tracks, 1):
        print(f"[{i}/{len(tracks)}] {track.display_title()[:70]}...", flush=True)
        match = find_youtube_match_for_track(
            track.artist,
            track.title,
            track.duration_s,
            cookies_path=COOKIES,
            use_youtube_music=True,
            match_quality=QUALITY,
            progress=lambda m: None,
        )
        stub = SearchResult(
            "", track.title, track.artist, track.duration_s,
            None, None, None,
        )
        parsed = ParsedTrack(artist=track.artist, title=track.title)
        score = (
            _score_audio_candidate(match, parsed, stub, expected_duration_s=track.duration_s)
            if match else 0.0
        )
        verdict = _classify(track.artist, track.title, match, score)
        rows.append({
            "spotify": track.display_title(),
            "youtube": match.display_title() if match else "—",
            "uploader": (match.uploader if match else "")[:40],
            "score": score,
            "verdict": verdict,
            "url": match.url if match else "",
        })

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for verdict in ("GOOD", "OK", "WEAK", "WRONG_ARTIST", "REVIEW", "FAILED"):
        if verdict in counts:
            print(f"  {verdict:12} {counts[verdict]:3}")

    good_ok = counts.get("GOOD", 0) + counts.get("OK", 0)
    total = len(rows)
    print(f"\n  Accuracy (GOOD+OK): {good_ok}/{total} ({100*good_ok/total:.0f}%)")

    problems = [r for r in rows if r["verdict"] not in ("GOOD", "OK")]
    if problems:
        print("\n" + "=" * 72)
        print("NEEDS REVIEW")
        print("=" * 72)
        for r in problems:
            print(f"\n  [{r['verdict']}] score={r['score']:.2f}")
            print(f"    Spotify:  {r['spotify']}")
            print(f"    YouTube:  {r['youtube']}")
            if r["uploader"]:
                print(f"    Uploader: {r['uploader']}")


if __name__ == "__main__":
    main()
