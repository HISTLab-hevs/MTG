"""Rule endpoints: the rule list, custom-rule creation, and rule editing.

A custom rule is compiled to CEL by a background job, so most routes answer with
the rules panel and the page polls until the job lands. Editing a rule's CEL by
hand can leave facts undeclared: the reconcile route hands that to the same
worker and the dialog waits on it. Every route resolves the course through
get_owned_course, so a professor only ever reaches their own rules.
"""

import json
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_owned_course, require_member
from app.jobs.queue import enqueue_contract_completion, enqueue_rule_generation
from app.models import (
    Course,
    CourseRule,
    DocumentType,
    RuleCategory,
    RuleOrigin,
    RuleSeverity,
    RuleStatus,
    User,
)
from app.services.contracts import AGG_CHOICES, contract_fields
from app.services.rule_generation import cel_field_names
from app.services.rules import (
    CONTRACT_DRAFT_MAXLEN,
    RULE_CEL_MAXLEN,
    RULE_CHAR_LIMIT,
    RULE_TEXT_MAXLEN,
    RuleError,
    add_custom_rule,
    contract_with_aggs,
    ensure_default_rules,
    rules_by_type,
    rules_in_review,
    update_rule,
)
from app.templating import templates

router = APIRouter(prefix="/courses/{course_id}/rules")


def _rules_context(request: Request, course: Course, alert=None) -> dict:
    """Template context for the rules panel.

    Args:
        request: Current request.
        course: Course whose rules to show.
        alert: Optional (level, message) pair to display above the list.

    Returns:
        The context, including whether any rule is still being generated.
    """
    review = rules_in_review(course)
    return {
        "request": request,
        "course": course,
        "rule_sections": rules_by_type(course),
        "rules_in_review": review,
        "any_generating": any(r.is_generating for r in review),
        "alert": alert,
    }


def _panel(request: Request, course: Course, alert=None):
    """Render the rules panel alone, the answer to most of these routes."""
    return templates.TemplateResponse(
        request, "courses/_rules_panel.html", _rules_context(request, course, alert)
    )


@router.get("")
def rules_page(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Render the rules page, seeding the default rules on first visit."""
    course = get_owned_course(course_id, user, db)
    ensure_default_rules(db, course)
    context = _rules_context(request, course)
    context.update(
        {
            "user": user,
            "document_types": DocumentType.ALL,
            "rule_char_limit": RULE_CHAR_LIMIT,
            "active_tab": "rules",
        }
    )
    return templates.TemplateResponse(request, "courses/rules.html", context)


@router.get("/panel")
def rules_panel(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Polled by htmx while a rule is being generated."""
    course = get_owned_course(course_id, user, db)
    return _panel(request, course)


@router.post("/chat")
def add_rule_from_chat(
    course_id: int,
    request: Request,
    message: str = Form(...),
    document_type: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Create a custom rule from the professor's request and queue its compilation.

    Args:
        message: The requirement in natural language.
        document_type: The types the rule targets.
    """
    course = get_owned_course(course_id, user, db)
    ensure_default_rules(db, course)
    try:
        rule = add_custom_rule(db, course, message, document_type)
        enqueue_rule_generation(rule.id)
        # In eager mode the job already ran in its own session; reload so the
        # panel reflects the rule's new status.
        db.expire_all()
        alert = None
    except RuleError as exc:
        alert = ("error", str(exc))
    return _panel(request, course, alert)


def _get_owned_rule(
    course_id: int, rule_id: int, user: User, db: Session
) -> tuple[Course, CourseRule]:
    """Resolve a rule, checking it belongs to a course the user owns.

    Returns:
        The course and the rule.

    Raises:
        HTTPException: 404 when either is not the user's.
    """
    course = get_owned_course(course_id, user, db)
    rule = db.get(CourseRule, rule_id)
    if rule is None or rule.course_id != course.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    return course, rule


@router.post("/{rule_id}/accept")
def accept_rule(
    course_id: int,
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Accept a proposed rule, putting it into the active rule set.

    Raises:
        HTTPException: 400 when the rule is not awaiting review.
    """
    course, rule = _get_owned_rule(course_id, rule_id, user, db)
    if rule.status != RuleStatus.PROPOSED:
        raise HTTPException(status_code=400, detail="Only proposed rules can be accepted")
    rule.status = RuleStatus.READY
    db.commit()
    db.refresh(course)
    target = DocumentType.label(rule.document_type)
    return _panel(request, course, ("success", f"Rule added for {target.lower()}."))


@router.post("/{rule_id}/reject")
def reject_rule(
    course_id: int,
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Discard a proposed or failed rule before it joins the rule set.

    Raises:
        HTTPException: 400 when the rule is not under review.
    """
    course, rule = _get_owned_rule(course_id, rule_id, user, db)
    if rule.status not in (RuleStatus.PROPOSED, RuleStatus.FAILED):
        raise HTTPException(status_code=400, detail="Rule is not under review")
    db.delete(rule)
    db.commit()
    db.refresh(course)
    return _panel(request, course)


def _edit_modal(
    request: Request,
    course: Course,
    rule: CourseRule,
    error=None,
    notice=None,
    carry_contract=False,
    waiting=None,
):
    """Render the rule edit dialog.

    Args:
        request: Current request.
        course: Course owning the rule.
        rule: Rule being edited, possibly carrying unsaved display values.
        error: Validation message to show in the dialog.
        notice: Informational message to show in the dialog.
        carry_contract: Ride the shown contract along in a hidden field, so Save
            persists it.
        waiting: Field names the rules worker is declaring right now; the dialog
            shows an in-place loading state and polls until done.
    """
    return templates.TemplateResponse(
        request,
        "courses/_rule_edit_modal.html",
        {
            "course": course,
            "rule": rule,
            "severities": RuleSeverity.ALL,
            "categories": RuleCategory.ALL,
            "document_types": DocumentType.ALL,
            "text_maxlen": RULE_TEXT_MAXLEN,
            "cel_maxlen": RULE_CEL_MAXLEN,
            "fields": contract_fields(rule),
            "agg_choices": AGG_CHOICES,
            "error": error,
            "notice": notice,
            "waiting": waiting,
            "carry_contract": carry_contract,
        },
    )


@router.get("/{rule_id}/edit")
def edit_rule_form(
    course_id: int,
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Open the edit dialog for a custom rule: text, severity, CEL, aggregations.

    Raises:
        HTTPException: 400 for a default rule, which can only be disabled.
    """
    course, rule = _get_owned_rule(course_id, rule_id, user, db)
    if rule.origin != RuleOrigin.CUSTOM:
        raise HTTPException(status_code=400, detail="Default rules can only be disabled")
    if rule.edit_draft:
        # A reconcile is (or was) in flight, resume it instead of discarding:
        # still completing → the waiting dialog with its poller; done → the
        # result, delivered for review exactly as if the dialog never closed.
        _expire_stuck_draft(rule, db)
        return _draft_modal(request, course, rule, db)
    return _edit_modal(request, course, rule)


@router.post("/{rule_id}/edit")
def save_rule_edit(
    course_id: int,
    rule_id: int,
    request: Request,
    text: str = Form(...),
    severity: str = Form(...),
    category: str = Form(...),
    document_type: list[str] = Form(default=[]),
    cel: str = Form(...),
    agg_field: list[str] = Form(default=[]),
    agg_value: list[str] = Form(default=[]),
    contract_draft: str | None = Form(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Save an edit, re-opening the dialog with the message on a rejected one.

    Args:
        agg_field: Hidden field names of the editable aggregations.
        agg_value: Their chosen values, emitted side by side with agg_field.
        contract_draft: The contract shown in the dialog, when it is being
            carried along rather than read from the rule.
    """
    course, rule = _get_owned_rule(course_id, rule_id, user, db)
    aggs = dict(zip(agg_field, agg_value))
    try:
        update_rule(
            db, rule, text, severity, category, document_type, cel, aggs,
            contract_json=contract_draft,
        )
    except RuleError as exc:
        # 422 keeps the swap inside the modal (htmx is configured to swap on
        # 422), so the validation error shows and the form keeps the draft.
        rule.text, rule.severity, rule.category, rule.cel = text, severity, category, cel
        rule.document_type = DocumentType.normalize(
            [t for t in document_type if t in DocumentType.ALL]
        ) or rule.document_type
        rule.extraction_contract = contract_with_aggs(
            contract_draft or rule.extraction_contract, aggs
        )
        response = _edit_modal(
            request, course, rule, error=str(exc), carry_contract=bool(contract_draft)
        )
        response.status_code = 422
        return response
    db.refresh(course)
    response = _panel(request, course, ("success", "Rule updated."))
    # On success the form's default target is the modal; redirect the swap to the
    # rules list and tell the page to close the dialog.
    response.headers["HX-Retarget"] = "#rules-area"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = "rule-modal-close"
    return response


# A completion job is capped at RULE_JOB_TIMEOUT_SECONDS (5 min); a draft still
# "completing" well past that means the worker died mid-job. Flip it to failed
# so the rule never becomes uneditable behind an eternal spinner.
DRAFT_STUCK_SECONDS = 6 * 60


def _expire_stuck_draft(rule: CourseRule, db: Session) -> None:
    """Mark a draft failed when its worker died mid-job, leaving it completing."""
    try:
        draft = json.loads(rule.edit_draft)
    except (TypeError, ValueError):
        rule.edit_draft = None
        db.commit()
        return
    if (
        draft.get("status") == "completing"
        and time.time() - float(draft.get("queued_at") or 0) > DRAFT_STUCK_SECONDS
    ):
        draft["status"] = "failed"
        draft["error"] = (
            "The assistant took too long and the attempt was abandoned. "
            "Your edit is untouched, try again."
        )
        rule.edit_draft = json.dumps(draft)
        db.commit()


def _draft_modal(request: Request, course: Course, rule: CourseRule, db: Session):
    """Render the edit dialog from the rule's in-flight edit_draft.

    A draft still completing gives the waiting dialog with its poller; a ready
    one gives the completed contract, carried for review and Save; a failed one
    gives the error with the draft values kept. Terminal states clear the draft:
    it has been delivered, and from then on it lives in the form's hidden
    contract_draft field.

    Args:
        request: Current request.
        course: Course owning the rule.
        rule: Rule whose edit_draft to render.
        db: Session used to clear a delivered draft.
    """
    draft = json.loads(rule.edit_draft)
    rule.text = draft["text"]
    rule.severity = draft["severity"]
    rule.category = draft["category"]
    rule.document_type = DocumentType.normalize(draft["document_types"]) or rule.document_type
    rule.cel = draft["cel"]
    status = draft["status"]
    contract = draft.get("contract") if status == "ready" else draft["base_contract"]
    rule.extraction_contract = contract_with_aggs(json.dumps(contract), draft.get("aggs") or {})
    if status == "completing":
        kwargs: dict = {"waiting": ", ".join(draft["missing"])}
    elif status == "ready":
        kwargs = {
            "carry_contract": True,
            "notice": (
                "The assistant declared: " + ", ".join(draft["missing"]) + ". "
                "Review the new facts below (wording and combination), then save."
            ),
        }
    else:
        kwargs = {"error": draft.get("error", "Completion failed."), "carry_contract": True}
    response = _edit_modal(request, course, rule, **kwargs)  # renders eagerly
    if status != "completing":
        # Drop the delivered draft WITHOUT persisting the display-only attrs.
        db.expire(rule, ["text", "severity", "category", "document_type",
                         "cel", "extraction_contract"])
        rule.edit_draft = None
        db.commit()
    return response


@router.post("/{rule_id}/reconcile")
def reconcile_rule_contract(
    course_id: int,
    rule_id: int,
    request: Request,
    text: str = Form(...),
    severity: str = Form(...),
    category: str = Form(...),
    document_type: list[str] = Form(default=[]),
    cel: str = Form(...),
    agg_field: list[str] = Form(default=[]),
    agg_value: list[str] = Form(default=[]),
    contract_draft: str | None = Form(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Queue the assistant's declaration of the fields a hand-edited CEL reads.

    Nothing is saved to the rule itself: the draft goes to the rules worker
    (same queue as rule compilation, so the web process never blocks on the
    model), the dialog shows a small in-place loading state and polls; the
    completed contract comes back for review and only Save persists it.

    Args:
        agg_field: Hidden field names of the editable aggregations.
        agg_value: Their chosen values, emitted side by side with agg_field.
        contract_draft: The contract shown in the dialog, when it is being
            carried along rather than read from the rule.

    Raises:
        HTTPException: 400 for a default rule, which can only be disabled.
    """
    from app.services.rule_generation import validate_cel

    course, rule = _get_owned_rule(course_id, rule_id, user, db)
    if rule.origin != RuleOrigin.CUSTOM:
        raise HTTPException(status_code=400, detail="Default rules can only be disabled")
    if contract_draft is not None and len(contract_draft) > CONTRACT_DRAFT_MAXLEN:
        response = _edit_modal(
            request, course, rule,
            error=f"The contract draft must be {CONTRACT_DRAFT_MAXLEN} characters or fewer.",
        )
        response.status_code = 422
        return response

    cel = cel.strip()
    base_contract = contract_draft or rule.extraction_contract or "[]"
    try:
        entries = json.loads(base_contract)
        if not isinstance(entries, list):
            entries = []
    except ValueError:
        entries = []
    chosen = [t for t in document_type if t in DocumentType.ALL]
    aggs = dict(zip(agg_field, agg_value))
    missing = sorted(cel_field_names(cel) - {e.get("name") for e in entries})

    cel_error = validate_cel(cel) if cel else "The CEL expression cannot be empty."
    if not cel_error and len(cel) > RULE_CEL_MAXLEN:
        cel_error = f"The CEL expression must be {RULE_CEL_MAXLEN} characters or fewer."
    if cel_error or not missing:
        # Nothing for the model to do: fix the syntax first, or everything the
        # expression reads is already declared.
        rule.text, rule.severity, rule.category, rule.cel = text, severity, category, cel
        rule.document_type = DocumentType.normalize(chosen) or rule.document_type
        rule.extraction_contract = contract_with_aggs(base_contract, aggs)
        if cel_error:
            response = _edit_modal(
                request, course, rule, error=cel_error,
                carry_contract=bool(contract_draft),
            )
            response.status_code = 422
            return response
        return _edit_modal(
            request, course, rule,
            notice="Every fact the expression reads is already declared.",
            carry_contract=bool(contract_draft),
        )

    rule.edit_draft = json.dumps({
        "status": "completing",
        "queued_at": time.time(),
        "text": text,
        "severity": severity,
        "category": category,
        "document_types": chosen,
        "cel": cel,
        "aggs": aggs,
        "base_contract": entries,
        "missing": missing,
    })
    db.commit()
    enqueue_contract_completion(rule.id)
    # In eager mode the job already ran in its own session; reload so the
    # dialog reflects the outcome immediately instead of showing the spinner.
    db.expire_all()
    rule = db.get(CourseRule, rule_id)
    return _draft_modal(request, course, rule, db)


@router.post("/{rule_id}/edit/cancel")
def cancel_edit_draft(
    course_id: int,
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Stop a reconcile in flight: drop the draft and show the rule as stored.

    The worker job cannot be killed mid-model-call, but it re-checks the draft
    before writing its result (queued_at match), so a cancelled draft's late
    answer is discarded, functionally the job is dead the moment this commits.
    """
    course, rule = _get_owned_rule(course_id, rule_id, user, db)
    rule.edit_draft = None
    db.commit()
    db.refresh(rule)
    return _edit_modal(request, course, rule)


@router.get("/{rule_id}/edit/draft")
def poll_edit_draft(
    course_id: int,
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Polled by the waiting dialog; 204 (no swap) while the worker runs."""
    course, rule = _get_owned_rule(course_id, rule_id, user, db)
    if not rule.edit_draft:
        return Response(status_code=204)
    _expire_stuck_draft(rule, db)
    if rule.edit_draft and json.loads(rule.edit_draft).get("status") == "completing":
        return Response(status_code=204)
    return _draft_modal(request, course, rule, db)


@router.post("/{rule_id}/toggle")
def toggle_rule(
    course_id: int,
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Enable or disable a rule, the only change allowed on a default one."""
    course, rule = _get_owned_rule(course_id, rule_id, user, db)
    rule.is_active = not rule.is_active
    db.commit()
    db.refresh(course)
    return _panel(request, course)


@router.post("/{rule_id}/delete")
def delete_rule(
    course_id: int,
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Delete a custom rule.

    Raises:
        HTTPException: 400 for a default rule, which can only be disabled.
    """
    course, rule = _get_owned_rule(course_id, rule_id, user, db)
    if rule.origin != RuleOrigin.CUSTOM:
        raise HTTPException(status_code=400, detail="Default rules can only be disabled")
    db.delete(rule)
    db.commit()
    db.refresh(course)
    return _panel(request, course)
