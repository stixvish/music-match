"""Shared fixtures for the unit tests.

Audio fixtures are built at test time rather than committed: WAV comes
from the stdlib `wave` module, and the M4A is a 0.2-second silent AAC
file embedded below as base64 (837 bytes) because no stdlib module can
produce an MP4 container. Nothing here needs ffmpeg or a network call.
"""

import base64
import pathlib
import wave

import pytest

# 0.2s of silent mono AAC in an MP4 container. Regenerate with:
#   ffmpeg -f lavfi -i anullsrc=r=8000:cl=mono -t 0.1 -c:a aac -b:a 8k out.m4a
_TINY_M4A_BASE64 = """
AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAAhmcmVlAAAAH21kYXTcAExhdmM2My4x
LjEwMQACMEAOARggBwAAAwJtb292AAAAbG12aGQAAAAAAAAAAAAAAAAAAB9AAAADIAABAAAB
AAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAACAAACLXRyYWsAAABcdGtoZAAAAAMAAAAAAAAAAAAAAAEA
AAAAAAADIAAAAAAAAAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAA
AEAAAAAAAAAAAAAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAyAAAAQAAAEAAAAAAaVt
ZGlhAAAAIG1kaGQAAAAAAAAAAAAAAAAAAB9AAAAHIFXEAAAAAAAtaGRscgAAAAAAAAAAc291
bgAAAAAAAAAAAAAAAFNvdW5kSGFuZGxlcgAAAAFQbWluZgAAABBzbWhkAAAAAAAAAAAAAAAk
ZGluZgAAABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAEUc3RibAAAAGpzdHNkAAAAAAAA
AAEAAABabXA0YQAAAAAAAAABAAAAAAAAAAAAAQAQAAAAAB9AAAAAAAA2ZXNkcwAAAAADgICA
JQABAASAgIAXQBUAAAAAAB9AAAADJwWAgIAFFYhW5QAGgICAAQIAAAAgc3R0cwAAAAAAAAAC
AAAAAQAABAAAAAABAAADIAAAABxzdHNjAAAAAAAAAAEAAAABAAAAAgAAAAEAAAAcc3RzegAA
AAAAAAAAAAAAAgAAABMAAAAEAAAAFHN0Y28AAAAAAAAAAQAAACwAAAAac2dwZAEAAAByb2xs
AAAAAgAAAAH//wAAABxzYmdwAAAAAHJvbGwAAAABAAAAAgAAAAEAAABhdWR0YQAAAFltZXRh
AAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAACxpbHN0AAAAJKl0b28A
AAAcZGF0YQAAAAEAAAAATGF2ZjYzLjEuMTAx
"""


@pytest.fixture(name="m4a_file")
def fixture_m4a_file(tmp_path: pathlib.Path) -> pathlib.Path:
  """Writes a tiny, untagged M4A file.

  Args:
    tmp_path: pytest's per-test temporary directory.

  Returns:
    Path to the file.
  """
  path = tmp_path / "track.m4a"
  path.write_bytes(base64.b64decode(_TINY_M4A_BASE64))
  return path


@pytest.fixture(name="wav_file")
def fixture_wav_file(tmp_path: pathlib.Path) -> pathlib.Path:
  """Writes a tiny, untagged WAV file, which mutagen tags with ID3.

  Args:
    tmp_path: pytest's per-test temporary directory.

  Returns:
    Path to the file.
  """
  path = tmp_path / "track.wav"
  with wave.open(str(path), "wb") as handle:
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(8000)
    handle.writeframes(b"\x00\x00" * 800)
  return path
