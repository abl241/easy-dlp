"""Map music match quality settings to concrete search/download parameters."""

from __future__ import annotations

from dataclasses import dataclass

MatchQuality = str  # "fast" | "balanced" | "accurate"

_LABELS = {
    "fast": "Fast (playlists)",
    "balanced": "Balanced",
    "accurate": "Accurate",
}


@dataclass(frozen=True)
class MatchQualityConfig:
    name: str
    search_limit: int
    enrich_top_n: int
    fallback_min_score: float
    inter_track_delay_s: float
    sleep_interval_requests: float
    min_match_score: float
    playlist_parallel: int

    @property
    def label(self) -> str:
        return _LABELS.get(self.name, self.name)


_PRESETS: dict[str, MatchQualityConfig] = {
    "fast": MatchQualityConfig(
        name="fast",
        search_limit=5,
        enrich_top_n=0,
        fallback_min_score=0.65,
        inter_track_delay_s=1.5,
        sleep_interval_requests=1.0,
        min_match_score=0.55,
        playlist_parallel=1,
    ),
    "balanced": MatchQualityConfig(
        name="balanced",
        search_limit=8,
        enrich_top_n=3,
        fallback_min_score=0.55,
        inter_track_delay_s=1.0,
        sleep_interval_requests=0.75,
        min_match_score=0.55,
        playlist_parallel=1,
    ),
    "accurate": MatchQualityConfig(
        name="accurate",
        search_limit=12,
        enrich_top_n=5,
        fallback_min_score=0.0,
        inter_track_delay_s=0.5,
        sleep_interval_requests=0.5,
        min_match_score=0.50,
        playlist_parallel=2,
    ),
}


def get_match_config(quality: str | None) -> MatchQualityConfig:
    key = (quality or "balanced").strip().lower()
    return _PRESETS.get(key, _PRESETS["balanced"])


def match_quality_choices() -> list[tuple[str, str]]:
    return [(key, _LABELS[key]) for key in ("fast", "balanced", "accurate")]
