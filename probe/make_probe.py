#!/usr/bin/env python3
"""
Format probe: determine which metadata fields Rekordbox 7 and Serato DJ Lite
actually read from FLAC (and AIFF as a control).

Method: every candidate tag key gets a SENTINEL value naming that key, e.g.
the Vorbis key MIXARTIST is written with the value "K-MIXARTIST". Whatever
string appears in the app's Remixer column tells us which key it read.

Usage:  python3 make_probe.py [--out DIR] [--src DIR]
"""
import argparse, os, shutil, subprocess, sys
from pathlib import Path

TRACKS = [
    "David Guetta, Bebe Rexha, Brooks - I'm Good (Blue) - Brooks Remix.m4a",
    "Khalid, HoneyLuv - in plain sight (HoneyLuv Remix).m4a",
    "David Guetta - I'm That Bitch (feat. Saweetie).m4a",
    "Lil Uzi Vert - I Gotta.m4a",
    "Calvin Harris - Open Wide (feat. Big Sean).m4a",
]

# Vorbis (FLAC): logical field -> candidate keys. Each gets value "K-<KEY>".
VORBIS_CANDIDATES = {
    "title":         ["TITLE"],
    "artist":        ["ARTIST"],
    "album":         ["ALBUM"],
    "albumartist":   ["ALBUMARTIST", "ALBUM ARTIST", "ALBUM_ARTIST"],
    "date":          ["DATE", "YEAR", "ORIGINALDATE", "RELEASEDATE"],
    "tracknumber":   ["TRACKNUMBER"],
    "discnumber":    ["DISCNUMBER"],
    "genre":         ["GENRE"],
    "composer":      ["COMPOSER"],
    "lyricist":      ["LYRICIST"],
    "remixer":       ["REMIXER", "MIXARTIST"],
    "mixname":       ["MIXNAME", "SUBTITLE", "VERSION", "SETSUBTITLE"],
    "label":         ["LABEL", "ORGANIZATION", "PUBLISHER", "LABELNO"],
    "originalartist":["ORIGINALARTIST", "PERFORMER", "ORIGARTIST"],
    "key":           ["INITIALKEY", "KEY"],
    "bpm":           ["BPM", "TEMPO"],
    "isrc":          ["ISRC"],
    "comment":       ["COMMENT", "DESCRIPTION"],
    "catalog":       ["CATALOGNUMBER"],
    "grouping":      ["GROUPING", "CONTENTGROUP"],
}
# Typed fields can't hold "K-NAME" strings, so each candidate key gets a
# DISTINCT valid value; whichever value the app displays names the key.
TYPED = {
    "DATE": "2001-01-01", "YEAR": "2002", "ORIGINALDATE": "2003-03-03",
    "RELEASEDATE": "2004-04-04",
    "BPM": "121", "TEMPO": "122",
    "INITIALKEY": "1A", "KEY": "2A",
    "TRACKNUMBER": "7", "DISCNUMBER": "1",
}
ID3_TYPED = {"TDRC": "2001-01-01", "TDRL": "2004-04-04", "TBPM": "121",
             "TKEY": "1A", "TRCK": "7", "TPOS": "1"}

# ID3v2.4 (AIFF control): logical field -> frame id
ID3_FRAMES = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
    "date": "TDRC", "tracknumber": "TRCK", "discnumber": "TPOS", "genre": "TCON",
    "composer": "TCOM", "lyricist": "TEXT", "remixer": "TPE4", "mixname": "TIT3",
    "label": "TPUB", "originalartist": "TOPE", "key": "TKEY", "bpm": "TBPM",
    "isrc": "TSRC", "grouping": "TIT1", "releasedate": "TDRL",
}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ! {' '.join(cmd[:3])}... failed:\n{r.stderr[-400:]}", file=sys.stderr)
        return False
    return True


def extract_art(src, dest):
    return run(["ffmpeg", "-y", "-v", "quiet", "-i", str(src), "-an",
                "-vcodec", "copy", str(dest)]) and dest.exists()


def transcode(src, dest, fmt):
    codec = (["-c:a", "flac", "-sample_fmt", "s16", "-compression_level", "8"]
             if fmt == "flac" else ["-c:a", "pcm_s16be"])
    return run(["ffmpeg", "-y", "-v", "quiet", "-i", str(src), "-vn", "-map_metadata", "-1",
                *codec, str(dest)])


def _jpeg_size(data):
    """Minimal JPEG SOF parser -> (width, height); (0,0) if unparseable."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return (int.from_bytes(data[i + 7:i + 9], "big"),
                    int.from_bytes(data[i + 5:i + 7], "big"))
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return (0, 0)


def tag_flac(path, art):
    from mutagen.flac import FLAC, Picture
    f = FLAC(str(path))
    f.delete()
    f.clear_pictures()
    for field, keys in VORBIS_CANDIDATES.items():
        for k in keys:
            f[k] = TYPED.get(k, f"K-{k}")
    if art and art.exists():
        p = Picture()
        p.type, p.mime, p.data = 3, "image/jpeg", art.read_bytes()
        p.width, p.height, p.depth = _jpeg_size(p.data) + (24,)
        f.add_picture(p)
    f.save()


def tag_aiff(path, art):
    from mutagen.aiff import AIFF
    from mutagen.id3 import ID3, APIC, COMM, TXXX, Frames
    a = AIFF(str(path))
    try:
        a.add_tags()
    except Exception:
        pass
    a.tags.delete(str(path))
    for field, frame_id in ID3_FRAMES.items():
        cls = Frames.get(frame_id)
        if not cls:
            continue
        a.tags.add(cls(encoding=3, text=[ID3_TYPED.get(frame_id, f"F-{frame_id}")]))
    # Comment: COMM is the standard frame; TXXX:COMMENT is a common alternate.
    # Distinct values so the displayed string names the frame that was read.
    a.tags.add(COMM(encoding=3, lang="eng", desc="", text=["F-COMM"]))
    a.tags.add(TXXX(encoding=3, desc="COMMENT", text=["F-TXXX-COMMENT"]))
    if art and art.exists():
        a.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover",
                        data=art.read_bytes()))
    a.save(v2_version=4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.expanduser("~/Music/yt-dlp"))
    ap.add_argument("--out", default=os.path.expanduser("~/Music/_probe"))
    args = ap.parse_args()

    src_dir, out = Path(args.src), Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    flac_dir, aiff_dir, tmp = out / "FLAC", out / "AIFF_control", out / ".tmp"
    for d in (flac_dir, aiff_dir, tmp):
        d.mkdir(parents=True, exist_ok=True)

    made = 0
    for i, name in enumerate(TRACKS, 1):
        src = src_dir / name
        if not src.exists():
            print(f"  skip (missing): {name}")
            continue
        stem = f"{i:02d}_PROBE"
        art = tmp / f"{stem}.jpg"
        if not extract_art(src, art):
            art = None

        fl = flac_dir / f"{stem}.flac"
        if transcode(src, fl, "flac"):
            tag_flac(fl, art)
            made += 1
        ai = aiff_dir / f"{stem}.aiff"
        if transcode(src, ai, "aiff"):
            tag_aiff(ai, art)
        print(f"  [{i}] {name[:58]}")

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nBuilt {made} FLAC + AIFF control files in {out}")
    print("\nSentinel scheme:")
    print("  FLAC : value 'K-<VORBISKEY>'  e.g. Remixer showing 'K-MIXARTIST'")
    print("         means Rekordbox read the MIXARTIST key, not REMIXER.")
    print("  AIFF : value 'F-<ID3FRAME>'   e.g. 'F-TPE4'")
    print("\nCandidate keys under test:")
    for field, keys in VORBIS_CANDIDATES.items():
        if len(keys) > 1:
            print(f"  {field:<15} {', '.join(keys)}")


if __name__ == "__main__":
    main()
