"""CRUD operations for the trips database."""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from . import models, schemas

# normalize_name was used by the golf library CRUD that lived in this file
# pre-refactor; leaving the import out since trip CRUD doesn't use it.


def _utcnow():
    return datetime.now(timezone.utc)


# --- Trip Plans ---


def create_trip(db: Session, trip: schemas.TripCreate) -> models.TripPlan:
    db_trip = models.TripPlan(name=trip.name, description=trip.description)
    db.add(db_trip)
    db.commit()
    db.refresh(db_trip)
    return db_trip


def list_trips(db: Session) -> list[models.TripPlan]:
    return db.query(models.TripPlan).order_by(models.TripPlan.updated_at.desc()).all()


def get_trip(db: Session, trip_id: int) -> Optional[models.TripPlan]:
    return db.query(models.TripPlan).filter(models.TripPlan.id == trip_id).first()


def update_trip(
    db: Session, trip_id: int, update: schemas.TripUpdate
) -> Optional[models.TripPlan]:
    trip = get_trip(db, trip_id)
    if trip is None:
        return None
    if update.name is not None:
        trip.name = update.name
    if update.description is not None:
        trip.description = update.description
    if update.status is not None:
        trip.status = update.status
    if update.activity_weights is not None:
        trip.activity_weights = json.dumps(update.activity_weights)
    trip.updated_at = _utcnow()
    db.commit()
    db.refresh(trip)
    return trip


def delete_trip(db: Session, trip_id: int) -> bool:
    trip = get_trip(db, trip_id)
    if trip is None:
        return False
    # Conversations live in a polymorphic table with no FK back to trip_plans,
    # so cascade-on-delete has to be explicit here.
    convs = (
        db.query(models.Conversation)
        .filter(
            models.Conversation.owner_type == "trip",
            models.Conversation.owner_id == trip_id,
        )
        .all()
    )
    for conv in convs:
        db.delete(conv)
    db.delete(trip)
    db.commit()
    return True


def set_target_month(
    db: Session, trip_id: int, month: str
) -> Optional[models.TripPlan]:
    trip = get_trip(db, trip_id)
    if trip is None:
        return None
    trip.target_month = month
    trip.updated_at = _utcnow()
    db.commit()
    db.refresh(trip)
    return trip


# --- Suggested Destinations ---


def add_suggested(
    db: Session,
    trip_id: int,
    destination_name: str,
    ai_reasoning: str,
    region_lookup_key: Optional[str] = None,
    scores_snapshot: Optional[dict] = None,
    pre_filled_exclude_reason: Optional[str] = None,
    resort_id: Optional[int] = None,
    course_id: Optional[int] = None,
) -> models.SuggestedDestination:
    dest = models.SuggestedDestination(
        trip_id=trip_id,
        destination_name=destination_name,
        region_lookup_key=region_lookup_key,
        ai_reasoning=ai_reasoning,
        scores_snapshot=json.dumps(scores_snapshot) if scores_snapshot else None,
        pre_filled_exclude_reason=pre_filled_exclude_reason,
        resort_id=resort_id,
        course_id=course_id,
    )
    db.add(dest)
    trip = get_trip(db, trip_id)
    if trip:
        trip.updated_at = _utcnow()
    db.commit()
    db.refresh(dest)
    return dest


def get_suggested(
    db: Session, suggested_id: int
) -> Optional[models.SuggestedDestination]:
    return (
        db.query(models.SuggestedDestination)
        .filter(models.SuggestedDestination.id == suggested_id)
        .first()
    )


def move_suggested_to_shortlist(
    db: Session, suggested_id: int, user_note: Optional[str] = None
) -> Optional[models.ShortlistedDestination]:
    """Move a suggested destination to the shortlist."""
    sug = get_suggested(db, suggested_id)
    if sug is None:
        return None
    dest = models.ShortlistedDestination(
        trip_id=sug.trip_id,
        destination_name=sug.destination_name,
        region_lookup_key=sug.region_lookup_key,
        ai_reasoning=sug.ai_reasoning,
        scores_snapshot=sug.scores_snapshot,
        user_note=user_note,
        resort_id=sug.resort_id,
        course_id=sug.course_id,
    )
    db.add(dest)
    db.delete(sug)
    trip = get_trip(db, sug.trip_id)
    if trip:
        trip.updated_at = _utcnow()
    db.commit()
    db.refresh(dest)
    return dest


def move_suggested_to_excluded(
    db: Session, suggested_id: int, reason: str
) -> Optional[models.ExcludedDestination]:
    """Move a suggested destination to the excluded list."""
    sug = get_suggested(db, suggested_id)
    if sug is None:
        return None
    dest = models.ExcludedDestination(
        trip_id=sug.trip_id,
        destination_name=sug.destination_name,
        region_lookup_key=sug.region_lookup_key,
        reason=reason,
        ai_reasoning=sug.ai_reasoning,
        resort_id=sug.resort_id,
        course_id=sug.course_id,
    )
    db.add(dest)
    db.delete(sug)
    trip = get_trip(db, sug.trip_id)
    if trip:
        trip.updated_at = _utcnow()
    db.commit()
    db.refresh(dest)
    return dest


# --- Shortlisted Destinations ---


def add_shortlisted(
    db: Session,
    trip_id: int,
    destination_name: str,
    ai_reasoning: str,
    region_lookup_key: Optional[str] = None,
    scores_snapshot: Optional[dict] = None,
    user_note: Optional[str] = None,
) -> models.ShortlistedDestination:
    dest = models.ShortlistedDestination(
        trip_id=trip_id,
        destination_name=destination_name,
        region_lookup_key=region_lookup_key,
        ai_reasoning=ai_reasoning,
        scores_snapshot=json.dumps(scores_snapshot) if scores_snapshot else None,
        user_note=user_note,
    )
    db.add(dest)
    # Touch trip updated_at
    trip = get_trip(db, trip_id)
    if trip:
        trip.updated_at = _utcnow()
    db.commit()
    db.refresh(dest)
    return dest


# --- Excluded Destinations ---


def add_excluded(
    db: Session,
    trip_id: int,
    destination_name: str,
    reason: str,
    region_lookup_key: Optional[str] = None,
    ai_reasoning: Optional[str] = None,
) -> models.ExcludedDestination:
    dest = models.ExcludedDestination(
        trip_id=trip_id,
        destination_name=destination_name,
        region_lookup_key=region_lookup_key,
        reason=reason,
        ai_reasoning=ai_reasoning,
    )
    db.add(dest)
    trip = get_trip(db, trip_id)
    if trip:
        trip.updated_at = _utcnow()
    db.commit()
    db.refresh(dest)
    return dest


def get_shortlisted(
    db: Session, shortlisted_id: int
) -> Optional[models.ShortlistedDestination]:
    return (
        db.query(models.ShortlistedDestination)
        .filter(models.ShortlistedDestination.id == shortlisted_id)
        .first()
    )


def move_shortlisted_to_excluded(
    db: Session, shortlisted_id: int, reason: str
) -> Optional[models.ExcludedDestination]:
    sl = get_shortlisted(db, shortlisted_id)
    if sl is None:
        return None
    dest = models.ExcludedDestination(
        trip_id=sl.trip_id,
        destination_name=sl.destination_name,
        region_lookup_key=sl.region_lookup_key,
        reason=reason,
        ai_reasoning=sl.ai_reasoning,
        resort_id=sl.resort_id,
        course_id=sl.course_id,
    )
    db.add(dest)
    db.delete(sl)
    trip = get_trip(db, sl.trip_id)
    if trip:
        trip.updated_at = _utcnow()
    db.commit()
    db.refresh(dest)
    return dest


def move_shortlisted_to_suggested(
    db: Session, shortlisted_id: int
) -> Optional[models.SuggestedDestination]:
    sl = get_shortlisted(db, shortlisted_id)
    if sl is None:
        return None
    dest = models.SuggestedDestination(
        trip_id=sl.trip_id,
        destination_name=sl.destination_name,
        region_lookup_key=sl.region_lookup_key,
        ai_reasoning=sl.ai_reasoning,
        scores_snapshot=sl.scores_snapshot,
        user_note=sl.user_note,
        resort_id=sl.resort_id,
        course_id=sl.course_id,
    )
    db.add(dest)
    db.delete(sl)
    trip = get_trip(db, sl.trip_id)
    if trip:
        trip.updated_at = _utcnow()
    db.commit()
    db.refresh(dest)
    return dest


def get_excluded(db: Session, excluded_id: int) -> Optional[models.ExcludedDestination]:
    return (
        db.query(models.ExcludedDestination)
        .filter(models.ExcludedDestination.id == excluded_id)
        .first()
    )


def move_excluded_to_shortlist(
    db: Session, excluded_id: int, user_note: Optional[str] = None
) -> Optional[models.ShortlistedDestination]:
    """Reconsider an excluded destination — move it to the shortlist."""
    exc = get_excluded(db, excluded_id)
    if exc is None:
        return None
    dest = models.ShortlistedDestination(
        trip_id=exc.trip_id,
        destination_name=exc.destination_name,
        region_lookup_key=exc.region_lookup_key,
        ai_reasoning=exc.ai_reasoning or "",
        scores_snapshot=None,
        user_note=user_note,
        resort_id=exc.resort_id,
        course_id=exc.course_id,
    )
    db.add(dest)
    db.delete(exc)
    trip = get_trip(db, exc.trip_id)
    if trip:
        trip.updated_at = _utcnow()
    db.commit()
    db.refresh(dest)
    return dest


def delete_message(db: Session, message_id: int) -> bool:
    msg = (
        db.query(models.ConversationMessage)
        .filter(models.ConversationMessage.id == message_id)
        .first()
    )
    if msg is None:
        return False
    db.delete(msg)
    db.commit()
    return True


def update_message(
    db: Session, message_id: int, content: str
) -> Optional[models.ConversationMessage]:
    msg = (
        db.query(models.ConversationMessage)
        .filter(models.ConversationMessage.id == message_id)
        .first()
    )
    if msg is None:
        return None
    msg.content = content
    db.commit()
    db.refresh(msg)
    return msg


# --- Conversations ---


def create_conversation(
    db: Session, trip_id: int, name: str = "Main"
) -> models.Conversation:
    conv = models.Conversation(owner_type="trip", owner_id=trip_id, name=name)
    db.add(conv)
    trip = get_trip(db, trip_id)
    if trip:
        trip.updated_at = _utcnow()
    db.commit()
    db.refresh(conv)
    return conv


def get_conversation(
    db: Session, conversation_id: int
) -> Optional[models.Conversation]:
    return (
        db.query(models.Conversation)
        .filter(models.Conversation.id == conversation_id)
        .first()
    )


def list_conversations(db: Session, trip_id: int) -> list[models.Conversation]:
    return (
        db.query(models.Conversation)
        .filter(
            models.Conversation.owner_type == "trip",
            models.Conversation.owner_id == trip_id,
        )
        .order_by(models.Conversation.created_at.asc())
        .all()
    )


def archive_conversation(
    db: Session, conversation_id: int
) -> Optional[models.Conversation]:
    conv = get_conversation(db, conversation_id)
    if conv is None:
        return None
    conv.status = "archived"
    db.commit()
    db.refresh(conv)
    return conv


def unarchive_conversation(
    db: Session, conversation_id: int
) -> Optional[models.Conversation]:
    conv = get_conversation(db, conversation_id)
    if conv is None:
        return None
    conv.status = "active"
    db.commit()
    db.refresh(conv)
    return conv


def delete_conversation(db: Session, conversation_id: int) -> bool:
    conv = get_conversation(db, conversation_id)
    if conv is None:
        return False
    db.delete(conv)
    db.commit()
    return True


def rename_conversation(
    db: Session, conversation_id: int, name: str
) -> Optional[models.Conversation]:
    conv = get_conversation(db, conversation_id)
    if conv is None:
        return None
    conv.name = name
    db.commit()
    db.refresh(conv)
    return conv


# --- Conversation Messages ---


def add_message(
    db: Session, conversation_id: int, role: str, content: str
) -> models.ConversationMessage:
    """Persist a message. Keeps the legacy `trip_id` column populated for
    trip-owned conversations so older tooling that reads it still works."""
    conv = get_conversation(db, conversation_id)
    legacy_trip_id = conv.owner_id if conv and conv.owner_type == "trip" else None
    msg = models.ConversationMessage(
        conversation_id=conversation_id,
        trip_id=legacy_trip_id,
        role=role,
        content=content,
    )
    db.add(msg)
    if conv and conv.owner_type == "trip":
        trip = get_trip(db, conv.owner_id)
        if trip:
            trip.updated_at = _utcnow()
    db.commit()
    db.refresh(msg)
    return msg


def list_messages(
    db: Session, conversation_id: int
) -> list[models.ConversationMessage]:
    return (
        db.query(models.ConversationMessage)
        .filter(models.ConversationMessage.conversation_id == conversation_id)
        .order_by(models.ConversationMessage.created_at.asc())
        .all()
    )


# --- Helpers for schemas ---


def trip_to_summary(trip: models.TripPlan) -> schemas.TripSummary:
    try:
        weights = json.loads(trip.activity_weights) if trip.activity_weights else {}
    except (ValueError, TypeError):
        weights = {}
    return schemas.TripSummary(
        id=trip.id,
        name=trip.name,
        description=trip.description,
        target_month=trip.target_month,
        status=trip.status,
        suggested_count=len(trip.suggested),
        shortlisted_count=len(trip.shortlisted),
        excluded_count=len(trip.excluded),
        created_at=trip.created_at,
        updated_at=trip.updated_at,
        activity_weights=weights,
    )


def trip_to_detail(trip: models.TripPlan, db: Session) -> schemas.TripDetail:
    shortlisted = []
    for s in trip.shortlisted:
        scores = json.loads(s.scores_snapshot) if s.scores_snapshot else None
        shortlisted.append(
            schemas.ShortlistedDestinationResponse(
                id=s.id,
                destination_name=s.destination_name,
                region_lookup_key=s.region_lookup_key,
                ai_reasoning=s.ai_reasoning,
                scores_snapshot=scores,
                user_note=s.user_note,
                added_at=s.added_at,
                resort_id=s.resort_id,
                course_id=s.course_id,
            )
        )

    excluded = [
        schemas.ExcludedDestinationResponse(
            id=e.id,
            destination_name=e.destination_name,
            region_lookup_key=e.region_lookup_key,
            reason=e.reason,
            user_note=e.user_note,
            excluded_at=e.excluded_at,
            resort_id=e.resort_id,
            course_id=e.course_id,
        )
        for e in trip.excluded
    ]

    suggested = []
    for s in trip.suggested:
        scores = json.loads(s.scores_snapshot) if s.scores_snapshot else None
        suggested.append(
            schemas.SuggestedDestinationResponse(
                id=s.id,
                destination_name=s.destination_name,
                region_lookup_key=s.region_lookup_key,
                ai_reasoning=s.ai_reasoning,
                scores_snapshot=scores,
                user_note=s.user_note,
                pre_filled_exclude_reason=s.pre_filled_exclude_reason,
                suggested_at=s.suggested_at,
                resort_id=s.resort_id,
                course_id=s.course_id,
            )
        )

    convos = [
        schemas.ConversationSummary(
            id=c.id,
            name=c.name,
            status=c.status or "active",
            created_at=c.created_at,
            message_count=len(c.messages),
        )
        for c in list_conversations(db, trip.id)
    ]

    year_plan_link = _year_plan_link_for_trip(db, trip.id)

    return schemas.TripDetail(
        id=trip.id,
        name=trip.name,
        description=trip.description,
        target_month=trip.target_month,
        status=trip.status,
        created_at=trip.created_at,
        updated_at=trip.updated_at,
        conversations=convos,
        suggested=suggested,
        shortlisted=shortlisted,
        excluded=excluded,
        year_plan_link=year_plan_link,
    )


def _year_plan_link_for_trip(
    db: Session, trip_id: int
) -> Optional[schemas.TripYearPlanLink]:
    """Reverse lookup a trip → slot → option → year plan. Returns None if the
    trip isn't linked to a slot, or if the yearly package isn't importable."""
    try:
        from ..yearly import crud as yearly_crud, models as yearly_models
    except Exception:
        return None
    slot = yearly_crud.slot_for_trip(db, trip_id)
    if slot is None:
        return None
    option = (
        db.query(yearly_models.YearOption)
        .filter(yearly_models.YearOption.id == slot.year_option_id)
        .first()
    )
    if option is None:
        return None
    plan = (
        db.query(yearly_models.YearPlan)
        .filter(yearly_models.YearPlan.id == option.year_plan_id)
        .first()
    )
    if plan is None:
        return None
    window_label = None
    if slot.window_index is not None:
        windows = yearly_crud._parse_windows(plan.windows)
        if 0 <= slot.window_index < len(windows):
            w = windows[slot.window_index]
            window_label = w.get("label") or f"Window #{slot.window_index + 1}"
        else:
            window_label = f"Window #{slot.window_index + 1}"
    return schemas.TripYearPlanLink(
        year_plan_id=plan.id,
        year_plan_name=plan.name,
        year=plan.year,
        option_id=option.id,
        option_name=option.name,
        window_label=window_label,
        slot_id=slot.id,
        slot_label=slot.label,
    )
