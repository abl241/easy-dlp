"""Validate link imports before starting background work."""

from urllib.parse import urlsplit

from .sources import detect_platform


def parse_import_links(text: str, *, music: bool = False) -> tuple[list[str], str | None]:
    urls = []
    seen = set()
    platforms = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        url = line.strip()
        if not url:
            continue
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
        except ValueError:
            valid = False
        if not valid or any(char.isspace() for char in url):
            raise ValueError(f"Line {line_number}: paste a complete link, one per line.")
        if music:
            platform = detect_platform(url)
            if platform is None:
                raise ValueError(f"Line {line_number}: use a YouTube or Spotify link.")
            platforms.add(platform)
        if url not in seen:
            urls.append(url)
            seen.add(url)
    if not urls:
        raise ValueError("Paste at least one link to preview its tracks." if music else
                         "Paste at least one link to preview its videos.")
    if len(platforms) > 1:
        raise ValueError("Import YouTube and Spotify links separately, one service at a time.")
    return urls, next(iter(platforms), None)
