#!/usr/bin/env python
"""Populate the golf library from the curated seed YAML (spec 006 FR-S-001..S-006).

Two subcommands:

    extract   — run the per-entry AI extraction pipeline. Cheap when entries
                have `homepage_url` or `source_urls` set; expensive (web_search)
                for bare-name entries.

    enumerate — fetch one or more ranking pages (Today's Golfer, Top 100 Golf
                Courses, etc.), ask Claude to parse a structured list of
                `(name, country_code, homepage_url)` tuples, and merge them
                into the YAML under their section. One-shot and cheap.

    ls-sources — list the enumerate source registry.

Both subcommands are idempotent. Extract dedups by (name_norm, country_code,
entity_type) before calling Claude. Enumerate dedups by the same key before
appending to the YAML.

Usage:
    cd backend && source venv/bin/activate
    python scripts/seed_golf_library.py extract [--entity resorts|courses|all] [--limit N] [--dry-run] [--require-url]
    python scripts/seed_golf_library.py enumerate --source todays-golfer-resorts
    python scripts/seed_golf_library.py ls-sources

Requires:
    ANTHROPIC_API_KEY exported (or in .env at the repo root)
    PyYAML installed (pip install pyyaml)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Make `app.*` importable when run as a script from `backend/`.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

import yaml  # noqa: E402

import anthropic  # noqa: E402

from app.database import GolfSessionLocal, init_golf_db  # noqa: E402
from app.golf import crud, extraction, fetcher  # noqa: E402,F401
from app.text_utils import normalize_name  # noqa: E402

SEED_PATH = _BACKEND_ROOT / "app" / "golf" / "seed_data" / "golf_library_seed.yaml"
PAUSE_BETWEEN_S = 1.5


# ---------------------------------------------------------------------------
# Source registry — used by the `enumerate` subcommand.
# ---------------------------------------------------------------------------


SOURCES: dict[str, dict] = {
    "todays-golfer-resorts-cont-europe": {
        "url": (
            "https://www.todays-golfer.com/courses/best/golf-resorts-in-continental-europe/"
        ),
        "kind": "resorts",
        "description": "Today's Golfer — 100 best golf resorts in Continental Europe",
    },
    "top100gc-europe-resorts": {
        "url": "https://www.top100golfcourses.com/top-100-golf-resorts-europe",
        "kind": "resorts",
        "description": "Top 100 Golf Courses — best European resorts",
    },
    "top100gc-europe-courses": {
        "url": "https://www.top100golfcourses.com/top-100-golf-courses-europe",
        "kind": "courses",
        "description": "Top 100 Golf Courses — best European courses",
    },
    "top100gc-britain-ireland-courses": {
        "url": (
            "https://www.top100golfcourses.com/top-100-golf-courses-britain-and-ireland"
        ),
        "kind": "courses",
        "description": "Top 100 Golf Courses — Great Britain & Ireland",
    },
    "golfmonthly-europe-resorts": {
        "url": "https://www.golfmonthly.com/features/the-best-golf-resorts-in-europe",
        "kind": "resorts",
        "description": "Golf Monthly — best golf resorts in Europe",
    },
}


_ENUMERATE_TOOL = {
    "name": "return_ranked_entries",
    "description": (
        "Return the parsed list of resorts or courses found on the page. "
        "For each entry, provide: name, country_code (ISO alpha-2), and, when "
        "resolvable, the OFFICIAL homepage_url (not the ranking site's URL). "
        "If the entry is clearly not in the scope requested (wrong kind, wrong "
        "region, aggregator blurb), skip it."
    ),
    "input_schema": {
        "type": "object",
        "required": ["entries"],
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "country_code"],
                    "properties": {
                        "name": {"type": "string"},
                        "country_code": {"type": "string"},
                        "homepage_url": {
                            "type": "string",
                            "description": (
                                "Prefer the resort/course's own site. Omit if "
                                "only the ranking page's deep-link is known."
                            ),
                        },
                        "rank_position": {
                            "type": "integer",
                            "description": "Position on the ranking, when explicit.",
                        },
                    },
                },
            },
        },
    },
}


_ENUMERATE_SYSTEM = (
    "You are parsing a golf-ranking page. Extract the structured list of "
    "resorts or courses (per the page's scope). Be faithful to the page — do "
    "not invent entries or infer homepage URLs you can't see. Prefer the "
    "entity's own domain when it's mentioned or linked."
)


# ---------------------------------------------------------------------------
# YAML load / save with merge dedup
# ---------------------------------------------------------------------------


def _load_seed() -> dict:
    if not SEED_PATH.is_file():
        raise SystemExit(f"seed YAML not found at {SEED_PATH}")
    with SEED_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data.get("version") != 1:
        print(f"warning: seed file version is {data.get('version')}, expected 1")
    data.setdefault("resorts", [])
    data.setdefault("courses", [])
    return data


def _save_seed(data: dict) -> None:
    header = (
        "# Golf Library seed list (spec 006 FR-S-001..S-006)\n"
        "#\n"
        "# This file is auto-updated by `seed_golf_library.py enumerate`.\n"
        "# Manual edits are preserved on merge (dedup by name_norm + country).\n"
        "#\n"
        "# Entry schema:\n"
        "#   name: ..., country_code: XX, homepage_url?: ..., source_urls?: [...]\n"
    )
    with SEED_PATH.open("w", encoding="utf-8") as f:
        f.write(header + "\n")
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=100)


def _dedup_key(entry: dict) -> tuple[str, str]:
    return (
        normalize_name(entry.get("name", "")),
        (entry.get("country_code") or "").upper(),
    )


def _merge_entries(existing: list[dict], new_entries: list[dict]) -> tuple[int, int]:
    """Merge `new_entries` into `existing` list by dedup key. Returns (added, updated)."""
    by_key = {_dedup_key(e): e for e in existing}
    added = 0
    updated = 0
    for e in new_entries:
        if not e.get("name") or not e.get("country_code"):
            continue
        key = _dedup_key(e)
        if key in by_key:
            # Merge additive fields: homepage_url fills if absent, source_urls unions.
            target = by_key[key]
            changed = False
            if e.get("homepage_url") and not target.get("homepage_url"):
                target["homepage_url"] = e["homepage_url"]
                changed = True
            src_existing = set(target.get("source_urls") or [])
            src_new = [u for u in e.get("source_urls") or [] if u]
            merged = list(target.get("source_urls") or [])
            for u in src_new:
                if u not in src_existing:
                    merged.append(u)
                    src_existing.add(u)
                    changed = True
            if merged:
                target["source_urls"] = merged
            if changed:
                updated += 1
        else:
            existing.append(
                {
                    k: v
                    for k, v in e.items()
                    if k in ("name", "country_code", "homepage_url", "source_urls")
                }
            )
            added += 1
    return added, updated


# ---------------------------------------------------------------------------
# Extract subcommand (per-entry)
# ---------------------------------------------------------------------------


def _resort_exists(db, name: str, country_code: str) -> bool:
    from app.golf import models

    norm = normalize_name(name)
    return (
        db.query(models.GolfResort)
        .filter(
            models.GolfResort.name_norm == norm,
            models.GolfResort.country_code == country_code,
        )
        .first()
        is not None
    )


def _course_exists(db, name: str, country_code: str) -> bool:
    from app.golf import models

    norm = normalize_name(name)
    return (
        db.query(models.GolfCourse)
        .filter(
            models.GolfCourse.name_norm == norm,
            models.GolfCourse.country_code == country_code,
        )
        .first()
        is not None
    )


def _pick_fetch_url(entry: dict) -> tuple[str | None, list[str]]:
    """Return (url_to_fetch, extra_source_urls_for_provenance).

    Prefers homepage_url; falls back to source_urls[0]; else returns (None, …).
    The non-chosen URLs are still recorded as sources.
    """
    homepage = entry.get("homepage_url")
    sources = [
        s for s in entry.get("source_urls") or [] if isinstance(s, str) and s.strip()
    ]
    if homepage:
        return homepage, sources
    if sources:
        return sources[0], sources[1:]
    return None, []


def _seed_one_resort(
    db, entry: dict, *, dry_run: bool, require_url: bool, vm_db=None
) -> str:
    name = entry["name"]
    country = entry["country_code"]
    if _resort_exists(db, name, country):
        return f"SKIPPED (duplicate)   {name} ({country})"
    url, extra_sources = _pick_fetch_url(entry)
    if require_url and not url:
        return f"SKIPPED (no url)      {name} ({country})"
    if dry_run:
        via = f"url={url}" if url else "name-only (web_search)"
        return f"WOULD CREATE [{via}]  {name} ({country})"

    try:
        extracted = extraction.extract_resort(
            url=url,
            name=None if url else name,
            extra_source_urls=extra_sources,
        )
    except extraction.ExtractError as e:
        return f"FAILED ({e.status})   {name} ({country}) — {e.message[:240]}"

    extracted.data.country_code = country
    try:
        resort = crud.create_resort(db, extracted.data, force=True, vm_db=vm_db)
    except Exception as e:
        return f"FAILED (db)           {name} ({country}) — {str(e)[:240]}"
    return f"CREATED id={resort.id:<4} {name} ({country})"


def _seed_one_course(
    db, entry: dict, *, dry_run: bool, require_url: bool, vm_db=None
) -> str:
    name = entry["name"]
    country = entry["country_code"]
    if _course_exists(db, name, country):
        return f"SKIPPED (duplicate)   {name} ({country})"
    url, extra_sources = _pick_fetch_url(entry)
    if require_url and not url:
        return f"SKIPPED (no url)      {name} ({country})"
    if dry_run:
        via = f"url={url}" if url else "name-only (web_search)"
        return f"WOULD CREATE [{via}]  {name} ({country})"

    try:
        extracted = extraction.extract_course(
            url=url,
            name=None if url else name,
            extra_source_urls=extra_sources,
            existing_parent_resort_lookup=lambda n: crud.find_resort_by_name_norm(
                db, n
            ),
        )
    except extraction.ExtractError as e:
        return f"FAILED ({e.status})   {name} ({country}) — {e.message[:240]}"

    extracted.data.country_code = country
    try:
        course = crud.create_course(db, extracted.data, force=True)
    except Exception as e:
        return f"FAILED (db)           {name} ({country}) — {str(e)[:240]}"
    return f"CREATED id={course.id:<4} {name} ({country})"


def cmd_extract(args):
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set. Pass --dry-run or export the key.")
        sys.exit(1)

    init_golf_db()
    data = _load_seed()
    remaining = args.limit

    db = GolfSessionLocal()
    summary = {"created": 0, "skipped": 0, "failed": 0, "processed": 0}
    try:

        def _budget_ok() -> bool:
            return remaining is None or remaining > 0

        def _bump(line: str) -> None:
            summary["processed"] += 1
            if line.startswith("CREATED"):
                summary["created"] += 1
            elif line.startswith("SKIPPED"):
                summary["skipped"] += 1
            elif line.startswith("WOULD CREATE"):
                pass
            else:
                summary["failed"] += 1

        # --limit counts real API attempts only (CREATED / FAILED / WOULD CREATE).
        # Cheap no-ops (SKIPPED as duplicate or SKIPPED as no-url) don't consume
        # the budget, so you can set --limit 10 and get 10 actual extractions
        # even when the list is front-loaded with already-seeded rows.
        def _consumes_budget(line: str) -> bool:
            return line.startswith(("CREATED", "FAILED", "WOULD CREATE"))

        if args.entity in ("resorts", "all"):
            entries = data.get("resorts", [])
            print(f"\n=== Resorts ({len(entries)}) ===")
            for entry in entries:
                if not _budget_ok():
                    break
                line = _seed_one_resort(
                    db, entry, dry_run=args.dry_run, require_url=args.require_url
                )
                print(line)
                _bump(line)
                if remaining is not None and _consumes_budget(line):
                    remaining -= 1
                if not args.dry_run and _consumes_budget(line):
                    time.sleep(PAUSE_BETWEEN_S)

        if args.entity in ("courses", "all") and _budget_ok():
            entries = data.get("courses", [])
            print(f"\n=== Courses ({len(entries)}) ===")
            for entry in entries:
                if not _budget_ok():
                    break
                line = _seed_one_course(
                    db, entry, dry_run=args.dry_run, require_url=args.require_url
                )
                print(line)
                _bump(line)
                if remaining is not None and _consumes_budget(line):
                    remaining -= 1
                if not args.dry_run and _consumes_budget(line):
                    time.sleep(PAUSE_BETWEEN_S)

        print("\n=== Summary ===")
        print(
            f"processed={summary['processed']} created={summary['created']} "
            f"skipped={summary['skipped']} failed={summary['failed']}"
        )
        if args.limit is not None and summary["processed"] >= args.limit:
            print(f"(limit reached: --limit {args.limit})")
        if summary["failed"]:
            sys.exit(2)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Enumerate subcommand (ranking-page → YAML)
# ---------------------------------------------------------------------------


def cmd_ls_sources(_args):
    print("\nRegistered enumerate sources:\n")
    for name, meta in SOURCES.items():
        print(f"  {name:<42} ({meta['kind']}) — {meta['description']}")
        print(f"  {'':<42}  {meta['url']}\n")


def _enumerate_one_source(source_name: str, *, dry_run: bool) -> list[dict]:
    meta = SOURCES.get(source_name)
    if meta is None:
        print(
            f"ERROR: unknown source {source_name!r}. Run `ls-sources` to see options."
        )
        return []
    source_url = meta["url"]
    print(f"\n--- Fetching {source_name} ---\n    {source_url}")

    if dry_run:
        print("  dry-run: would delegate page read to Claude + web_search")
        return []

    client = anthropic.Anthropic()
    # Ranking pages are often JS-rendered, so raw HTML fetching doesn't see
    # the list. Delegate the read to Claude's server-side web_search tool,
    # which renders JavaScript. A single source usually needs 1–2 search uses.
    tools = [
        _ENUMERATE_TOOL,
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
    ]
    try:
        response = client.messages.create(
            model=extraction.MODEL,
            # Ranking lists of ~100 entries ≈ 4–6k output tokens. Generous
            # budget; one-shot cost is negligible compared to per-entry extracts.
            max_tokens=8192,
            system=_ENUMERATE_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Source: {meta['description']}\n"
                                f"URL: {source_url}\n\n"
                                "Read the page using web_search (which renders "
                                "JavaScript). Then call return_ranked_entries "
                                "with every entry on the page — names, country "
                                "codes, and homepage URLs when you can see them. "
                                f"Scope: this source lists {meta['kind']}."
                            ),
                        }
                    ],
                }
            ],
            tools=tools,
            tool_choice={"type": "tool", "name": "return_ranked_entries"},
        )
    except anthropic.APIError as e:
        print(f"  ! Claude API error: {e}")
        return []

    # Debug: summarize what Claude did before we dig into tool_use.
    web_search_count = 0
    text_preview = ""
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
            web_search_count += 1
        elif btype == "text":
            text_preview = (getattr(block, "text", "") or "")[:200]
    if web_search_count:
        print(f"  (web_search used {web_search_count} time(s))")
    if text_preview:
        print(f"  Claude text: {text_preview}...")
    print(f"  stop_reason={getattr(response, 'stop_reason', '?')}")

    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "return_ranked_entries"
        ):
            entries = list(block.input.get("entries", []))
            out: list[dict] = []
            for e in entries:
                name = (e.get("name") or "").strip()
                country = (e.get("country_code") or "").strip().upper()
                if not name or not country:
                    continue
                row: dict = {"name": name, "country_code": country}
                if e.get("homepage_url"):
                    row["homepage_url"] = e["homepage_url"]
                row["source_urls"] = [source_url]
                out.append(row)
            print(f"  extracted {len(out)} entries")
            return out

    print("  ! no tool_use block returned — Claude didn't emit a structured list")
    return []


def cmd_enumerate(args):
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set. Pass --dry-run or export the key.")
        sys.exit(1)

    data = _load_seed()
    total_added_resorts = 0
    total_added_courses = 0
    total_updated_resorts = 0
    total_updated_courses = 0
    for source_name in args.source:
        meta = SOURCES.get(source_name)
        if meta is None:
            print(f"  ! unknown source: {source_name} (skipping)")
            continue
        entries = _enumerate_one_source(source_name, dry_run=args.dry_run)
        if args.dry_run or not entries:
            continue
        if meta["kind"] == "resorts":
            added, updated = _merge_entries(data["resorts"], entries)
            total_added_resorts += added
            total_updated_resorts += updated
            print(f"  merged into resorts: {added} new, {updated} updated")
        else:
            added, updated = _merge_entries(data["courses"], entries)
            total_added_courses += added
            total_updated_courses += updated
            print(f"  merged into courses: {added} new, {updated} updated")

    if not args.dry_run:
        _save_seed(data)
        print("\n=== Enumerate summary ===")
        print(
            f"resorts: +{total_added_resorts} new, {total_updated_resorts} updated; "
            f"courses: +{total_added_courses} new, {total_updated_courses} updated"
        )
        print(f"seed YAML saved to {SEED_PATH}")


# ---------------------------------------------------------------------------
# .env loader + argparse wiring
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from .env (repo root) into os.environ if unset."""
    env_path = _BACKEND_ROOT.parent / ".env"
    if not env_path.is_file():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    except OSError:
        pass


def main():
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd")

    p_extract = sub.add_parser(
        "extract", help="Run per-entry AI extraction from the YAML"
    )
    p_extract.add_argument(
        "--entity", choices=["resorts", "courses", "all"], default="all"
    )
    p_extract.add_argument("--limit", type=int, default=None)
    p_extract.add_argument("--dry-run", action="store_true")
    p_extract.add_argument(
        "--require-url",
        action="store_true",
        help="Skip entries without a homepage_url or source_urls[0] — no web_search fallback",
    )
    p_extract.set_defaults(func=cmd_extract)

    p_enum = sub.add_parser(
        "enumerate", help="Parse a ranking page and merge entries into the YAML"
    )
    p_enum.add_argument(
        "--source",
        action="append",
        required=True,
        help="Source name — run `ls-sources` to list",
    )
    p_enum.add_argument("--dry-run", action="store_true")
    p_enum.set_defaults(func=cmd_enumerate)

    p_ls = sub.add_parser("ls-sources", help="List registered enumerate sources")
    p_ls.set_defaults(func=cmd_ls_sources)

    # Back-compat: bare invocation (no subcommand) → extract
    args = parser.parse_args()
    if args.cmd is None:
        # Re-parse with `extract` defaulted.
        sys.argv.insert(1, "extract")
        args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
