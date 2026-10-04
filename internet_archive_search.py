#!/usr/bin/env python3
"""Simple CLI app to search the Internet Archive and list matches."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

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


def first(value: Any, default: str = "N/A") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        if not value:
            return default
        return str(value[0])
    return str(value)


def print_results(data: dict[str, Any], base_url_rows: int) -> None:
    response = data.get("response", {})
    docs = response.get("docs", [])
    total = response.get("numFound", 0)

    print(f"\nFound {total:,} result(s). Showing page results ({len(docs)} item(s), up to {base_url_rows} requested).\n")

    if not docs:
        print("No matches found.")
        return

    for index, item in enumerate(docs, start=1):
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
    args = parser.parse_args()

    query = args.query or input("Enter a search query: ").strip()
    if not query:
        print("Error: search query cannot be empty.", file=sys.stderr)
        return 2

    if args.rows < 1 or args.page < 1:
        print("Error: --rows and --page must be positive integers.", file=sys.stderr)
        return 2

    try:
        data = search_archive(build_query(query, args.mediatype), args.rows, args.page)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_results(data, args.rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
