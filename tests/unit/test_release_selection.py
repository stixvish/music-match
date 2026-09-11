"""Release selection and dating (tasks/todo.md t9, SPEC.md §7)."""

from music.sources.base import ReleaseInfo
from music.sources.musicbrainz import earliest_date, select_release


def rel(title, tracks, date, primary="Album", secondary=(), num=1, aa="Pitbull"):
  return ReleaseInfo(
    release_id=title,
    title=title,
    track_count=tracks,
    track_number=num,
    album_artist=aa,
    date=date,
    primary_type=primary,
    secondary_types=secondary,
  )


def test_various_artists_compilation_loses_even_when_typed_as_album():
  """A various-artists compilation must not win on track count.

  Real failure: MusicBrainz types many compilations as plain Album with no
  Compilation secondary type, so they beat the real album (SPEC.md §7).
  """
  chosen = select_release(
    [
      rel("Planet Pit", 12, "2011-06-17", aa="Pitbull"),
      rel("Hit Mania 2012", 24, "2011-11-29", aa="Various Artists"),
      rel("Berlin Tag & Nacht #7", 40, "2014", aa="Various Artists"),
    ],
    artist="Pitbull",
  )
  assert chosen.title == "Planet Pit"


def test_deluxe_beats_standard():
  chosen = select_release(
    [rel("Planet Pit", 12, "2011-06-17"), rel("Planet Pit (Deluxe)", 16, "2011-06-17")],
    artist="Pitbull",
  )
  assert chosen.title == "Planet Pit (Deluxe)"


def test_compilation_does_not_win_on_size():
  """A 40-track greatest hits must not beat the real album (SPEC.md §7)."""
  chosen = select_release(
    [
      rel("Planet Pit", 12, "2011-06-17"),
      rel("Greatest Hits", 40, "2015-01-01", secondary=("Compilation",)),
    ]
  )
  assert chosen.title == "Planet Pit"


def test_live_album_is_excluded():
  chosen = select_release(
    [rel("Studio", 10, "2010"), rel("Live at X", 30, "2012", secondary=("Live",))]
  )
  assert chosen.title == "Studio"


def test_keyword_only_breaks_a_tie():
  chosen = select_release([rel("Album (Deluxe)", 12, "2011"), rel("Album", 14, "2011")])
  # track count wins; keyword is a tie-break only
  assert chosen.title == "Album"


def test_falls_back_when_no_plain_album_exists():
  chosen = select_release([rel("Only A Comp", 20, "2011", secondary=("Compilation",))])
  assert chosen.title == "Only A Comp"


def test_no_releases():
  assert select_release([]) is None


# --- dating ---------------------------------------------------------------


def test_single_predates_its_album():
  """The whole point: most singles precede the album (SPEC.md §7)."""
  releases = [
    rel("Give Me Everything", 1, "2011-03-18", primary="Single"),
    rel("Planet Pit", 12, "2011-06-17"),
    rel("Planet Pit (Deluxe)", 16, "2011-06-17"),
  ]
  assert earliest_date(releases) == "2011-03-18"
  # and the album fields still come from the deluxe
  assert select_release(releases).title == "Planet Pit (Deluxe)"


def test_deluxe_reissue_does_not_move_the_date():
  releases = [rel("Album", 12, "2011-06-17"), rel("Album (Deluxe)", 18, "2012-11-01")]
  assert earliest_date(releases) == "2011-06-17"
  assert select_release(releases).title == "Album (Deluxe)"


def test_implausible_dates_are_ignored():
  assert earliest_date([rel("Bad", 1, "1000-01-01"), rel("Ok", 1, "2011-06-17")]) == (
    "2011-06-17"
  )


def test_future_dates_are_ignored():
  assert earliest_date([rel("Future", 1, "2999-01-01"), rel("Ok", 1, "2011")]) == "2011"


def test_year_only_dates_are_usable():
  assert earliest_date([rel("A", 1, "2011")]) == "2011"


def test_full_precision_preferred_over_bare_year():
  assert earliest_date([rel("A", 1, "2011"), rel("B", 1, "2011-01-01")]) == "2011-01-01"


def test_no_usable_dates():
  assert earliest_date([rel("A", 1, "")]) == ""
