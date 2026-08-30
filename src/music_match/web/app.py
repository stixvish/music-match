"""The NiceGUI review interface.

Two pages, in the order the build order asks for them: the review queue,
which is the reason this exists, and link submission.

The queue holds the three things the pipeline cannot decide on its own —
a match the matcher was unsure of, a track nothing matched, and a file
held back as a possible video rip. Each needs a different answer, so each
gets different buttons rather than a single "approve".
"""

from typing import Any, Callable

from nicegui import ui

from music_match import intake as intake_lib
from music_match.tagging.fields import ALL_FIELDS
from music_match.tagging.fields import NUMERIC_FIELDS
from music_match.tagging.fields import TrackTags
from music_match.web import state as web_state

# Fields worth offering as editable inputs. The internal bookkeeping one
# is not a person's business to change.
EDITABLE_FIELDS = tuple(
    field for field in ALL_FIELDS if field != "source_video_id")

STATUS_HELP = {
    "review": "matched, but not confidently enough to write without a look",
    "no_match": "no source returned anything usable",
    "quarantined": "held back as a possible rip from a music video",
}


def build(settings: web_state.Settings) -> None:
  """Registers the application's pages.

  Args:
    settings: Where the running app reads and writes state.
  """

  @ui.page("/")
  def review_page() -> None:
    """The review queue."""
    _render_header(active="review")
    _render_queue(settings)

  @ui.page("/intake")
  def intake_page() -> None:
    """Link submission."""
    _render_header(active="intake")
    _render_intake(settings)


def _render_header(active: str) -> None:
  """Draws the navigation shared by every page.

  Args:
    active: Which page is being shown.
  """
  with ui.header().classes("items-center justify-between"):
    ui.label("music-match").classes("text-lg font-bold")
    with ui.row():
      for label, target, name in (("Review queue", "/", "review"),
                                  ("Add links", "/intake", "intake")):
        button = ui.button(label, on_click=lambda t=target: ui.navigate.to(t))
        if name == active:
          button.props("color=primary")
        else:
          button.props("flat color=white")


def _render_queue(settings: web_state.Settings) -> None:
  """Draws the review queue.

  Args:
    settings: Where to read and write state.
  """
  container = ui.column().classes("w-full gap-4 p-4")

  def refresh() -> None:
    """Reloads the queue from the database."""
    container.clear()
    with container:
      totals = web_state.counts(settings)
      with ui.row().classes("items-center gap-4"):
        for status, count in totals.items():
          ui.badge(f"{status}: {count}").props("outline")
      items = web_state.load_queue(settings)
      if not items:
        ui.label("Nothing waiting. Run `music-match match run` to fill this.")
        return
      for item in items:
        _render_item(settings, item, refresh)

  refresh()


def _render_item(settings: web_state.Settings, item: web_state.ReviewItem,
                 refresh: Callable[[], None]) -> None:
  """Draws one track awaiting a decision.

  Args:
    settings: Where to read and write state.
    item: The track.
    refresh: Called after an action, to reload the queue.
  """
  with ui.card().classes("w-full").mark(f"track-{item.track_id}"):
    with ui.row().classes("items-center justify-between w-full"):
      ui.label(item.name()).classes("text-base font-medium")
      with ui.row().classes("items-center gap-2"):
        if item.confidence is not None:
          ui.badge(f"{item.confidence:.2f}").props("color=primary")
        ui.badge(item.status).props("outline")
    ui.label(STATUS_HELP.get(item.status, "")).classes("text-xs text-grey-7")

    if item.status == "quarantined":
      _render_quarantine_actions(settings, item, refresh)
      return

    changes = item.differences()
    if changes:
      rows = [{
          "field": field,
          "current": before,
          "proposed": after
      } for field, before, after in changes]
      ui.table(columns=[{
          "name": name,
          "label": name,
          "field": name,
          "align": "left"
      } for name in ("field", "current", "proposed")],
               rows=rows).classes("w-full")
    else:
      ui.label("no proposed changes").classes("text-xs text-grey-7")

    inputs = _render_editor(item)
    if item.art_url:
      ui.image(item.art_url).classes("w-32 h-32 object-cover")
    _render_match_actions(settings, item, inputs, refresh)


def _render_editor(item: web_state.ReviewItem) -> dict[str, Any]:
  """Draws editable inputs for the proposed tags.

  ARCHITECTURE asks for direct metadata editing, so the proposal is a
  starting point rather than a take-it-or-leave-it.

  Args:
    item: The track being reviewed.

  Returns:
    Field name to its input element.
  """
  proposed = item.proposed.as_dict(include_empty=True)
  inputs: dict[str, Any] = {}
  with ui.expansion("Edit fields").classes("w-full"):
    with ui.grid(columns=3).classes("w-full gap-2"):
      for field in EDITABLE_FIELDS:
        value = proposed.get(field)
        inputs[field] = ui.input(
            label=field,
            value="" if value is None else str(value)).props("dense outlined")
  return inputs


def _render_match_actions(settings: web_state.Settings,
                          item: web_state.ReviewItem, inputs: dict[str, Any],
                          refresh: Callable[[], None]) -> None:
  """Draws the buttons for a track with a proposal.

  Args:
    settings: Where to read and write state.
    item: The track.
    inputs: The editable field inputs.
    refresh: Called after an action.
  """
  with ui.row().classes("gap-2"):

    def on_accept() -> None:
      """Writes the edited proposal to the file."""
      message = web_state.accept(settings, item.track_id,
                                 tags_from_inputs(inputs), item.art_url)
      ui.notify(message)
      refresh()

    def on_reject() -> None:
      """Marks the track as never going to match."""
      ui.notify(web_state.reject(settings, item.track_id, "rejected in review"))
      refresh()

    ui.button("Accept and write", on_click=on_accept).props("color=primary")
    ui.button("Won't match", on_click=on_reject).props("flat color=negative")


def _render_quarantine_actions(settings: web_state.Settings,
                               item: web_state.ReviewItem,
                               refresh: Callable[[], None]) -> None:
  """Draws the buttons for a quarantined file.

  A held file needs a different question from an uncertain match: is this
  actually a video rip, or was the filename misleading?

  Args:
    settings: Where to read and write state.
    item: The track.
    refresh: Called after an action.
  """
  with ui.row().classes("gap-2"):

    def on_release() -> None:
      """Returns the track to the pipeline."""
      ui.notify(web_state.release(settings, item.track_id))
      refresh()

    ui.button("Audio is fine, release it",
              on_click=on_release).props("color=primary")
    ui.label("Leave it here to keep it out of matching.").classes(
        "text-xs text-grey-7 self-center")


def tags_from_inputs(inputs: dict[str, Any]) -> TrackTags:
  """Reads edited values back out of the form.

  Blank inputs become None rather than empty strings, so clearing a field
  in the form means "no opinion" and leaves the file's value alone —
  the same meaning the field has everywhere else in this tool.

  Args:
    inputs: Field name to its input element.

  Returns:
    The edited tags.
  """
  values: dict[str, Any] = {}
  for field, element in inputs.items():
    raw = str(getattr(element, "value", "") or "").strip()
    if raw:
      values[field] = raw
  return TrackTags.from_mapping(_coerce_numbers(values))


def _coerce_numbers(values: dict[str, Any]) -> dict[str, Any]:
  """Converts numeric fields from form text to integers.

  Args:
    values: Field name to its raw string value.

  Returns:
    The same mapping with numeric fields parsed, dropping any that are
    not numbers rather than writing nonsense into a file.
  """
  coerced = {}
  for field, value in values.items():
    if field in NUMERIC_FIELDS:
      try:
        coerced[field] = int(str(value))
      except ValueError:
        continue
    else:
      coerced[field] = value
  return coerced


def _render_intake(settings: web_state.Settings) -> None:
  """Draws the link submission page.

  Args:
    settings: Where to read and write state.
  """
  with ui.column().classes("w-full gap-4 p-4"):
    ui.label("Paste links — tracks, albums or playlists, one per line.")
    box = ui.textarea(placeholder="https://...").props(
        "outlined rows=8").classes("w-full").mark("links")
    status = ui.label("").classes("text-sm")

    def on_check() -> None:
      """Expands the links and reports what would be downloaded."""
      status.set_text(_describe_submission(settings, str(box.value or "")))

    ui.button("Check links", on_click=on_check).props("color=primary")
    ui.label("Downloading runs from the command line: `music-match intake"
             " --from-file links.txt`. It asks about possible duplicates, which"
             " needs a terminal.").classes("text-xs text-grey-7")


def _describe_submission(settings: web_state.Settings, text: str) -> str:
  """Expands submitted links and says what is new.

  Args:
    settings: Where to read state.
    text: The submitted text.

  Returns:
    A sentence describing what was found.
  """
  links = intake_lib.parse_links(text)
  if not links:
    return "No links found."
  try:
    entries = intake_lib.expand(links)
  except intake_lib.IntakeError as err:
    return f"Could not read those links: {err}"

  conn = settings.open_db()
  try:
    fresh = [
        entry for entry in entries if not intake_lib.in_archive(conn, entry)
    ]
  finally:
    conn.close()
  return (f"{len(links)} link(s) expanded to {len(entries)} track(s);"
          f" {len(entries) - len(fresh)} already downloaded,"
          f" {len(fresh)} new.")


def run(settings: web_state.Settings,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        show: bool = True,
        reload: bool = False) -> None:
  """Starts the web interface.

  Args:
    settings: Where the app reads and writes state.
    host: Address to bind.
    port: Port to listen on.
    show: Whether to open a browser.
    reload: Whether to watch for code changes.
  """
  build(settings)
  ui.run(host=host,
         port=port,
         show=show,
         reload=reload,
         title="music-match",
         favicon="🎧")
