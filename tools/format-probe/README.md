# format probe

reproducible evidence for `spec.md` §10 — which metadata fields rekordbox 7 and
serato dj lite actually read, per container format.

## why it still exists

the probe is done (m0 is closed), but the spec makes strong claims — "rekordbox
drops album artist from flac", "lyricist round-trips via `TEXT`" — and a spec
that asserts a measurement should ship the thing that measured it. it is also
the fastest way to re-answer the same question when the ground moves:
serato pro, a new rekordbox release, a cdj you have not met, or adding flac
back for a space-constrained usb.

## how it works

every candidate tag key is written with a **sentinel value naming itself**, so
the value displayed in the app identifies the key that was read:

```
vorbis MIXARTIST = "K-MIXARTIST"      id3 TPE4 = "F-TPE4"
vorbis REMIXER   = "K-REMIXER"
```

if rekordbox's remixer column shows `K-MIXARTIST`, it read `MIXARTIST`. if it
shows `K-REMIXER`, it read `REMIXER`. if blank, neither works.

typed fields cannot hold sentinel strings, so each candidate gets a **distinct
valid value** instead — a displayed year of `2002` means `YEAR` was read,
`2003` means `ORIGINALDATE`.

## usage

```bash
python3 make_probe.py [--src DIR] [--out DIR]
```

writes flac + aiff control files, then import them into both apps and read off
which sentinels appear. delete the output when finished; it is large.

## findings it produced

- aiff/id3v2.4 carries every required field; flac silently drops five
- `-sample_fmt s16` is mandatory — ffmpeg defaults to 24-bit decoding aac,
  which made flac *larger* than aiff
- rekordbox caches tags per path and needs **reload tag** to see changes
