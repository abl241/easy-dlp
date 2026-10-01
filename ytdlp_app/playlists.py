"""Read-only snapshots of local Music playlists and exported Music XML files."""
from dataclasses import dataclass
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from urllib.parse import urlparse, unquote


@dataclass(frozen=True)
class PlaylistTrack:
    library_id: str
    title: str
    artist: str
    album: str = ""
    duration_s: float = 0
    apple_id: str = ""
    audio_path: str = ""


@dataclass(frozen=True)
class Playlist:
    id: str
    name: str
    tracks: tuple[PlaylistTrack, ...] | None = None
    special_kind: str = ""
    is_smart: bool = False

    @property
    def display_name(self):
        return f"{self.name} (Library)" if self.special_kind else self.name


_MUSIC_SCRIPT = r'''
function run(argv) {
    const music = Application('Music');
    if (!argv.length) {
        return JSON.stringify(music.userPlaylists().map(p => ({id: p.persistentID(), name: p.name(), special_kind: p.specialKind(), is_smart: p.smart()})));
    }
    const matches = music.userPlaylists.whose({persistentID: argv[0]})();
    if (!matches.length) throw new Error('Playlist no longer exists. Refresh Apple Music.');
    const tracks = matches[0].tracks;
    const ids = tracks.persistentID(), titles = tracks.name(), artists = tracks.artist();
    const albums = tracks.album(), durations = tracks.duration();
    const locations = {};
    // Read file-track locations in bulk; cloud-only tracks may have no file.
    try {
        const fileTracks = matches[0].fileTracks;
        const fileIDs = fileTracks.persistentID(), paths = fileTracks.location();
        fileIDs.forEach((id, i) => { if (paths[i]) locations[id] = String(paths[i]); });
    } catch (_) { /* Local artwork remains optional. */ }
    return JSON.stringify(titles.map((title, i) => ({library_id: ids[i], title: title,
        artist: artists[i], album: albums[i], duration_s: durations[i], audio_path: locations[ids[i]] || ""})));
}
'''


def _music_read(args, cancel):
    if sys.platform != 'darwin':
        raise RuntimeError('Reading the Music app requires macOS. You can import an exported Music XML playlist instead.')
    process = subprocess.Popen(['osascript', '-l', 'JavaScript', '-e', _MUSIC_SCRIPT, *args],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 90
    try:
        while True:
            if cancel.is_set():
                raise RuntimeError('Cancelled')
            if time.monotonic() >= deadline:
                raise RuntimeError('Music did not respond. Open Music, check Automation permissions, then try again.')
            try:
                stdout, stderr = process.communicate(timeout=.15)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError('Could not read Music playlists. Open Music and allow this app (or Terminal/Python) in System Settings → Privacy & Security → Automation. ' + stderr.strip()[:160])
        result = json.loads(stdout)
        if not isinstance(result, list):
            raise ValueError('Unexpected Music response.')
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def list_playlists(cancel):
    return [Playlist(str(row['id']), str(row['name']), special_kind=str(row.get('special_kind') or '') if row.get('special_kind') != 'none' else '', is_smart=bool(row.get('is_smart'))) for row in _music_read([], cancel)]


def load_tracks(playlist, cancel):
    if playlist.tracks is not None:
        return list(playlist.tracks)
    return [PlaylistTrack(**dict(row, audio_path=local_path(row.get("audio_path", "")))) for row in _music_read([playlist.id], cancel)]


def local_path(value):
    value = str(value or '')
    parsed = urlparse(value)
    if parsed.scheme == 'file':
        return unquote(parsed.path) if parsed.netloc in ('', 'localhost') else ''
    return value if not parsed.scheme else ''


def import_xml(path):
    with Path(path).open('rb') as stream:
        data = plistlib.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get('Tracks'), dict):
        raise ValueError('Choose a playlist or library XML exported from Music: File → Library → Export Playlist / Export Library.')
    tracks = {}
    for key, raw in data['Tracks'].items():
        if not raw.get('Name'):
            continue
        catalog = str(raw.get('Store ID') or '')
        tracks[str(key)] = PlaylistTrack(str(raw.get('Persistent ID') or key), raw['Name'], raw.get('Artist', ''),
                                        raw.get('Album', ''), float(raw.get('Total Time') or 0) / 1000,
                                        catalog if catalog.isdigit() else '', local_path(raw.get('Location', '')))
    result = []
    for index, raw in enumerate(data.get('Playlists', [])):
        if raw.get('Folder'):
            continue
        members = tuple(tracks[str(item['Track ID'])] for item in raw.get('Playlist Items', []) if str(item.get('Track ID')) in tracks)
        result.append(Playlist('xml:' + str(index), raw.get('Name') or 'Imported playlist', members))
    if not result and tracks:
        result.append(Playlist('xml:all', Path(path).stem, tuple(tracks.values())))
    return result
