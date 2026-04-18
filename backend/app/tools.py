"""Tool handler implementations for Claude function calling.

Each handler receives the tool input dict, the trips DB session, and the
VacationMap DB session, then returns a result string for Claude.
"""

import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import crud, vacationmap

# ---------------------------------------------------------------------------
# Tool definitions (passed to Claude API)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "search_destinations",
        "description": (
            "Search VacationMap destinations for a given month with optional filters. "
            "Returns top scored results with weather, cost, busyness, attractiveness, "
            "golf, safety scores, and travel tips."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "enum": [
                        "jan",
                        "feb",
                        "mar",
                        "apr",
                        "may",
                        "jun",
                        "jul",
                        "aug",
                        "sep",
                        "oct",
                        "nov",
                        "dec",
                        "christmas",
                        "easter",
                    ],
                },
                "activity_focus": {
                    "type": "string",
                    "enum": ["golf", "hiking", "nature", "city", "beach", "general"],
                    "description": (
                        "Activity focus adjusts scoring weights (e.g. golf boosts golf_score weight)"
                    ),
                },
                "max_flight_hours": {
                    "type": "number",
                    "description": "Maximum flight time from Munich in hours",
                },
                "min_safety_score": {
                    "type": "number",
                    "description": "Minimum safety score (0-10). Default 6.0",
                },
                "exclude_visited_never": {
                    "type": "boolean",
                    "description": (
                        "Exclude destinations marked 'visit_again: never'. Default true"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return. Default 10",
                },
            },
            "required": ["month"],
        },
    },
    {
        "name": "get_destination_details",
        "description": (
            "Get full details for a specific destination including all scores "
            "for a given month, travel tips, flight info, and visit history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region_lookup_key": {
                    "type": "string",
                    "description": (
                        "Format: CC:RegionName (e.g., PT:Algarve, TH:Bangkok)"
                    ),
                },
                "month": {"type": "string"},
            },
            "required": ["region_lookup_key", "month"],
        },
    },
    {
        "name": "get_visit_history",
        "description": (
            "Get all previously visited regions with ratings and revisit preferences."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "suggest_for_review",
        "description": (
            "Add a destination to the 'To Review' list for the user to evaluate. "
            "Call this once for EACH destination you want to suggest. The user will "
            "then shortlist or exclude it from the UI. Include your reasoning and scores."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destination_name": {
                    "type": "string",
                    "description": "Display name (e.g. 'Algarve, Portugal')",
                },
                "region_lookup_key": {
                    "type": "string",
                    "description": (
                        "VacationMap stable key (e.g. PT:Algarve). Omit if not in database."
                    ),
                },
                "ai_reasoning": {
                    "type": "string",
                    "description": (
                        "Your pros/cons reasoning for why this destination fits (or doesn't perfectly fit) the trip"
                    ),
                },
                "scores_snapshot": {
                    "type": "object",
                    "description": (
                        "Key scores: total_score, weather_score, cost_relative, busyness_relative, attractiveness, golf_score, flight_hours"
                    ),
                },
                "pre_filled_exclude_reason": {
                    "type": "string",
                    "description": (
                        "Optional pre-filled reason for excluding. Use for recently visited destinations, e.g. 'Visited in 2024, rated 8/10 — revisit not planned soon'"
                    ),
                },
            },
            "required": ["destination_name", "ai_reasoning"],
        },
    },
    {
        "name": "get_trip_state",
        "description": (
            "Get the current shortlisted and excluded destinations for this trip."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_search_destinations(
    params: dict, trips_db: Session, vm_db: Session, trip_id: int
) -> str:
    month = params["month"]
    search_result = vacationmap.search_destinations(
        db=vm_db,
        month=month,
        activity_focus=params.get("activity_focus", "general"),
        max_flight_hours=params.get("max_flight_hours"),
        min_safety_score=params.get("min_safety_score", 6.0),
        exclude_visited_never=params.get("exclude_visited_never", True),
        limit=params.get("limit", 20),  # fetch extra to account for filtering
    )

    results = search_result["results"]
    filtered_visited = search_result["filtered_visited"]

    # Filter out destinations already in any trip list
    trip = crud.get_trip(trips_db, trip_id)
    existing_keys = set()
    if trip:
        for d in trip.suggested:
            if d.region_lookup_key:
                existing_keys.add(d.region_lookup_key)
        for d in trip.shortlisted:
            if d.region_lookup_key:
                existing_keys.add(d.region_lookup_key)
        for d in trip.excluded:
            if d.region_lookup_key:
                existing_keys.add(d.region_lookup_key)

    m = month.lower()
    formatted = []
    for r in results:
        if r["lookup_key"] in existing_keys:
            continue
        entry = {
            "destination": f"{r['region_name']}, {r['country_name']}",
            "lookup_key": r["lookup_key"],
            "total_score": r["total_score"],
            "weather_score": round(r["weather_score"], 1),
            "temp_day": r.get(f"temp_{m}"),
            "rain_days": r.get(f"rain_{m}"),
            "cost_relative": r.get(f"cost_relative_{m}"),
            "busyness_relative": r.get(f"busyness_relative_{m}"),
            "attractiveness": r.get(f"attractiveness_relative_{m}"),
            "golf_score": r.get("golf_score"),
            "safety": r.get("crime_safety"),
            "nature_score": r.get("nature_score"),
            "hiking_score": r.get("hiking_score"),
            "flight_hours": r.get("flight_time_hours"),
            "flight_transfers": r.get("flight_transfers"),
            "tips": r.get(f"tips_{m}"),
        }
        if r.get("visit_again"):
            entry["visit_again"] = r["visit_again"]
        formatted.append(entry)
        if len(formatted) >= params.get("limit", 10):
            break

    # Include high-scoring destinations excluded due to recent visits
    excluded_due_to_visit = []
    for r in filtered_visited:
        if r["lookup_key"] in existing_keys:
            continue
        excluded_due_to_visit.append(
            {
                "destination": f"{r['region_name']}, {r['country_name']}",
                "lookup_key": r["lookup_key"],
                "total_score": r["total_score"],
                "golf_score": r.get("golf_score"),
                "visit_again": r["visit_again"],
            }
        )

    output = {"destinations": formatted}
    if excluded_due_to_visit:
        output["excluded_due_to_recent_visit"] = excluded_due_to_visit

    return json.dumps(output, indent=2)


def handle_get_destination_details(
    params: dict, trips_db: Session, vm_db: Session, trip_id: int
) -> str:
    details = vacationmap.get_destination_details(
        vm_db, params["region_lookup_key"], params["month"]
    )
    if details is None:
        return json.dumps(
            {
                "error": (
                    f"Destination '{params['region_lookup_key']}' not found in VacationMap database"
                )
            }
        )

    m = params["month"].lower()
    result = {
        "destination": f"{details['region_name']}, {details['country_name']}",
        "lookup_key": details["lookup_key"],
        "total_score": details["total_score"],
        "weather_score": round(details["weather_score"], 1),
        "temp_day": details.get(f"temp_{m}"),
        "temp_night": details.get(f"temp_night_{m}"),
        "rain_days": details.get(f"rain_{m}"),
        "cost_relative": details.get(f"cost_relative_{m}"),
        "cost_absolute": details.get(f"cost_absolute_{m}"),
        "busyness_relative": details.get(f"busyness_relative_{m}"),
        "busyness_absolute": details.get(f"busyness_absolute_{m}"),
        "attractiveness": details.get(f"attractiveness_relative_{m}"),
        "golf_score": details.get("golf_score"),
        "nature_score": details.get("nature_score"),
        "hiking_score": details.get("hiking_score"),
        "safety": details.get("crime_safety"),
        "city_access": details.get("city_access"),
        "hotel_quality": details.get("hotel_quality"),
        "tourism_level": details.get("tourism_level"),
        "flight_hours": details.get("flight_time_hours"),
        "flight_transfers": details.get("flight_transfers"),
        "tips": details.get(f"tips_{m}"),
        "visit": details.get("visit"),
    }
    return json.dumps(result, indent=2)


def handle_get_visit_history(
    params: dict, trips_db: Session, vm_db: Session, trip_id: int
) -> str:
    visits = vacationmap.get_visit_history(vm_db)
    formatted = []
    for v in visits:
        formatted.append(
            {
                "destination": f"{v['region_name']}, {v['country_name']}",
                "lookup_key": f"{v['country_code']}:{v['region_name']}",
                "rating": v.get("rating"),
                "rating_summary": v.get("rating_summary"),
                "visit_again": v.get("visit_again"),
                "visited_month": v.get("visited_month"),
                "visited_year": v.get("visited_year"),
                "summary": v.get("summary"),
            }
        )
    return json.dumps(formatted, indent=2)


def _clean_destination_name(name: str) -> tuple[str, Optional[str]]:
    """Parse destination name into (region_part, country_part).

    Handles formats like:
      "Algarve, Portugal" → ("Algarve", "Portugal")
      "Ireland (Golf region)" → ("Ireland", None)
      "Costa del Sol, Spain" → ("Costa del Sol", "Spain")
    """
    import re

    # Strip parenthetical qualifiers like "(Golf region)", "(South)", etc.
    cleaned = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()

    parts = [p.strip() for p in cleaned.split(",")]
    region_part = parts[0] if parts else cleaned
    country_part = parts[1] if len(parts) > 1 else None
    return region_part, country_part


def _resolve_lookup_key(params: dict, vm_db: Session) -> Optional[str]:
    """Try to resolve a VacationMap lookup key from params or destination name.

    Matching strategy (first match wins):
      1. Exact region name match
      2. region_name matches a country → pick best region by golf_score
      3. country_name + region_name cross-match
      4. country_name matches a country → pick best region
      5. Fuzzy LIKE match on region name (fallback)
    """
    key = params.get("region_lookup_key")
    if key:
        return key

    name = params.get("destination_name", "")
    region_name, country_name = _clean_destination_name(name)

    # 1. Exact match on region name
    row = vm_db.execute(
        text(
            "SELECT r.name, c.code FROM regions r "
            "JOIN countries c ON r.country_id = c.id "
            "WHERE r.name = :name LIMIT 1"
        ),
        {"name": region_name},
    ).fetchone()
    if row:
        return f"{row[1]}:{row[0]}"

    # Helper: pick best region in a country by golf_score (desc)
    def _pick_best_region(country_match: str) -> Optional[str]:
        rows = vm_db.execute(
            text(
                "SELECT r.name, c.code, r.golf_score FROM regions r "
                "JOIN countries c ON r.country_id = c.id "
                "WHERE c.name LIKE :country "
                "ORDER BY r.golf_score DESC NULLS LAST"
            ),
            {"country": f"%{country_match}%"},
        ).fetchall()
        if rows:
            return f"{rows[0][1]}:{rows[0][0]}"
        return None

    # 2. region_name itself might be a country (e.g. "Ireland", "Scotland")
    #    Try this BEFORE fuzzy region LIKE to avoid e.g. "Ireland" matching
    #    "Northern Ireland" (GB) instead of Ireland (IE).
    result = _pick_best_region(region_name)
    if result:
        return result

    # 3. If country is provided, try matching region within that country
    if country_name:
        row = vm_db.execute(
            text(
                "SELECT r.name, c.code FROM regions r "
                "JOIN countries c ON r.country_id = c.id "
                "WHERE c.name LIKE :country AND r.name LIKE :region LIMIT 1"
            ),
            {"country": f"%{country_name}%", "region": f"%{region_name.split()[0]}%"},
        ).fetchone()
        if row:
            return f"{row[1]}:{row[0]}"

    # 4. country_name matches a country → pick best region
    if country_name:
        result = _pick_best_region(country_name)
        if result:
            return result

    # 5. Fuzzy LIKE match on region name
    row = vm_db.execute(
        text(
            "SELECT r.name, c.code FROM regions r "
            "JOIN countries c ON r.country_id = c.id "
            "WHERE r.name LIKE :name LIMIT 1"
        ),
        {"name": f"%{region_name}%"},
    ).fetchone()
    if row:
        return f"{row[1]}:{row[0]}"

    # 6. Try each word in region_name as a country name
    #    Handles "Portugal Golf Coast" → find "Portugal" as country,
    #    then try remaining words against its regions, else pick best.
    words = region_name.split()
    if len(words) > 1:
        for i, word in enumerate(words):
            if len(word) < 3:
                continue
            country_row = vm_db.execute(
                text("SELECT id, name FROM countries WHERE name LIKE :w LIMIT 1"),
                {"w": f"%{word}%"},
            ).fetchone()
            if not country_row:
                continue

            # Found a country — try remaining words against its regions
            remaining = [w for j, w in enumerate(words) if j != i]
            for rw in remaining:
                if len(rw) < 3:
                    continue
                row = vm_db.execute(
                    text(
                        "SELECT r.name, c.code FROM regions r "
                        "JOIN countries c ON r.country_id = c.id "
                        "WHERE c.id = :cid AND r.name LIKE :rw LIMIT 1"
                    ),
                    {"cid": country_row[0], "rw": f"%{rw}%"},
                ).fetchone()
                if row:
                    return f"{row[1]}:{row[0]}"

            # No region word matched — pick best region in that country
            return _pick_best_region(country_row[1])

    return None


def _build_scores_from_db(
    vm_db: Session, lookup_key: str, month: str
) -> Optional[dict]:
    """Look up real scores from VacationMap for a lookup key and month."""
    details = vacationmap.get_destination_details(vm_db, lookup_key, month)
    if details is None:
        return None

    m = month.lower()
    return {
        "total_score": details.get("total_score"),
        "weather_score": round(details.get("weather_score", 0), 1),
        "cost_relative": details.get(f"cost_relative_{m}"),
        "busyness_relative": details.get(f"busyness_relative_{m}"),
        "attractiveness": details.get(f"attractiveness_relative_{m}"),
        "golf_score": details.get("golf_score"),
        "flight_hours": details.get("flight_time_hours"),
    }


def _has_real_scores(scores: Optional[dict]) -> bool:
    """Check if scores dict has actual VacationMap keys (not AI-estimated ones)."""
    if not scores:
        return False
    return "total_score" in scores and scores["total_score"] is not None


def _auto_lookup_scores(
    params: dict, vm_db: Session, trip_id: int, trips_db: Session
) -> tuple[Optional[str], Optional[dict]]:
    """Resolve lookup key and scores. Returns (lookup_key, scores)."""
    scores = params.get("scores_snapshot")
    lookup_key = _resolve_lookup_key(params, vm_db)

    # If we already have real scores and a key, use them
    if _has_real_scores(scores) and lookup_key:
        return lookup_key, scores

    # If we have a key, look up real scores from VacationMap
    if lookup_key:
        trip = crud.get_trip(trips_db, trip_id)
        month = trip.target_month if trip else None
        if not month:
            month = "jun"
        db_scores = _build_scores_from_db(vm_db, lookup_key, month)
        if db_scores:
            return lookup_key, db_scores

    # No key or not in DB — return whatever we have
    return lookup_key, scores if _has_real_scores(scores) else None


def _is_already_in_trip(
    trips_db: Session, trip_id: int, name: str, lookup_key: Optional[str]
) -> Optional[str]:
    """Check if destination is already in any trip list. Returns list name or None."""
    trip = crud.get_trip(trips_db, trip_id)
    if not trip:
        return None
    name_lower = name.lower()
    for d in trip.suggested:
        if (
            lookup_key and d.region_lookup_key == lookup_key
        ) or d.destination_name.lower() == name_lower:
            return "pending review"
    for d in trip.shortlisted:
        if (
            lookup_key and d.region_lookup_key == lookup_key
        ) or d.destination_name.lower() == name_lower:
            return "shortlisted"
    for d in trip.excluded:
        if (
            lookup_key and d.region_lookup_key == lookup_key
        ) or d.destination_name.lower() == name_lower:
            return "excluded"
    return None


def _get_sibling_regions(vm_db: Session, lookup_key: str) -> list[dict]:
    """Get other regions in the same country, sorted by golf_score desc."""
    parts = lookup_key.split(":", 1)
    if len(parts) != 2:
        return []
    country_code = parts[0]
    region_name = parts[1]

    rows = vm_db.execute(
        text(
            "SELECT r.name, c.code, r.golf_score, r.nature_score, r.hiking_score "
            "FROM regions r JOIN countries c ON r.country_id = c.id "
            "WHERE c.code = :cc AND r.name != :rn "
            "ORDER BY r.golf_score DESC NULLS LAST"
        ),
        {"cc": country_code, "rn": region_name},
    ).fetchall()
    return [
        {
            "region": f"{r[1]}:{r[0]}",
            "name": r[0],
            "golf_score": r[2],
            "nature_score": r[3],
            "hiking_score": r[4],
        }
        for r in rows
    ]


def handle_suggest_for_review(
    params: dict, trips_db: Session, vm_db: Session, trip_id: int
) -> str:
    lookup_key, scores = _auto_lookup_scores(params, vm_db, trip_id, trips_db)

    # Reject if already in any list
    already = _is_already_in_trip(
        trips_db, trip_id, params["destination_name"], lookup_key
    )
    if already:
        return json.dumps(
            {
                "status": "rejected",
                "reason": f"Already in {already} list",
                "destination": params["destination_name"],
            }
        )

    # Detect if a fuzzy match changed the destination name
    original_name = params["destination_name"]
    matched_region_name = None
    sibling_hint = None
    if lookup_key:
        _, region_part = lookup_key.split(":", 1)
        # If the resolved region name differs from what the AI said, flag it
        if region_part.lower() not in original_name.lower():
            matched_region_name = region_part
            # Use the DB region name as the display name
            country_row = vm_db.execute(
                text(
                    "SELECT c.name FROM countries c "
                    "JOIN regions r ON r.country_id = c.id "
                    "WHERE c.code = :cc LIMIT 1"
                ),
                {"cc": lookup_key.split(":")[0]},
            ).fetchone()
            country_display = country_row[0] if country_row else ""
            original_name = (
                f"{region_part}, {country_display}" if country_display else region_part
            )

        siblings = _get_sibling_regions(vm_db, lookup_key)
        if siblings:
            sibling_hint = siblings[:5]

    dest = crud.add_suggested(
        db=trips_db,
        trip_id=trip_id,
        destination_name=original_name,
        ai_reasoning=params["ai_reasoning"],
        region_lookup_key=lookup_key,
        scores_snapshot=scores,
        pre_filled_exclude_reason=params.get("pre_filled_exclude_reason"),
    )
    result = {
        "status": "suggested_for_review",
        "destination": dest.destination_name,
        "id": dest.id,
        "scores_resolved": scores is not None,
        "lookup_key_resolved": lookup_key,
    }
    if matched_region_name:
        result["fuzzy_matched"] = True
        result["matched_region"] = matched_region_name
        result["note"] = (
            f"Your suggestion was fuzzy-matched to '{matched_region_name}'. "
            "Update your reasoning to mention this specific region."
        )
    if sibling_hint:
        result["other_regions_in_country"] = sibling_hint

    return json.dumps(result)


def handle_get_trip_state(
    params: dict, trips_db: Session, vm_db: Session, trip_id: int
) -> str:
    trip = crud.get_trip(trips_db, trip_id)
    if trip is None:
        return json.dumps({"error": "Trip not found"})

    shortlisted = []
    for s in trip.shortlisted:
        scores = json.loads(s.scores_snapshot) if s.scores_snapshot else None
        shortlisted.append(
            {
                "destination": s.destination_name,
                "lookup_key": s.region_lookup_key,
                "scores": scores,
                "user_note": s.user_note,
            }
        )

    excluded = [
        {"destination": e.destination_name, "reason": e.reason} for e in trip.excluded
    ]

    suggested = []
    for s in trip.suggested:
        scores = json.loads(s.scores_snapshot) if s.scores_snapshot else None
        suggested.append(
            {
                "destination": s.destination_name,
                "lookup_key": s.region_lookup_key,
                "scores": scores,
            }
        )

    return json.dumps(
        {
            "pending_review": suggested,
            "shortlisted": shortlisted,
            "excluded": excluded,
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "search_destinations": handle_search_destinations,
    "get_destination_details": handle_get_destination_details,
    "get_visit_history": handle_get_visit_history,
    "suggest_for_review": handle_suggest_for_review,
    "get_trip_state": handle_get_trip_state,
}


def execute_tool(
    tool_name: str,
    tool_input: dict,
    trips_db: Session,
    vm_db: Session,
    trip_id: int,
) -> str:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    return handler(tool_input, trips_db, vm_db, trip_id)
