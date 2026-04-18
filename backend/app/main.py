from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from . import crud, schemas
from .database import init_trips_db, get_trips_db, get_vacationmap_db

app = FastAPI(title="Trip Planner Chatbot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_trips_db()


# ---------------------------------------------------------------------------
# Trip endpoints
# ---------------------------------------------------------------------------


@app.post("/api/trips", response_model=schemas.TripSummary, status_code=201)
def create_trip(trip: schemas.TripCreate, db: Session = Depends(get_trips_db)):
    db_trip = crud.create_trip(db, trip)
    return crud.trip_to_summary(db_trip)


@app.get("/api/trips", response_model=list[schemas.TripSummary])
def list_trips(db: Session = Depends(get_trips_db)):
    trips = crud.list_trips(db)
    return [crud.trip_to_summary(t) for t in trips]


@app.get("/api/trips/{trip_id}", response_model=schemas.TripDetail)
def get_trip(trip_id: int, db: Session = Depends(get_trips_db)):
    trip = crud.get_trip(db, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return crud.trip_to_detail(trip)


@app.put("/api/trips/{trip_id}", response_model=schemas.TripSummary)
def update_trip(
    trip_id: int, update: schemas.TripUpdate, db: Session = Depends(get_trips_db)
):
    trip = crud.update_trip(db, trip_id, update)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return crud.trip_to_summary(trip)


@app.delete("/api/trips/{trip_id}", status_code=204)
def delete_trip(trip_id: int, db: Session = Depends(get_trips_db)):
    if not crud.delete_trip(db, trip_id):
        raise HTTPException(status_code=404, detail="Trip not found")


# ---------------------------------------------------------------------------
# VacationMap region search (for linking)
# ---------------------------------------------------------------------------


@app.get("/api/vacationmap/regions/{lookup_key:path}/details")
def get_region_details(
    lookup_key: str,
    month: str = "jun",
    db: Session = Depends(get_vacationmap_db),
):
    """Get full VacationMap details for a region."""
    from . import vacationmap as vm

    details = vm.get_destination_details(db, lookup_key, month)
    if details is None:
        raise HTTPException(status_code=404, detail="Region not found")

    m = month.lower()
    return {
        "destination": f"{details['region_name']}, {details['country_name']}",
        "lookup_key": lookup_key,
        "total_score": details.get("total_score"),
        "weather_score": round(details.get("weather_score", 0), 1),
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


@app.get("/api/vacationmap/regions/search")
def search_regions(q: str = "", db: Session = Depends(get_vacationmap_db)):
    """Search VacationMap regions by name for autocomplete."""
    from sqlalchemy import text

    if len(q) < 2:
        return []
    rows = db.execute(
        text(
            "SELECT r.name as region_name, c.name as country_name, c.code as country_code "
            "FROM regions r JOIN countries c ON r.country_id = c.id "
            "WHERE r.name LIKE :q OR c.name LIKE :q "
            "ORDER BY r.name LIMIT 20"
        ),
        {"q": f"%{q}%"},
    ).fetchall()
    return [
        {
            "lookup_key": f"{r.country_code}:{r.region_name}",
            "label": f"{r.region_name}, {r.country_name}",
        }
        for r in rows
    ]


class LinkRegionBody(schemas.BaseModel):
    lookup_key: str


@app.post("/api/trips/{trip_id}/suggested/{dest_id}/link")
def link_suggested_region(
    trip_id: int,
    dest_id: int,
    body: LinkRegionBody,
    trips_db: Session = Depends(get_trips_db),
    vm_db: Session = Depends(get_vacationmap_db),
):
    """Link a suggested destination to a VacationMap region and resolve scores."""
    dest = crud.get_suggested(trips_db, dest_id)
    if dest is None:
        raise HTTPException(status_code=404, detail="Not found")
    from .tools import _build_scores_from_db

    trip = crud.get_trip(trips_db, trip_id)
    month = trip.target_month or "jun"
    scores = _build_scores_from_db(vm_db, body.lookup_key, month)
    dest.region_lookup_key = body.lookup_key
    if scores:
        import json

        dest.scores_snapshot = json.dumps(scores)
    trips_db.commit()
    return {"status": "linked", "scores_resolved": scores is not None}


@app.post("/api/trips/{trip_id}/shortlisted/{dest_id}/link")
def link_shortlisted_region(
    trip_id: int,
    dest_id: int,
    body: LinkRegionBody,
    trips_db: Session = Depends(get_trips_db),
    vm_db: Session = Depends(get_vacationmap_db),
):
    """Link a shortlisted destination to a VacationMap region and resolve scores."""
    dest = (
        trips_db.query(crud.models.ShortlistedDestination).filter_by(id=dest_id).first()
    )
    if dest is None:
        raise HTTPException(status_code=404, detail="Not found")
    from .tools import _build_scores_from_db

    trip = crud.get_trip(trips_db, trip_id)
    month = trip.target_month or "jun"
    scores = _build_scores_from_db(vm_db, body.lookup_key, month)
    dest.region_lookup_key = body.lookup_key
    if scores:
        import json

        dest.scores_snapshot = json.dumps(scores)
    trips_db.commit()
    return {"status": "linked", "scores_resolved": scores is not None}


# ---------------------------------------------------------------------------
# Suggested destination actions
# ---------------------------------------------------------------------------


class ShortlistBody(schemas.BaseModel):
    user_note: str | None = None


class ExcludeBody(schemas.BaseModel):
    reason: str


@app.post("/api/trips/{trip_id}/suggested/{suggested_id}/shortlist")
def shortlist_suggested(
    trip_id: int,
    suggested_id: int,
    body: ShortlistBody,
    db: Session = Depends(get_trips_db),
):
    dest = crud.move_suggested_to_shortlist(db, suggested_id, body.user_note)
    if dest is None:
        raise HTTPException(status_code=404, detail="Suggested destination not found")
    return {"status": "shortlisted", "id": dest.id}


@app.post("/api/trips/{trip_id}/suggested/{suggested_id}/exclude")
def exclude_suggested(
    trip_id: int,
    suggested_id: int,
    body: ExcludeBody,
    db: Session = Depends(get_trips_db),
):
    dest = crud.move_suggested_to_excluded(db, suggested_id, body.reason)
    if dest is None:
        raise HTTPException(status_code=404, detail="Suggested destination not found")
    return {"status": "excluded", "id": dest.id}


class NoteBody(schemas.BaseModel):
    user_note: str | None = None


@app.put("/api/trips/{trip_id}/suggested/{dest_id}/note")
def update_suggested_note(
    trip_id: int,
    dest_id: int,
    body: NoteBody,
    db: Session = Depends(get_trips_db),
):
    dest = crud.get_suggested(db, dest_id)
    if dest is None:
        raise HTTPException(status_code=404, detail="Not found")
    dest.user_note = body.user_note
    db.commit()
    return {"status": "ok"}


@app.put("/api/trips/{trip_id}/shortlisted/{dest_id}/note")
def update_shortlisted_note(
    trip_id: int,
    dest_id: int,
    body: NoteBody,
    db: Session = Depends(get_trips_db),
):
    dest = db.query(crud.models.ShortlistedDestination).filter_by(id=dest_id).first()
    if dest is None:
        raise HTTPException(status_code=404, detail="Not found")
    dest.user_note = body.user_note
    db.commit()
    return {"status": "ok"}


@app.put("/api/trips/{trip_id}/excluded/{dest_id}/note")
def update_excluded_note(
    trip_id: int,
    dest_id: int,
    body: NoteBody,
    db: Session = Depends(get_trips_db),
):
    dest = crud.get_excluded(db, dest_id)
    if dest is None:
        raise HTTPException(status_code=404, detail="Not found")
    dest.user_note = body.user_note
    db.commit()
    return {"status": "ok"}


@app.post("/api/trips/{trip_id}/shortlisted/{shortlisted_id}/exclude")
def exclude_shortlisted(
    trip_id: int,
    shortlisted_id: int,
    body: ExcludeBody,
    db: Session = Depends(get_trips_db),
):
    dest = crud.move_shortlisted_to_excluded(db, shortlisted_id, body.reason)
    if dest is None:
        raise HTTPException(status_code=404, detail="Shortlisted destination not found")
    return {"status": "excluded", "id": dest.id}


@app.post("/api/trips/{trip_id}/shortlisted/{shortlisted_id}/unreview")
def unreview_shortlisted(
    trip_id: int,
    shortlisted_id: int,
    db: Session = Depends(get_trips_db),
):
    dest = crud.move_shortlisted_to_suggested(db, shortlisted_id)
    if dest is None:
        raise HTTPException(status_code=404, detail="Shortlisted destination not found")
    return {"status": "moved_to_review", "id": dest.id}


@app.post("/api/trips/{trip_id}/excluded/{excluded_id}/reconsider")
def reconsider_excluded(
    trip_id: int,
    excluded_id: int,
    body: ShortlistBody,
    db: Session = Depends(get_trips_db),
):
    dest = crud.move_excluded_to_shortlist(db, excluded_id, body.user_note)
    if dest is None:
        raise HTTPException(status_code=404, detail="Excluded destination not found")
    return {"status": "shortlisted", "id": dest.id}


# ---------------------------------------------------------------------------
# Message management endpoints
# ---------------------------------------------------------------------------


class MessageUpdate(schemas.BaseModel):
    content: str


@app.delete("/api/messages/{message_id}", status_code=204)
def delete_message(message_id: int, db: Session = Depends(get_trips_db)):
    if not crud.delete_message(db, message_id):
        raise HTTPException(status_code=404, detail="Message not found")


@app.put("/api/messages/{message_id}", response_model=schemas.MessageResponse)
def update_message(
    message_id: int,
    body: MessageUpdate,
    db: Session = Depends(get_trips_db),
):
    msg = crud.update_message(db, message_id, body.content)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


# ---------------------------------------------------------------------------
# Conversation endpoints
# ---------------------------------------------------------------------------


@app.post("/api/trips/{trip_id}/conversations", status_code=201)
def create_conversation(
    trip_id: int,
    body: schemas.ConversationCreate,
    db: Session = Depends(get_trips_db),
):
    trip = crud.get_trip(db, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    conv = crud.create_conversation(db, trip_id, body.name)
    return {"id": conv.id, "name": conv.name}


@app.post("/api/conversations/{conv_id}/archive")
def archive_conversation(conv_id: int, db: Session = Depends(get_trips_db)):
    conv = crud.archive_conversation(db, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"id": conv.id, "status": conv.status}


@app.post("/api/conversations/{conv_id}/unarchive")
def unarchive_conversation(conv_id: int, db: Session = Depends(get_trips_db)):
    conv = crud.unarchive_conversation(db, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"id": conv.id, "status": conv.status}


@app.delete("/api/conversations/{conv_id}", status_code=204)
def delete_conversation(conv_id: int, db: Session = Depends(get_trips_db)):
    if not crud.delete_conversation(db, conv_id):
        raise HTTPException(status_code=404, detail="Conversation not found")


@app.put("/api/conversations/{conv_id}/rename")
def rename_conversation(
    conv_id: int,
    body: schemas.ConversationCreate,
    db: Session = Depends(get_trips_db),
):
    conv = crud.rename_conversation(db, conv_id, body.name)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"id": conv.id, "name": conv.name}


# ---------------------------------------------------------------------------
# Message / Chat endpoints (conversation-scoped)
# ---------------------------------------------------------------------------


@app.get(
    "/api/conversations/{conv_id}/messages",
    response_model=list[schemas.MessageResponse],
)
def get_messages(conv_id: int, db: Session = Depends(get_trips_db)):
    conv = crud.get_conversation(db, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return crud.list_messages(db, conv_id)


@app.post("/api/conversations/{conv_id}/messages", response_model=schemas.ChatResponse)
def send_message(
    conv_id: int,
    message: schemas.MessageCreate,
    trips_db: Session = Depends(get_trips_db),
    vm_db: Session = Depends(get_vacationmap_db),
):
    conv = crud.get_conversation(trips_db, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    trip = crud.get_trip(trips_db, conv.trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")

    from .chat import handle_chat_message

    return handle_chat_message(trip, conv, message.content, trips_db, vm_db)


# ---------------------------------------------------------------------------
# Static files — serve frontend
# ---------------------------------------------------------------------------

_frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
if _frontend_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")

    @app.get("/")
    def serve_index():
        return FileResponse(str(_frontend_dir / "index.html"))
