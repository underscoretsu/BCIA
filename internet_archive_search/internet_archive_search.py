#!/usr/bin/env python3
"""Internet Archive search app with a Textual TUI by default."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, DirectoryTree, Footer, Header, Input, RichLog, Select, SelectionList, Static
from textual.screen import ModalScreen

API_URL = "https://archive.org/advancedsearch.php"
FIELDS = [
    "identifier",
    "title",
    "creator",
    "date",
    "mediatype",
    "downloads",
    "description",
]


def build_query(user_query: str, mediatype: str | None) -> str:
    # Search across default fields; optionally restrict by media type.
    query = user_query.strip()
    if mediatype:
        query = f"({query}) AND mediatype:{mediatype}"
    return query


def search_archive(query: str, rows: int, page: int) -> dict[str, Any]:
    params: list[tuple[str, str]] = [("q", query), ("rows", str(rows)), ("page", str(page)), ("output", "json")]
    params.extend(("fl[]", field) for field in FIELDS)

    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "python-internet-archive-search/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Internet Archive returned HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Internet Archive: {exc.reason}") from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Internet Archive did not return valid JSON") from exc


def fetch_item_metadata(identifier: str) -> dict[str, Any]:
    url = f"https://archive.org/metadata/{urllib.parse.quote(identifier)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "python-internet-archive-search/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Internet Archive returned HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Internet Archive: {exc.reason}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Internet Archive did not return valid JSON") from exc


def download_item_files(
    identifier: str,
    data: dict[str, Any],
    destination: Path,
    progress: Any = None,
) -> Path:
    files = data.get("files", []) if isinstance(data, dict) else []
    if not files:
        raise RuntimeError("This item has no downloadable files.")

    destination.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []

    for index, item in enumerate(files, start=1):
        name = item.get("name")
        if not name or not isinstance(name, str):
            continue

        parts = [part for part in PurePosixPath(name).parts if part not in ("", ".", "..", "/")]
        if not parts:
            continue
        target = destination.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)

        url = f"https://archive.org/download/{urllib.parse.quote(identifier)}/{urllib.parse.quote(name)}"
        if progress:
            progress(f"Downloading {index}/{len(files)}: {name}")

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "python-internet-archive-search/1.0"})
                with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as out:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        out.write(chunk)
                last_error = None
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                target.unlink(missing_ok=True)
                if attempt < 3:
                    if progress:
                        progress(f"Retry {attempt}/3 for {name} due to error: {exc}")
                    time.sleep(1)

        if last_error is not None:
            failures.append(f"{name}: {last_error}")
            if progress:
                progress(f"Skipped {name} after 3 failed attempts: {last_error}")

    if failures and progress:
        progress(f"Finished with {len(failures)} failed file(s):")
        for failure in failures[:20]:
            progress(f"- {failure}")
        if len(failures) > 20:
            progress(f"- ... and {len(failures) - 20} more")

    return destination


def first(value: Any, default: str = "N/A") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        if not value:
            return default
        return str(value[0])
    return str(value)


def print_results(data: dict[str, Any], base_url_rows: int, start: int = 1) -> None:
    response = data.get("response", {})
    docs = response.get("docs", [])
    total = response.get("numFound", 0)

    print(f"\nFound {total:,} result(s). Showing page results ({len(docs)} item(s), up to {base_url_rows} requested).\n")

    if not docs:
        print("No matches found.")
        return

    for index, item in enumerate(docs, start=start):
        identifier = first(item.get("identifier"))
        title = first(item.get("title"), "Untitled")
        creator = first(item.get("creator"))
        date = first(item.get("date"))
        mediatype = first(item.get("mediatype"))
        downloads = item.get("downloads", "N/A")
        description = first(item.get("description"), "No description available.")

        print(f"{index}. {title}")
        print(f"   Creator: {creator}")
        print(f"   Date: {date} | Type: {mediatype} | Downloads: {downloads}")
        print(f"   URL: https://archive.org/details/{identifier}")
        wrapped = textwrap.fill(description, width=100, initial_indent="   Description: ", subsequent_indent="   ")
        print(wrapped if len(description) <= 300 else wrapped + "...")
        print()


class ChooseDirectoryScreen(ModalScreen):
    CSS = """
    ChooseDirectoryScreen {
        align: center middle;
    }

    #dir-dialog {
        width: 80%;
        height: 80%;
        border: round white;
        background: $panel;
        padding: 1;
    }

    #dir-tree {
        height: 1fr;
        border: solid gray;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, current_path: Path):
        super().__init__()
        self.current_path = current_path

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Choose a download folder", id="dir-title"),
            DirectoryTree(self.current_path if self.current_path.exists() else Path.home(), id="dir-tree"),
            Input(value=str(self.current_path), placeholder="/path/to/download/folder", id="dir-input"),
            Horizontal(
                Button("Choose", id="dir-choose", variant="primary"),
                Button("Cancel", id="dir-cancel"),
            ),
            id="dir-dialog",
        )

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#dir-input", Input).value = str(event.path)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.choose()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dir-choose":
            self.choose()
        elif event.button.id == "dir-cancel":
            self.dismiss(None)

    def choose(self) -> None:
        path = Path(self.query_one("#dir-input", Input).value.strip()).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.query_one("#dir-input", Input).value = f"Error: {exc}"
            return
        self.dismiss(path)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RangeSelectionList(SelectionList):
    """SelectionList that supports shift-click range selection."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.anchor_index: int | None = None

    async def _on_click(self, event) -> None:
        clicked_option = event.style.meta.get("option")

        if event.shift and clicked_option is not None and self.anchor_index is not None:
            self.highlighted = clicked_option
            start, end = sorted((self.anchor_index, clicked_option))
            for index in range(start, end + 1):
                option = self.get_option_at_index(index)
                if option is not None:
                    self.select(option.value)
            event.stop()
            return

        if clicked_option is not None:
            self.highlighted = clicked_option
            option = self.get_option_at_index(clicked_option)
            if option is not None:
                self.toggle(option.value)
            self.anchor_index = clicked_option
            event.stop()
            return

        await super()._on_click(event)


class ArchiveSearchApp(App):
    CSS = """
    Screen {
        layout: vertical;
        background: #050505;
    }

    #controls {
        height: auto;
        dock: top;
        padding: 1;
        background: #000000;
    }

    #query {
        width: 3fr;
    }

    Select {
        width: 14;
    }

    Button {
        width: auto;
    }

    #results {
        height: 1fr;
        background: #000000;
    }

    #status {
        height: auto;
        padding: 0 1;
    }

    #details-view {
        height: 1fr;
        padding: 1;
        display: none;
    }

    #details-title {
        height: auto;
        text-style: bold;
        margin-bottom: 1;
    }

    #details-log {
        height: 1fr;
        border: solid gray;
    }

    #choose-download-folder {
        margin-top: 1;
    }

    #details-meta {
        height: 8;
    }

    #files-select {
        height: 3fr;
        border: solid gray;
    }

    #download-status {
        height: auto;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("n", "next_page", "Next page"),
        ("p", "previous_page", "Previous page"),
        ("f", "focus_search", "Focus search"),
        ("o", "open_selected", "Open details"),
        ("w", "cursor_up", "Move up"),
        ("s", "cursor_down", "Move down"),
        ("a", "tab_previous", "Previous tab"),
        ("d", "tab_next", "Next tab"),
        ("ctrl+d", "download_details", "Download"),
        ("backspace", "back_to_results", "Back"),
    ]

    def __init__(self, query: str = "", rows: int = 10, page: int = 1, mediatype: str | None = None):
        super().__init__()
        self.query = query.strip()
        self.rows = rows if rows in {5, 10, 20, 50, 100} else 10
        self.page = page
        self.mediatype = mediatype
        self.current_identifiers: list[str] = []
        self.details_open = False
        self.last_status = "Enter a query and press Search."
        self.current_identifier: str | None = None
        self.current_detail_data: dict[str, Any] | None = None
        self.download_dir = Path.home() / "Downloads"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(
            Input(value=self.query, placeholder="Search Internet Archive...", id="query"),
            Select(
                options=[("5", "5"), ("10", "10"), ("20", "20"), ("50", "50"), ("100", "100")],
                value=str(self.rows) if self.rows in {5, 10, 20, 50, 100} else "10",
                id="rows",
            ),
            Select(
                options=[
                    ("Any type", "any"),
                    ("Texts", "texts"),
                    ("Movies", "movies"),
                    ("Audio", "audio"),
                    ("Image", "image"),
                    ("Software", "software"),
                    ("Collection", "collection"),
                    ("Web", "web"),
                ],
                value=self.mediatype or "any",
                id="mediatype",
            ),
            Button("Search", id="search", variant="primary"),
            Button("Prev", id="prev"),
            Button("Next", id="next"),
            id="controls",
        )
        yield DataTable(id="results")
        yield Static("Enter a query and press Search.", id="status")
        yield Vertical(
            Static("", id="details-title"),
            Static("", id="details-meta"),
            Static("Files (click to toggle selected files):", id="files-label"),
            RangeSelectionList(id="files-select"),
            Button("Download selected", id="download-details", variant="primary"),
            Button("Choose download folder", id="choose-download-folder"),
            Static("", id="download-status"),
            id="details-view",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("#", "Title", "Creator", "Date", "Type", "Downloads", "URL")
        table.cursor_type = "row"
        self.query_one("#query", Input).focus()
        if self.query:
            self.run_search()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query = event.value.strip()
        self.page = 1
        self.run_search()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "rows":
            self.rows = int(event.value)
            self.page = 1
        elif event.select.id == "mediatype":
            self.mediatype = None if event.value == "any" else str(event.value)
            self.page = 1
        if self.query:
            self.run_search()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "search":
            self.query = self.query_one("#query", Input).value.strip()
            self.page = 1
            self.run_search()
        elif event.button.id == "prev":
            self.action_previous_page()
        elif event.button.id == "next":
            self.action_next_page()
        elif event.button.id == "download-details":
            self.download_current_item()
        elif event.button.id == "choose-download-folder":
            self.push_screen(ChooseDirectoryScreen(self.download_dir), callback=self.set_download_dir)

    def run_search(self) -> None:
        if not self.query:
            self.query_one("#status", Static).update("Please enter a search query.")
            return
        self.query_one("#status", Static).update("Searching Internet Archive...")
        self.do_search()

    @work(thread=True, exclusive=True)
    def do_search(self) -> None:
        try:
            data = search_archive(build_query(self.query, self.mediatype), self.rows, self.page)
        except RuntimeError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        self.call_from_thread(self.display_results, data)

    def display_results(self, data: dict[str, Any]) -> None:
        response = data.get("response", {})
        docs = response.get("docs", [])
        total = response.get("numFound", 0)
        table = self.query_one(DataTable)
        table.clear(columns=False)

        if not docs:
            self.current_identifiers = []
            table.add_row("-", "No matches found", "", "", "", "", "")
        else:
            start = (self.page - 1) * self.rows + 1
            self.current_identifiers = []
            for index, item in enumerate(docs, start=start):
                identifier = first(item.get("identifier"))
                self.current_identifiers.append(identifier)
                title = first(item.get("title"), "Untitled").replace("\n", " ")
                creator = first(item.get("creator")).replace("\n", " ")
                date = first(item.get("date")).replace("\n", " ")
                mediatype = first(item.get("mediatype"))
                downloads = item.get("downloads", "N/A")
                table.add_row(
                    str(index),
                    title[:100],
                    creator[:50],
                    date[:19],
                    mediatype,
                    str(downloads),
                    f"https://archive.org/details/{identifier}",
                    key=identifier,
                )

        self.last_status = (
            f"Found {total:,} result(s) | Page {self.page} | Showing {len(docs)} item(s) | {self.rows} entries per page"
        )
        self.query_one("#status", Static).update(self.last_status)
        if not self.details_open:
            table.focus()

    def action_focus_search(self) -> None:
        self.query_one("#query", Input).focus()

    def action_open_selected(self) -> None:
        table = self.query_one(DataTable)
        cursor_row = getattr(table, "cursor_row", None)
        if isinstance(cursor_row, int) and 0 <= cursor_row < len(self.current_identifiers):
            identifier = self.current_identifiers[cursor_row]
            if identifier and identifier != "-":
                self.open_details(identifier)

    def action_cursor_up(self) -> None:
        if self.details_open:
            files = self.query_one("#files-select", RangeSelectionList)
            files.focus()
            files.action_cursor_up()
        else:
            self.query_one(DataTable).action_cursor_up()

    def action_cursor_down(self) -> None:
        if self.details_open:
            files = self.query_one("#files-select", RangeSelectionList)
            files.focus()
            files.action_cursor_down()
        else:
            self.query_one(DataTable).action_cursor_down()

    def action_tab_next(self) -> None:
        self._switch_tab(1)

    def action_tab_previous(self) -> None:
        self._switch_tab(-1)

    def _switch_tab(self, direction: int) -> None:
        if self.details_open:
            self.query_one("#files-select", RangeSelectionList).focus()
            return

        focusables = [self.query_one("#query", Input), self.query_one(DataTable)]
        current = self.focused
        try:
            index = focusables.index(current)
        except ValueError:
            index = 0
        focusables[(index + direction) % len(focusables)].focus()

    def set_download_dir(self, path: Path | None) -> None:
        if path is not None:
            self.download_dir = path
            if self.details_open:
                try:
                    self.append_details_log(f"Download folder set to: {path}")
                except Exception:
                    pass
            else:
                self.last_status = f"Download folder set to: {path}"
                try:
                    self.query_one("#status", Static).update(self.last_status)
                except Exception:
                    pass

    def action_download_details(self) -> None:
        if self.details_open:
            self.download_current_item()

    def download_current_item(self) -> None:
        if not self.current_identifier or not self.current_detail_data:
            self.show_error("Open an item first before downloading.")
            return

        selected_names = set(self.query_one("#files-select", RangeSelectionList).selected)
        selected_names.discard("__truncated__")
        if not selected_names:
            self.append_details_log("Select one or more files to download first.")
            return

        files = self.current_detail_data.get("files", []) if isinstance(self.current_detail_data, dict) else []
        selected_files = [item for item in files if item.get("name") in selected_names]
        if not selected_files:
            self.append_details_log("No matching selected files found.")
            return

        filtered_data = dict(self.current_detail_data)
        filtered_data["files"] = selected_files
        self.append_details_log(f"Starting download of {len(selected_files)} selected file(s)...")
        self.download_worker(self.current_identifier, filtered_data)

    @work(thread=True, exclusive=True, group="download")
    def download_worker(self, identifier: str, data: dict[str, Any]) -> None:
        try:
            destination = download_item_files(identifier, data, self.download_dir, progress=lambda msg: self.call_from_thread(self.append_details_log, msg))
        except RuntimeError as exc:
            self.call_from_thread(self.append_details_log, f"Error: {exc}")
            return
        self.call_from_thread(self.append_details_log, f"Downloaded all contents to: {destination}")

    def append_details_log(self, message: str) -> None:
        self.query_one("#download-status", Static).update(message)

    def action_back_to_results(self) -> None:
        if not self.details_open:
            # If already on the results list, move focus back to the search bar.
            if self.focused is self.query_one(DataTable):
                self.query_one("#query", Input).focus()
            return
        self.query_one("#details-view", Vertical).display = False
        self.query_one("#controls", Horizontal).display = True
        self.query_one(DataTable).display = True
        self.query_one("#status", Static).display = True
        self.query_one("#status", Static).update(self.last_status)
        self.details_open = False
        table = self.query_one(DataTable)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        cursor_row = getattr(event, "cursor_row", None)
        identifier = None
        if isinstance(cursor_row, int) and 0 <= cursor_row < len(self.current_identifiers):
            identifier = self.current_identifiers[cursor_row]
        else:
            row_key = getattr(getattr(event, "row_key", None), "value", None)
            if row_key is not None:
                identifier = str(row_key)
        if identifier and identifier != "-":
            self.open_details(identifier)

    def open_details(self, identifier: str) -> None:
        self.query_one("#status", Static).update(f"Loading details for {identifier}...")
        self.fetch_details(identifier)

    @work(thread=True, exclusive=True, group="details")
    def fetch_details(self, identifier: str) -> None:
        try:
            data = fetch_item_metadata(identifier)
        except RuntimeError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        self.call_from_thread(self.push_detail_screen, identifier, data)

    def push_detail_screen(self, identifier: str, data: dict[str, Any]) -> None:
        self.current_identifier = identifier
        self.current_detail_data = data
        metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
        files = data.get("files", []) if isinstance(data, dict) else []
        title = first(metadata.get("title"), identifier)
        description = first(metadata.get("description"), "No description available.")
        if len(description) > 400:
            description = description[:400].rstrip() + "..."

        meta_lines = [
            f"Identifier: {identifier}",
            f"Creator: {first(metadata.get('creator'))}",
            f"Date: {first(metadata.get('date'))}",
            f"Type: {first(metadata.get('mediatype'))}",
            f"URL: https://archive.org/details/{identifier}",
            "",
            f"Description: {description}",
        ]

        self.query_one("#details-title", Static).update(title)
        self.query_one("#details-meta", Static).update("\n".join(meta_lines))

        selector = self.query_one("#files-select", RangeSelectionList)
        selector.clear_options()
        selector.anchor_index = None
        for item in files[:200]:
            name = item.get("name", "unknown")
            fmt = item.get("format", "unknown")
            size = item.get("size", "?")
            selector.add_option((f"{name} | {fmt} | {size} bytes", name))
        if len(files) > 200:
            selector.add_option((f"... and {len(files) - 200} more files", "__truncated__"))

        self.query_one("#download-status", Static).update(f"{len(files)} file(s) listed. Click items to select them.")
        self.query_one("#controls", Horizontal).display = False
        self.query_one(DataTable).display = False
        self.query_one("#status", Static).display = False
        self.query_one("#details-view", Vertical).display = True
        self.details_open = True
        selector.focus()

    def show_error(self, message: str) -> None:
        self.last_status = f"Error: {message}"
        self.query_one("#status", Static).update(self.last_status)

    def action_next_page(self) -> None:
        self.page += 1
        self.run_search()

    def action_previous_page(self) -> None:
        self.page = max(1, self.page - 1)
        self.run_search()

    def action_focus_search(self) -> None:
        self.query_one("#query", Input).focus()

    def action_quit(self) -> None:
        self.exit()


def interactive_search(query: str, rows: int, page: int, mediatype: str | None) -> int:
    while True:
        try:
            data = search_archive(build_query(query, mediatype), rows, page)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        print_results(data, rows, start=(page - 1) * rows + 1)
        print(f"Current search: {query!r} | Page {page} | Requesting {rows} entries per page")
        print("Options: [n] next page, [p] previous page, [m] change entries listed, [s] new search, [q] quit")

        try:
            choice = input("Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return 0

        if choice in {"n", "next"}:
            page += 1
        elif choice in {"p", "prev", "previous"}:
            page = max(1, page - 1)
        elif choice in {"m", "rows", "amount"}:
            try:
                new_rows = int(input("How many entries would you like listed? (1-100): ").strip())
            except (ValueError, EOFError, KeyboardInterrupt):
                print("Please enter a valid number.")
                continue
            if 1 <= new_rows <= 100:
                rows = new_rows
                page = 1
            else:
                print("Please choose a number between 1 and 100.")
        elif choice in {"s", "search", "new"}:
            try:
                query = input("New search query: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                return 0
            if not query:
                print("Search query cannot be empty.")
                continue
            page = 1
        elif choice in {"q", "quit", "exit"}:
            return 0
        else:
            print("Invalid choice. Use n, p, m, s, or q.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Search the Internet Archive and list matching items.")
    parser.add_argument("query", nargs="?", help="Search query, e.g. 'python programming'")
    parser.add_argument("--rows", "-r", type=int, default=10, help="Number of results to request (default: 10)")
    parser.add_argument("--page", "-p", type=int, default=1, help="Result page (default: 1)")
    parser.add_argument(
        "--mediatype",
        "-m",
        choices=["texts", "movies", "audio", "image", "software", "collection", "web"],
        help="Limit results to a media type",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of formatted results")
    parser.add_argument("--once", action="store_true", help="Print one page of results and exit without interactive options")
    parser.add_argument("--cli", action="store_true", help="Use the original terminal prompt loop instead of the Textual TUI")
    args = parser.parse_args()

    if args.rows < 1 or args.page < 1:
        print("Error: --rows and --page must be positive integers.", file=sys.stderr)
        return 2

    if args.json:
        query = args.query or input("Enter a search query: ").strip()
        if not query:
            print("Error: search query cannot be empty.", file=sys.stderr)
            return 2
        try:
            data = search_archive(build_query(query, args.mediatype), args.rows, args.page)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if args.once:
        query = args.query or input("Enter a search query: ").strip()
        if not query:
            print("Error: search query cannot be empty.", file=sys.stderr)
            return 2
        try:
            data = search_archive(build_query(query, args.mediatype), args.rows, args.page)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print_results(data, args.rows, start=(args.page - 1) * args.rows + 1)
        return 0

    if args.cli:
        query = args.query or input("Enter a search query: ").strip()
        if not query:
            print("Error: search query cannot be empty.", file=sys.stderr)
            return 2
        return interactive_search(query, args.rows, args.page, args.mediatype)

    ArchiveSearchApp(query=args.query or "", rows=args.rows, page=args.page, mediatype=args.mediatype).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
