"""Local web app: the only human interface (SPEC.md §15).

Review queue, metadata editor, provenance and elicitation in one surface. It
exists because **auto-accept will be wrong sometimes** — §9 projects ~25-35
tracks landing in the library with a wrong field and no flag on them, and a
confident wrong match is precisely the case that never reaches review. The
remedy is making correction cheap on *any* track, not only the doubted ones.

Localhost only, no auth: it reads and writes the user's own library.
"""

from music.web.app import create_app

__all__ = ["create_app"]
