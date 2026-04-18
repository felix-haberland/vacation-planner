"""Read-only access to VacationMap's vacation.db.

Uses raw SQL via SQLAlchemy text() to avoid duplicating VacationMap's full
80-column ORM model. All queries are SELECT-only.
"""

from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Scoring functions — ported from VacationMap/backend/app/crud.py
# ---------------------------------------------------------------------------


def _weather_comfort(
    temp: Optional[float], rain_days: Optional[float], humidity: Optional[float]
) -> float:
    if temp is None or rain_days is None or humidity is None:
        return 5.0

    if 25 <= temp <= 28:
        ts = 10.0
    elif 23 <= temp < 25:
        ts = 9.5
    elif 28 < temp <= 30:
        ts = 9.5
    elif 20 <= temp < 23:
        ts = 8.0 + (temp - 20) * 0.5
    elif 30 < temp <= 32:
        ts = 9.0 - (temp - 30) * 0.75
    elif 18 <= temp < 20:
        ts = 6.0 + (temp - 18) * 1.0
    elif 32 < temp <= 36:
        ts = 7.5 - (temp - 32) * 1.5
    elif 15 <= temp < 18:
        ts = 2.0 + (temp - 15) * 1.33
    elif temp > 36:
        ts = max(0, 1.5 - (temp - 36) * 0.4)
    elif 10 <= temp < 15:
        ts = 0.5 + (temp - 10) * 0.3
    else:
        ts = max(0, 0.5 - (10 - temp) * 0.1)

    if rain_days <= 2:
        rs = 10.0
    elif rain_days <= 4:
        rs = 8.5 - (rain_days - 2) * 0.5
    elif rain_days <= 6:
        rs = 7.5 - (rain_days - 4) * 1.0
    elif rain_days <= 8:
        rs = 5.5 - (rain_days - 6) * 1.25
    elif rain_days <= 12:
        rs = 3.0 - (rain_days - 8) * 0.5
    else:
        rs = max(0, 1.0 - (rain_days - 12) * 0.2)

    return max(0, min(10, ts * 0.55 + rs * 0.45))


def _golf_weather(temp: Optional[float], rain_days: Optional[float]) -> Optional[float]:
    if temp is None or rain_days is None:
        return None

    if 20 <= temp <= 26:
        ts = 10.0
    elif 18 <= temp < 20:
        ts = 8.0 + (temp - 18) * 1.0
    elif 26 < temp <= 28:
        ts = 10.0 - (temp - 26) * 1.0
    elif 15 <= temp < 18:
        ts = 4.0 + (temp - 15) * 1.33
    elif 28 < temp <= 32:
        ts = 8.0 - (temp - 28) * 1.25
    elif 10 <= temp < 15:
        ts = 1.0 + (temp - 10) * 0.6
    elif temp > 32:
        ts = max(0, 3.0 - (temp - 32) * 0.75)
    else:
        ts = max(0, 1.0 - (10 - temp) * 0.2)

    if rain_days <= 2:
        rs = 10.0
    elif rain_days <= 4:
        rs = 10 - (rain_days - 2) * 1.0
    elif rain_days <= 7:
        rs = 8 - (rain_days - 4) * 1.33
    elif rain_days <= 12:
        rs = 4 - (rain_days - 7) * 0.6
    else:
        rs = max(0, 1 - (rain_days - 12) * 0.1)

    return round(ts * 0.5 + rs * 0.5, 2)


def _compute_score(row: dict, month: str, golf_weight: float = 0.0) -> float:
    """Compute composite score for a region row dict, matching VacationMap logic."""
    m = month.lower()
    temp = row.get(f"temp_{m}")
    rain = row.get(f"rain_{m}")
    humidity = row.get(f"humidity_{m}")
    attractiveness = row.get(f"attractiveness_relative_{m}")
    cost_rel = row.get(f"cost_relative_{m}") or 5.0
    busy_rel = row.get(f"busyness_relative_{m}") or 5.0

    weather = _weather_comfort(temp, rain, humidity)
    if attractiveness is None:
        attractiveness = weather

    quality = (
        (row.get("golf_score") or 0)
        + (row.get("nature_score") or 0)
        + (row.get("tourism_level") or 0)
        + (row.get("city_access") or 0)
        + (row.get("hotel_quality") or 0)
    ) / 5.0

    gw = _golf_weather(temp, rain)
    if gw is not None:
        golf_combined = (row.get("golf_score") or 0) * (0.4 + 0.6 * gw / 10)
        golf_combined = max(0, min(10, golf_combined))
    else:
        golf_combined = row.get("golf_score") or 0

    w = {
        "attractiveness": 0.30,
        "weather": 0.30,
        "cost": 0.15,
        "busyness": 0.10,
        "quality": 0.15,
    }
    nf = 1.0 - golf_weight
    total = (
        attractiveness * w["attractiveness"] * nf
        + weather * w["weather"] * nf
        + cost_rel * w["cost"] * nf
        + busy_rel * w["busyness"] * nf
        + quality * w["quality"] * nf
        + golf_combined * golf_weight
    )

    safety = row.get("crime_safety") or 5.0
    if safety < 4.0:
        total = min(total, 2.0)
    elif safety < 6.0:
        total = min(total, 4.0)

    return round(total, 2)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

_REGION_COLS = """
    r.id, r.name as region_name, r.country_id, r.latitude, r.longitude,
    c.name as country_name, c.code as country_code,
    r.golf_score, r.nature_score, r.hiking_score, r.crime_safety,
    r.city_access, r.hotel_quality, r.tourism_level,
    r.flight_time_hours, r.flight_transfers
"""

_MONTH_COLS_TEMPLATE = """
    r.temp_{m}, r.temp_night_{m}, r.rain_{m}, r.humidity_{m},
    r.cost_relative_{m}, r.cost_absolute_{m},
    r.busyness_relative_{m}, r.busyness_absolute_{m},
    r.attractiveness_relative_{m},
    r.tips_{m}
"""


def _month_cols(month: str) -> str:
    return _MONTH_COLS_TEMPLATE.replace("{m}", month.lower())


def _row_to_dict(row) -> dict:
    return dict(row._mapping)


def search_destinations(
    db: Session,
    month: str,
    activity_focus: str = "general",
    max_flight_hours: Optional[float] = None,
    min_safety_score: float = 6.0,
    exclude_visited_never: bool = True,
    limit: int = 10,
) -> dict:
    """Search VacationMap regions for a month, apply filters, return scored results.

    Returns a dict with:
      - "results": list of scored destination dicts
      - "filtered_visited": list of high-scoring destinations excluded due to
        visit_again being 'not_soon' (so the AI can mention them)
    """
    m = month.lower()
    sql = f"""
        SELECT {_REGION_COLS}, {_month_cols(m)}
        FROM regions r
        JOIN countries c ON r.country_id = c.id
        WHERE 1=1
    """
    params: dict = {}

    if min_safety_score > 0:
        sql += " AND r.crime_safety >= :min_safety"
        params["min_safety"] = min_safety_score

    if max_flight_hours is not None:
        sql += " AND r.flight_time_hours IS NOT NULL AND r.flight_time_hours <= :max_fh"
        params["max_fh"] = max_flight_hours

    rows = db.execute(text(sql), params).fetchall()
    results = []
    filtered_visited = []

    # Get visited region IDs and their revisit preferences
    exclude_ids = {}  # never + not_soon: hard-exclude from results
    few_years_ids = {}  # few_years: include but annotate
    if exclude_visited_never:
        vrows = db.execute(
            text(
                "SELECT region_id, visit_again, rating, rating_summary "
                "FROM region_visits"
            )
        ).fetchall()
        for vr in vrows:
            if vr[1] in ("never", "not_soon"):
                exclude_ids[vr[0]] = {
                    "visit_again": vr[1],
                    "rating": vr[2],
                    "rating_summary": vr[3],
                }
            elif vr[1] == "few_years":
                few_years_ids[vr[0]] = {
                    "visit_again": vr[1],
                    "rating": vr[2],
                    "rating_summary": vr[3],
                }

    golf_weight = 0.3 if activity_focus == "golf" else 0.0

    for row in rows:
        d = _row_to_dict(row)
        score = _compute_score(d, m, golf_weight=golf_weight)
        d["total_score"] = score
        d["weather_score"] = _weather_comfort(
            d.get(f"temp_{m}"), d.get(f"rain_{m}"), d.get(f"humidity_{m}")
        )
        d["lookup_key"] = f"{d['country_code']}:{d['region_name']}"

        if d["id"] in exclude_ids:
            # Track high-scoring excluded destinations (not_soon only, skip never)
            visit_info = exclude_ids[d["id"]]
            if visit_info["visit_again"] == "not_soon":
                d["visit_again"] = visit_info
                filtered_visited.append(d)
            continue

        if d["id"] in few_years_ids:
            d["visit_again"] = few_years_ids[d["id"]]
        results.append(d)

    results.sort(key=lambda x: x["total_score"], reverse=True)
    filtered_visited.sort(key=lambda x: x["total_score"], reverse=True)

    return {
        "results": results[:limit],
        "filtered_visited": filtered_visited[:5],
    }


def get_destination_details(
    db: Session, region_lookup_key: str, month: str
) -> Optional[dict]:
    """Get full details for a region by its stable lookup key."""
    parts = region_lookup_key.split(":", 1)
    if len(parts) != 2:
        return None
    country_code, region_name = parts
    m = month.lower()

    sql = f"""
        SELECT {_REGION_COLS}, {_month_cols(m)}
        FROM regions r
        JOIN countries c ON r.country_id = c.id
        WHERE c.code = :cc AND r.name = :rn
        LIMIT 1
    """
    row = db.execute(text(sql), {"cc": country_code, "rn": region_name}).fetchone()
    if row is None:
        return None

    d = _row_to_dict(row)
    d["total_score"] = _compute_score(d, m)
    d["weather_score"] = _weather_comfort(
        d.get(f"temp_{m}"), d.get(f"rain_{m}"), d.get(f"humidity_{m}")
    )
    d["lookup_key"] = region_lookup_key

    # Check visit history
    visit = db.execute(
        text("""
            SELECT summary, rating, rating_summary, visit_again, visited_month, visited_year
            FROM region_visits WHERE region_id = :rid
        """),
        {"rid": d["id"]},
    ).fetchone()
    d["visit"] = dict(visit._mapping) if visit else None

    return d


def get_visit_history(db: Session) -> list[dict]:
    """Get all visited regions with their visit data."""
    rows = db.execute(text("""
        SELECT rv.*, r.name as region_name, c.name as country_name, c.code as country_code
        FROM region_visits rv
        JOIN regions r ON rv.region_id = r.id
        JOIN countries c ON r.country_id = c.id
        ORDER BY rv.visited_year DESC, rv.visited_month DESC
    """)).fetchall()
    return [_row_to_dict(r) for r in rows]
