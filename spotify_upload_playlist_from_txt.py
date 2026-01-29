#!/usr/bin/env python3
"""
Upload a playlist to Spotify from a cleaned TXT file.

Features:
- Append mode (default): add only missing tracks
- Overwrite mode (--overwrite): clear playlist then upload
- Fallback search logic for robust matching
- Validation report before upload
- Progress output so the script never looks stuck

Requires:
    pip install spotipy
"""

import argparse
import os
import sys
import time
from typing import List, Dict, Optional, Set

from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

import re
from difflib import SequenceMatcher

# =========================
# CONFIG – CHANGE IF NEEDED
# =========================

CLIENT_ID = "<your_spotify_client_id>"
CLIENT_SECRET = "<your_spotify_client_secret>"
REDIRECT_URI = "http://127.0.0.1:8888/callback"

SCOPE = (
    "playlist-read-private "
    "playlist-read-public "
    "playlist-modify-private "
    "playlist-modify-public"
)

SEARCH_DELAY_SECONDS = 0.25
ADD_BATCH_SIZE = 100
REMOVE_BATCH_SIZE = 100
DEBUG = False


# =========================
# AUTH
# =========================

spotify_client = Spotify(
    auth_manager=SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        show_dialog=True,
    )
)

def normalize(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    text = text.replace("&", "and")
    text = re.sub(r"[./\-]", "", text)   # <-- ADD THIS
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def normalize_playlist_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())

def normalize_track_for_matching(name: str) -> str:
    name = normalize(name)
    name = re.sub(r"\s*-\s*live.*$", "", name)
    return name

def is_exact_track_artist_match(entry: dict, track: dict) -> bool:
    entry_track = normalize_track_for_matching(entry["track"])
    track_name = normalize_track_for_matching(track["name"])

    if entry_track != track_name:
        return False

    entry_artist = normalize(entry.get("artist", ""))
    track_artists = [normalize(a["name"]) for a in track["artists"]]

    return entry_artist and entry_artist in track_artists

def clean_album_name(album: str, artist: str) -> str:
    """
    Remove artist prefix from album name if present.
    Example:
    'Al Green - Let's Stay Together' → 'Let's Stay Together'
    """
    if not album or not artist:
        return album

    album_norm = normalize(album)
    artist_norm = normalize(artist)

    prefix = artist_norm + " - "
    if album_norm.startswith(prefix):
        return album[len(prefix):].strip()

    return album

def similarity(a: str, b: str) -> float:
    """
    Return similarity ratio between two strings (0.0 – 1.0).
    """
    return SequenceMatcher(None, a, b).ratio()

def score_candidate(entry: dict, track: dict) -> int:
    """
    Assign a confidence score to a Spotify track candidate.
    """

    score = 0

    entry_track = normalize_track_for_matching(entry["track"])
    entry_artist = normalize(entry.get("artist", ""))
    entry_album = normalize(clean_album_name(entry.get("album", ""), entry.get("artist", "")))

    track_name = normalize_track_for_matching(track["name"])
    track_artists = [normalize(a["name"]) for a in track["artists"]]
    album_name = normalize(track["album"]["name"])

    # Track name (strongest signal)
    if track_name == entry_track:
        score += 40
    else:
        score += int(similarity(track_name, entry_track) * 30)

    # Small bias if Spotify track has 'live' but entry does not
    if "live" in normalize(track["name"]) and "live" not in normalize(entry["track"]):
        score += 5

    # Artist match
    if entry_artist:
        artist_matched = False

        for a in track_artists:
            if entry_artist == a:
                score += 40
                artist_matched = True
                break
            # Partial containment (band leader / shortened credit)
            if entry_artist in a or a in entry_artist:
                score += 25
                artist_matched = True
                break

        if not artist_matched and entry_artist:
            score -= 10


    # Album match (optional but useful)
    if entry_album and entry_album in album_name:
        score += 20

    # Live album compatibility
    if (
        "live" in normalize(entry.get("album", "")) and
        "live" in normalize(track["album"]["name"])
    ):
        score += 10

    return score


# =========================
# PARSING
# =========================

import re

KEY_VALUE_PATTERN = re.compile(
    r"(track|artist|album):([^:]+)(?=\s+(?:track|artist|album):|$)",
    re.IGNORECASE,
)

def parse_txt_playlist(txt_path: str):
    """
    Parse cleaned TXT playlist into structured entries.
    Supports BOTH formats:

    1) artist=Genesis | album=The Lamb Lies Down On Broadway | track=In the Cage
    2) track:In the Cage artist:Genesis album:The Lamb Lies Down On Broadway
    """

    entries = []
    seen_keys = set()
    skipped_lines = 0

    with open(txt_path, "r", encoding="utf-8") as f:
        for line_number, raw_line in enumerate(f, start=1):
            line = raw_line.strip()

            if not line or line.startswith("#"):
                continue

            parts = {}

            # Case 1: pipe-separated key=value
            if "|" in line and "=" in line:
                segments = [s.strip() for s in line.split("|")]
                for segment in segments:
                    if "=" not in segment:
                        continue
                    key, value = segment.split("=", 1)
                    parts[key.strip().lower()] = value.strip()

            # Case 2: space-separated key:value (regex-based, supports spaces)
            else:
                for match in KEY_VALUE_PATTERN.finditer(line):
                    key = match.group(1).lower()
                    value = match.group(2).strip()
                    parts[key] = value

            # Require at least track name
            if "track" not in parts:
                skipped_lines += 1
                print(f"⚠ Skipping malformed line {line_number}: {raw_line.strip()}")
                continue

            artist = parts.get("artist", "")
            album = parts.get("album", "")
            track = parts.get("track", "")

            dedup_key = (
                artist.lower(),
                album.lower(),
                track.lower(),
            )

            if dedup_key in seen_keys:
                continue

            seen_keys.add(dedup_key)
            entries.append(parts)

    if skipped_lines:
        print(f"\n⚠ Skipped {skipped_lines} malformed line(s) in TXT file\n")

    return entries

# =========================
# SEARCH SONG
# =========================

def search_track_with_scoring(entry: dict) -> Optional[str]:
    """
    Search Spotify using fallback queries and select the best match via scoring.
    """

    search_queries = []

    # 1) Strict: track + artist
    if entry.get("artist"):
        search_queries.append(
            f'track:"{entry["track"]}" artist:"{entry["artist"]}"'
        )

    # 2) Track only (important for live / remix naming)
    search_queries.append(
        f'track:"{entry["track"]}"'
    )

    # 3) Track + album (important for renamed artists)
    if entry.get("album"):
        search_queries.append(
            f'track:"{entry["track"]}" album:"{entry["album"]}"'
        )

    # 4) Artist only (last resort)
    if entry.get("artist"):
        search_queries.append(
            f'artist:"{entry["artist"]}"'
        )

    best_track = None
    best_score = 0

    for query in search_queries:
        response = spotify_client.search(
            q=query,
            type="track",
            limit=25
        )

        candidates = response.get("tracks", {}).get("items", [])
        if not candidates:
            continue

        for track in candidates:
            score = score_candidate(entry, track)

            # FOR DEBUG !!!!!!!!!
            if DEBUG:
                if score >= 40:
                    print(
                        f"    candidate: {track['name']} — "
                        f"{', '.join(a['name'] for a in track['artists'])} | "
                        f"album={track['album']['name']} | score={score}"
                    )

            # IMMEDIATE ACCEPT: exact, high-confidence match
            if score >= 85:
                return track["uri"]

            if score > best_score:
                best_score = score
                best_track = track

        if best_track:
            if best_score >= 55:
                return best_track["uri"]

            # Accept exact track+artist matches even if score is lower
            if is_exact_track_artist_match(entry, best_track) and best_score >= 50:
                return best_track["uri"]

        return None

    return None


# =========================
# PLAYLIST HELPERS
# =========================

def get_current_user_id() -> str:
    return spotify_client.current_user()["id"]


def find_playlist_by_name(user_id: str, name: str) -> Optional[Dict]:
    offset = 0

    normalized_target = normalize_playlist_name(name)

    while True:
        playlists = spotify_client.current_user_playlists(limit=50, offset=offset)
        for playlist in playlists["items"]:
            normalized_name = normalize_playlist_name(playlist["name"])
            owner_id = playlist["owner"]["id"]

            # Only match playlists owned by you
            if normalized_name == normalized_target and owner_id == user_id:
                return playlist

        if playlists["next"] is None:
            break
        offset += 50

    return None


def get_all_playlist_track_uris(playlist_id: str) -> Set[str]:
    uris = set()
    offset = 0

    while True:
        response = spotify_client.playlist_items(
            playlist_id,
            fields="items(track(uri)),next",
            limit=100,
            offset=offset,
        )

        for item in response["items"]:
            if item["track"] and item["track"]["uri"]:
                uris.add(item["track"]["uri"])

        if response["next"] is None:
            break

        offset += 100

    return uris


def chunked(items: List[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# =========================
# MAIN LOGIC
# =========================

def main():
    parser = argparse.ArgumentParser(description="Upload Spotify playlist from TXT file")
    parser.add_argument("txt_file", help="Clean TXT playlist file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite playlist contents")

    args = parser.parse_args()

    txt_file = args.txt_file
    playlist_name = os.path.splitext(os.path.basename(txt_file))[0]

    print(f"\nPlaylist name: {playlist_name}")

    entries = parse_txt_playlist(txt_file)
    print(f"Entries after deduplication: {len(entries)}")

    print(f"\nValidating {len(entries)} tracks...\n")

    found_uris = []
    not_found = []

    for idx, entry in enumerate(entries, start=1):
        uri = search_track_with_scoring(entry)
        if uri:
            found_uris.append(uri)
            print(f"[{idx}/{len(entries)}] ✔ Found: {entry['track']} — {entry['artist']}")
        else:
            not_found.append(entry)
            print(f"[{idx}/{len(entries)}] ✖ NOT FOUND: {entry}")

    print("\nValidation summary:")
    print(f"  Found: {len(found_uris)}")
    print(f"  Not found: {len(not_found)}")

    if not found_uris:
        print("No tracks found — aborting.")
        sys.exit(1)

    user_id = get_current_user_id()
    playlist = find_playlist_by_name(user_id, playlist_name)

    if playlist is None:
        if DEBUG:
            # DEBUG: playlist creation notice
            print(f"\nPlaylist '{playlist_name}' not found. Creating a new one...")
        playlist = spotify_client.user_playlist_create(
            user=user_id,
            name=playlist_name,
            public=False,
            description="Imported from local playlist",
        )

    playlist_id = playlist["id"]

    existing_uris = get_all_playlist_track_uris(playlist_id)
    existing_uris_list = list(existing_uris)

    if DEBUG:
        # DEBUG: number of existing tracks
        print(f"Existing tracks in playlist: {len(existing_uris_list)}")

    if args.overwrite and existing_uris_list:
        if DEBUG:
            # DEBUG: overwrite mode cleanup
            print("\nMode: OVERWRITE – removing existing tracks")
        for batch in chunked(existing_uris_list, REMOVE_BATCH_SIZE):
            spotify_client.playlist_remove_all_occurrences_of_items(playlist_id, batch)
            if DEBUG:
                # DEBUG: overwrite mode cleanup
                print(f"Removed {len(batch)} tracks")
            time.sleep(0.2)


    # Determine which tracks to add
    if args.overwrite:
        uris_to_add = found_uris
    else:
        # Append only missing tracks
        uris_to_add = [u for u in found_uris if u not in existing_uris]

    print(f"Tracks to add: {len(uris_to_add)}\n")

    for i, batch in enumerate(chunked(uris_to_add, ADD_BATCH_SIZE), start=1):
        spotify_client.playlist_add_items(playlist_id, batch)
        if DEBUG:
            # DEBUG: upload progress
            print(f"✔ Uploaded batch {i} ({len(batch)} tracks)")
        time.sleep(0.2)

    print("\nDone.")


if __name__ == "__main__":
    main()
