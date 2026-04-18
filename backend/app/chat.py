"""Claude API integration: system prompt assembly, tool execution loop, message persistence."""

import os
from pathlib import Path

import anthropic
from sqlalchemy.orm import Session

from . import crud, models, schemas, vacationmap
from .tools import TOOL_DEFINITIONS, execute_tool

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _read_md_file(filename: str) -> str:
    """Read a markdown file from the project root. Returns empty string if missing."""
    path = _PROJECT_ROOT / filename
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def _build_system_prompt(trip: models.TripPlan, vm_db: Session) -> str:
    """Assemble the system prompt from instructions.md, profile.md, trip state, and visit history."""
    instructions = _read_md_file("instructions.md")
    profile = _read_md_file("profile.md")

    # Current trip state summary
    pending = []
    for s in trip.suggested:
        entry = f"- {s.destination_name}"
        entry += f" — {s.ai_reasoning}"
        pending.append(entry)

    shortlisted = []
    for s in trip.shortlisted:
        entry = f"- {s.destination_name}"
        entry += f" — {s.ai_reasoning}"
        if s.user_note:
            entry += f" | User note: {s.user_note}"
        shortlisted.append(entry)

    excluded = []
    for e in trip.excluded:
        entry = f"- {e.destination_name} — REASON: {e.reason}"
        if e.user_note:
            entry += f" | User note: {e.user_note}"
        excluded.append(entry)

    trip_context = f"""## Current Trip
**Name**: {trip.name}
**Description**: {trip.description}
**Target Month**: {trip.target_month or 'Not specified yet'}
**Status**: {trip.status}

### Pending Review ({len(pending)})
{chr(10).join(pending) if pending else 'None'}

### Shortlisted Destinations ({len(shortlisted)})
{chr(10).join(shortlisted) if shortlisted else 'None yet'}

### Excluded Destinations ({len(excluded)}) — RESPECT THESE DECISIONS. Read the reasons carefully — they reveal preferences that may apply to similar destinations too.
{chr(10).join(excluded) if excluded else 'None yet'}"""

    # Visit history from VacationMap
    visits = vacationmap.get_visit_history(vm_db)
    if visits:
        visit_lines = []
        for v in visits:
            line = f"- {v.get('country_code')}:{v.get('region_name')}"
            if v.get("rating"):
                line += f" — rated {v['rating']}/10"
            if v.get("visit_again"):
                line += f", revisit: {v['visit_again']}"
            if v.get("rating_summary"):
                line += f" ({v['rating_summary']})"
            visit_lines.append(line)
        visit_context = f"""## Previously Visited Destinations
The couple has visited these places before. "never" and "not_soon" destinations are filtered from search results. "few_years" destinations appear annotated — only suggest them if they're a truly exceptional fit. Always mention high-scoring filtered destinations to the user (e.g., "X and Y would fit well but are excluded due to recent visits").

{chr(10).join(visit_lines)}"""
    else:
        visit_context = ""

    parts = []
    if instructions:
        parts.append(instructions)
    if profile:
        parts.append(profile)
    parts.append(trip_context)
    if visit_context:
        parts.append(visit_context)

    return "\n\n---\n\n".join(parts)


def _build_messages(conversation: models.Conversation) -> list[dict]:
    """Build the messages list from persisted conversation history."""
    messages = []
    for msg in conversation.messages:
        messages.append({"role": msg.role, "content": msg.content})
    return messages


def handle_chat_message(
    trip: models.TripPlan,
    conversation: models.Conversation,
    user_content: str,
    trips_db: Session,
    vm_db: Session,
) -> schemas.ChatResponse:
    """Process a user message: persist, call Claude with tools, persist response."""

    # 1. Persist user message
    user_msg = crud.add_message(trips_db, conversation.id, "user", user_content)

    # Refresh to get latest state
    trips_db.refresh(trip)
    trips_db.refresh(conversation)

    # 2. Build system prompt and message history
    system_prompt = _build_system_prompt(trip, vm_db)
    messages = _build_messages(conversation)

    # 3. Call Claude with tool use loop
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # No API key — return a helpful error message
        error_content = (
            "I'm unable to respond because no Claude API key is configured. "
            "Please set the ANTHROPIC_API_KEY environment variable and restart the server."
        )
        assistant_msg = crud.add_message(
            trips_db, conversation.id, "assistant", error_content
        )
        return schemas.ChatResponse(
            user_message=schemas.MessageResponse(
                id=user_msg.id,
                role=user_msg.role,
                content=user_msg.content,
                created_at=user_msg.created_at,
            ),
            assistant_message=schemas.MessageResponse(
                id=assistant_msg.id,
                role=assistant_msg.role,
                content=assistant_msg.content,
                created_at=assistant_msg.created_at,
            ),
            trip_state_changed=False,
        )

    client = anthropic.Anthropic(api_key=api_key)
    trip_state_changed = False

    # Tool use loop: keep calling Claude until we get a final text response
    current_messages = list(messages)
    max_iterations = 10  # safety limit

    for _ in range(max_iterations):
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=system_prompt,
            messages=current_messages,
            tools=TOOL_DEFINITIONS,
        )

        # Check if Claude wants to use tools
        if response.stop_reason == "tool_use":
            # Process all tool calls in this response
            tool_results = []
            assistant_content = response.content  # list of content blocks

            for block in response.content:
                if block.type == "tool_use":
                    tool_result = execute_tool(
                        tool_name=block.name,
                        tool_input=block.input,
                        trips_db=trips_db,
                        vm_db=vm_db,
                        trip_id=trip.id,
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": tool_result,
                        }
                    )

                    # Track if trip state was modified
                    if block.name == "suggest_for_review":
                        trip_state_changed = True

            # Add assistant response and tool results to messages
            current_messages.append({"role": "assistant", "content": assistant_content})
            current_messages.append({"role": "user", "content": tool_results})

            # Refresh trip state after tool calls that modify it
            if trip_state_changed:
                trips_db.refresh(trip)

        else:
            # Final text response — extract text content
            text_parts = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
            final_text = "\n".join(text_parts)
            break
    else:
        final_text = (
            "I apologize, but I wasn't able to complete my response. Please try again."
        )

    # 4. Try to detect target month from the first message if not set
    if trip.target_month is None:
        _try_set_target_month(trip, trips_db)

    # 5. Persist assistant message
    assistant_msg = crud.add_message(trips_db, conversation.id, "assistant", final_text)

    return schemas.ChatResponse(
        user_message=schemas.MessageResponse(
            id=user_msg.id,
            role=user_msg.role,
            content=user_msg.content,
            created_at=user_msg.created_at,
        ),
        assistant_message=schemas.MessageResponse(
            id=assistant_msg.id,
            role=assistant_msg.role,
            content=assistant_msg.content,
            created_at=assistant_msg.created_at,
        ),
        trip_state_changed=trip_state_changed,
    )


_MONTH_KEYWORDS = {
    "january": "jan",
    "february": "feb",
    "march": "mar",
    "april": "apr",
    "may": "may",
    "june": "jun",
    "july": "jul",
    "august": "aug",
    "september": "sep",
    "october": "oct",
    "november": "nov",
    "december": "dec",
    "christmas": "christmas",
    "easter": "easter",
    "jan": "jan",
    "feb": "feb",
    "mar": "mar",
    "apr": "apr",
    "jun": "jun",
    "jul": "jul",
    "aug": "aug",
    "sep": "sep",
    "oct": "oct",
    "nov": "nov",
    "dec": "dec",
}


def _try_set_target_month(trip: models.TripPlan, db: Session):
    """Try to detect a target month from the trip description."""
    desc = trip.description.lower()
    for keyword, month_code in _MONTH_KEYWORDS.items():
        if keyword in desc:
            crud.set_target_month(db, trip.id, month_code)
            return
