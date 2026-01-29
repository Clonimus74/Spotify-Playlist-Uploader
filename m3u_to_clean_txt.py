import sys
import os
import re

r"""
Parse an M3U playlist containing local file paths and produce
a TXT file with Spotify search queries.

Supported layouts:

1) Multiple albums per artist:
   V:\Music\Artist\Artist - (Year) Album [Format]\01 - Track.ext

2) Single album artist:
   V:\Music\Artist - (Year) Album [Format]\01 - Track.ext
"""

def album_folder_contains_artist(folder_name: str) -> bool:
    """
    True if folder starts with:
    Artist - (YYYY) Album
    """
    return bool(re.match(r".+?\s*-\s*\(\d{4}\)", folder_name))


def extract_artist_and_album_from_album_folder(folder_name: str):
    """
    'Al Green - (1972) Let's Stay Together [FLAC]'
    -> ('Al Green', "Let's Stay Together")
    """

    # Remove [FLAC], [MP3], etc.
    cleaned = re.sub(r"\[.*?\]", "", folder_name).strip()

    match = re.match(r"(.+?)\s*-\s*\(\d{4}\)\s*(.+)", cleaned)
    if not match:
        return None, cleaned

    artist = match.group(1).strip()
    album = match.group(2).strip()
    return artist, album


def extract_track_name(filename: str) -> str:
    """
    '01 - Let Me Drown.flac' -> 'Let Me Drown'
    """
    name = os.path.splitext(filename)[0]
    name = re.sub(r"^\d+\s*-\s*", "", name)
    return name.strip()


def clean_album_folder_name(folder_name: str, artist_name: str) -> str:
    """
    'Soundgarden - (1994) Superunknown [FLAC]'
    -> 'Superunknown'
    """
    # Remove year and format
    folder_name = re.sub(r"\(\d{4}\)", "", folder_name)
    folder_name = re.sub(r"\[.*?\]", "", folder_name)
    folder_name = folder_name.strip(" -_")

    # Remove leading 'Artist - ' if present
    artist_prefix = artist_name + " - "
    if folder_name.startswith(artist_prefix):
        folder_name = folder_name[len(artist_prefix):]

    return folder_name.strip()


def normalize_artist_name(artist: str) -> str:
    """
    Convert library-style names to display-style names.

    'Rolling Stones, The' -> 'The Rolling Stones'
    """
    artist = artist.strip()
    match = re.match(r"(.+),\s*the$", artist, re.IGNORECASE)
    if match:
        return "The " + match.group(1)
    return artist


def normalize_track_title(title: str) -> str:
    """
    Remove suffixes Spotify may encode differently.
    """
    title = title.strip()
    title = re.sub(
        r"\s*-\s*(live|remaster(ed)?|mono|stereo).*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return title.strip()


def split_artist_from_track(track_name: str):
    """
    'Barry Bostwick - Dammit Janet'
    -> ('Barry Bostwick', 'Dammit Janet')
    """
    if " - " not in track_name:
        return None, track_name

    artist, title = track_name.split(" - ", 1)
    return artist.strip(), title.strip()


def parse_m3u(m3u_path: str) -> list[str]:
    queries = []
    GENERIC_ARTISTS = {"va", "various", "various artists"}

    with open(m3u_path, "r", encoding="utf-8", errors="ignore") as file:
        for line in file:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            parts = os.path.normpath(line).split(os.sep)

            track_filename = parts[-1]
            album_folder = parts[-2]

            track_name = extract_track_name(track_filename)
            track_name = normalize_track_title(track_name)

            # -------- determine artist & album --------
            if album_folder_contains_artist(album_folder):
                # Single-album artist layout
                artist_name, album_name = extract_artist_and_album_from_album_folder(album_folder)
            else:
                # Multi-album artist layout
                artist_name = parts[-3]
                album_name = clean_album_folder_name(album_folder, artist_name)

            if artist_name.lower() in {"music", "unknown"}:
                raise ValueError(f"Invalid artist detected from path: {line}")
                
            artist_name = normalize_artist_name(artist_name)
            
            if artist_name.lower() in GENERIC_ARTISTS:
                artist_name = ""

            # Handle VA albums where track name embeds artist
            if not artist_name:
                extracted_artist, clean_track = split_artist_from_track(track_name)
                if extracted_artist:
                    artist_name = extracted_artist
                    track_name = clean_track

            query = (
                f"track:{track_name} "
                f"artist:{artist_name} "
                f"album:{album_name}"
            )

            queries.append(query)

    return queries


def main():
    if len(sys.argv) != 2:
        print("Usage: python m3u_to_clean_txt.py <playlist.m3u>")
        sys.exit(1)

    m3u_file = sys.argv[1]
    playlist_name = os.path.splitext(os.path.basename(m3u_file))[0]
    output_file = f"{playlist_name}.txt"

    queries = parse_m3u(m3u_file)

    with open(output_file, "w", encoding="utf-8") as out:
        for q in queries:
            out.write(q + "\n")

    print(f"Created: {output_file}")
    print(f"Total tracks: {len(queries)}")


if __name__ == "__main__":
    main()
