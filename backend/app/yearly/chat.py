"""Claude integration for the yearly chatbot (F009 — year-options advisor).

The year-level chat helps the user generate and compare **year options**
(candidate whole-year arrangements). It does NOT pick destinations — each
slot can link to a concrete trip via the existing Trip Planner chat.

Reads `instructions.md` + `profile.md` fresh on every turn.
"""

import json
import os
from pathlib import Path

import anthropic
from sqlalchemy.orm import Session

from . import crud, models, tools
from ..anthropic_utils import create_message
from ..trips import crud as trips_crud, vacationmap
from ..trips.models import Conversation

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _read_md_file(filename: str) -> str:
    path = _PROJECT_ROOT / filename
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def _format_weights(weights: dict) -> str:
    if not weights:
        return "(none)"
    return ", ".join(f"{k}: {v}" for k, v in weights.items())


def _format_window(idx: int, w: dict) -> str:
    parts = []
    if w.get("label"):
        parts.append(str(w["label"]))
    parts.append(f"{w.get('start_date')} → {w.get('end_date')}")
    if w.get("duration_hint"):
        parts.append(f"~{w['duration_hint']}d")
    if w.get("constraints"):
        parts.append(f"constraints: {w['constraints']}")
    return f"- [{idx}] " + " · ".join(parts)


def _format_slot_block(slot: models.Slot) -> str:
    when = (
        f"{slot.start_year}-{slot.start_month:02d} → "
        f"{slot.end_year}-{slot.end_month:02d}"
    )
    if slot.exact_start_date and slot.exact_end_date:
        when += f" (exact: {slot.exact_start_date} → {slot.exact_end_date})"
    meta = [when, f"status: {slot.status}"]
    if slot.duration_days:
        meta.append(f"~{slot.duration_days}d")
    if slot.climate_hint:
        meta.append(f"climate: {slot.climate_hint}")
    if slot.window_index is not None:
        meta.append(f"window #{slot.window_index}")
    weights = crud._parse_weights(slot.activity_weights)
    if weights:
        meta.append(f"weights: {_format_weights(weights)}")
    if slot.trip_plan_id:
        meta.append(f"linked trip #{slot.trip_plan_id}")
    lines = [f"    * {slot.label or '(no label)'} — " + " | ".join(meta)]
    if slot.theme:
        lines.append(f"      Theme: {slot.theme}")
    if slot.status == "excluded" and slot.excluded_reason:
        lines.append(f"      EXCLUDED REASON: {slot.excluded_reason}")
    return "\n".join(lines)


def _format_option_block(option: models.YearOption) -> str:
    header = (
        f"### Option #{option.id} — {option.name} "
        f"[{option.status}, by {option.created_by}]"
    )
    lines = [header]
    if option.summary:
        lines.append(f"  _{option.summary}_")
    if option.status == "excluded" and option.excluded_reason:
        lines.append(f"  EXCLUDED REASON: {option.excluded_reason}")
    non_excluded_slots = [s for s in option.slots if s.status != "excluded"]
    excluded_slots = [s for s in option.slots if s.status == "excluded"]
    if not option.slots:
        lines.append("  (no slots yet)")
    else:
        for s in non_excluded_slots:
            lines.append(_format_slot_block(s))
        if excluded_slots:
            lines.append(
                f"  Excluded ideas in this option ({len(excluded_slots)}) "
                "— RESPECT THESE DECISIONS; do not re-propose:"
            )
            for s in excluded_slots:
                lines.append(_format_slot_block(s))
    return "\n".join(lines)


def _build_system_prompt(
    year_plan: models.YearPlan, trips_db: Session, vm_db: Session
) -> str:
    instructions = _read_md_file("instructions.md")
    profile = _read_md_file("profile.md")

    weights = crud._parse_weights(year_plan.activity_weights)
    windows = crud._parse_windows(year_plan.windows)

    windows_block = (
        "\n".join(_format_window(i, w) for i, w in enumerate(windows))
        if windows
        else "(no windows entered yet — ask the user which weeks/months they're available)"
    )

    options = list(year_plan.options)
    active_options = [o for o in options if o.status != "excluded"]
    excluded_options = [o for o in options if o.status == "excluded"]
    if options:
        option_blocks = "\n\n".join(
            _format_option_block(o) for o in active_options
        ) or ("(all options are excluded — wait for the user or propose a new one)")
        if excluded_options:
            option_blocks += (
                "\n\n### Excluded options "
                f"({len(excluded_options)}) — RESPECT THESE DECISIONS; do not re-propose:\n\n"
                + "\n\n".join(_format_option_block(o) for o in excluded_options)
            )
    else:
        option_blocks = "(no options yet — the user may want you to generate a few)"

    # Sibling plans for the same year (genuinely different contexts).
    sibling_plans = (
        trips_db.query(models.YearPlan)
        .filter(
            models.YearPlan.year == year_plan.year,
            models.YearPlan.id != year_plan.id,
        )
        .all()
    )
    siblings_line = (
        "Other YearPlans this year: "
        + ", ".join(f"{p.name} (#{p.id})" for p in sibling_plans)
        if sibling_plans
        else "No other YearPlans this year."
    )

    linked = crud.trips_linked_in_plan(trips_db, year_plan.id)
    loose = [
        t
        for t in crud.trips_in_year(trips_db, year_plan.year)
        if t.id not in {c.id for c in linked}
    ]
    linked_line = (
        "Trips linked via option slots: "
        + ", ".join(f"{t.name} [{t.target_month or '??'}]" for t in linked)
        if linked
        else "No trips linked via option slots yet."
    )
    loose_line = (
        "Unlinked existing trips that mention this year: "
        + ", ".join(f"{t.name} [{t.target_month or '??'}]" for t in loose)
        if loose
        else ""
    )

    year_context = f"""## Year Plan #{year_plan.id} — {year_plan.year} "{year_plan.name}"
**Status**: {year_plan.status}
**Intent**: {year_plan.intent or '(none)'}
**Activity targets**: {_format_weights(weights)}
{siblings_line}
{linked_line}
{loose_line}

### Open windows (shared across all Options — soft anchors, Options may shift dates slightly)
{windows_block}

### Options (candidate year arrangements)
{option_blocks}

### Your role — year-options advisor
You help the user compose and compare **candidate whole-year arrangements**
(YearOptions). Each Option is one alternative layout of the year with one
trip per open window. The user wants to see options side-by-side and pick.

**What you can do**
- Use `list_options` / `list_slots_in_option` / `get_visit_history` /
  `list_linked_trips` to read current state.
- `generate_year_option(name, summary, slots)` creates a whole new Option
  (typically one slot per open window) with status='proposed' slots.
- `propose_slot_in_option(option_id, …)` adds/refines a single slot.
- Discuss tradeoffs between options; suggest variations; help the user
  decide which to fork or mark chosen.

**What you must NOT do**
- Do not suggest specific destinations (cities, resorts, countries) at the
  year level. Use *themes* and *rough region direction* only ("warm
  beach", "Southern Africa safari", "Nordic nature"). Destination
  discovery happens in the trip chat once the user starts a trip from a
  slot.
- Do not edit the YearPlan's windows or user-level intent — user owns
  those.
- Do not create overlapping slots inside one Option.
- Do not pick the "winner" for the user — suggest, compare, and let them
  call it.
- **Respect excluded options and excluded trip ideas.** Do not re-propose
  anything the user has excluded (listed above with their reasons). The
  reasons often generalize — e.g., "too skewed toward one activity"
  applies to similar mono-theme options, not just the one excluded."""

    visits = vacationmap.get_visit_history(vm_db)
    if visits:
        visit_lines = []
        for v in visits:
            line = f"- {v.get('country_code')}:{v.get('region_name')}"
            if v.get("rating"):
                line += f" — rated {v['rating']}/10"
            if v.get("visit_again"):
                line += f", revisit: {v['visit_again']}"
            if v.get("visited_year"):
                line += f", visited {v['visited_year']}"
            visit_lines.append(line)
        visit_context = (
            "## Previously Visited Destinations\n"
            "Spot patterns and suggest complementary themes — do not "
            "recommend specific destinations at this level.\n\n"
            + "\n".join(visit_lines)
        )
    else:
        visit_context = ""

    parts = []
    if instructions:
        parts.append(instructions)
    if profile:
        parts.append(profile)
    parts.append(year_context)
    if visit_context:
        parts.append(visit_context)

    return "\n\n---\n\n".join(parts)


def _build_messages(conversation: Conversation) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in conversation.messages]


def handle_year_plan_chat_message(
    year_plan: models.YearPlan,
    conversation: Conversation,
    user_content: str,
    trips_db: Session,
    vm_db: Session,
):
    user_msg = trips_crud.add_message(trips_db, conversation.id, "user", user_content)
    trips_db.refresh(year_plan)
    trips_db.refresh(conversation)

    system_prompt = _build_system_prompt(year_plan, trips_db, vm_db)
    messages = _build_messages(conversation)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        error_text = (
            "I can't respond — ANTHROPIC_API_KEY is not configured. Set it and "
            "restart the server."
        )
        assistant_msg = trips_crud.add_message(
            trips_db, conversation.id, "assistant", error_text
        )
        return {
            "user_message": {
                "id": user_msg.id,
                "role": user_msg.role,
                "content": user_msg.content,
                "created_at": user_msg.created_at,
            },
            "assistant_message": {
                "id": assistant_msg.id,
                "role": assistant_msg.role,
                "content": assistant_msg.content,
                "created_at": assistant_msg.created_at,
            },
            "year_plan_state_changed": False,
        }

    client = anthropic.Anthropic(api_key=api_key)
    state_changed = False
    current_messages = list(messages)
    max_iterations = 10

    for _ in range(max_iterations):
        response = create_message(
            client,
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=system_prompt,
            messages=current_messages,
            tools=tools.YEARLY_TOOL_DEFINITIONS,
        )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = tools.execute_tool(
                        block.name, block.input, trips_db, vm_db, year_plan.id
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
                    if block.name in (
                        "generate_year_option",
                        "propose_slot_in_option",
                    ):
                        state_changed = True
            current_messages.append({"role": "assistant", "content": response.content})
            current_messages.append({"role": "user", "content": tool_results})
            if state_changed:
                trips_db.refresh(year_plan)
        else:
            text_parts = [b.text for b in response.content if hasattr(b, "text")]
            final_text = "\n".join(text_parts)
            break
    else:
        final_text = (
            "I apologize, but I wasn't able to complete my response. Please try "
            "again."
        )

    assistant_msg = trips_crud.add_message(
        trips_db, conversation.id, "assistant", final_text
    )

    return {
        "user_message": {
            "id": user_msg.id,
            "role": user_msg.role,
            "content": user_msg.content,
            "created_at": user_msg.created_at,
        },
        "assistant_message": {
            "id": assistant_msg.id,
            "role": assistant_msg.role,
            "content": assistant_msg.content,
            "created_at": assistant_msg.created_at,
        },
        "year_plan_state_changed": state_changed,
    }


_ = json
