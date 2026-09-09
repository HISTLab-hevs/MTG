"""The rule set of a course: seeding, editing, and rule identity codes.

The bulk covers the edit flow, where changing a CEL can leave the extraction
contract out of step: the contract is reconciled by a background model call,
and the draft holding it must survive polling, cancelling, and a concurrent
save without ever resurrecting itself.
"""

import json

from sqlalchemy import select

from app.models import CourseRule, RuleOrigin, RuleStatus
from app.services.rules import load_default_rules
from tests.conftest import login


def test_rules_page_seeds_default_rules(client, db, member, course):
    login(client, "user@example.com")
    response = client.get(f"/courses/{course.id}/rules")
    assert response.status_code == 200

    expected = len(load_default_rules())
    rules = db.execute(select(CourseRule)).scalars().all()
    assert len(rules) == expected
    assert all(r.origin == RuleOrigin.DEFAULT for r in rules)
    # Visiting the page again must not duplicate the defaults.
    client.get(f"/courses/{course.id}/rules")
    assert len(db.execute(select(CourseRule)).scalars().all()) == expected


def test_chat_message_creates_custom_rule(client, db, member, course):
    login(client, "user@example.com")
    response = client.post(
        f"/courses/{course.id}/rules/chat",
        data={
            "message": "The syllabus must list the exam dates.",
            "document_type": "syllabus",
        },
    )
    assert response.status_code == 200

    # With JOB_EAGER the generation job ran inline: the stubbed LLM answer
    # became a proposal awaiting review.
    rule = db.execute(
        select(CourseRule).where(CourseRule.origin == RuleOrigin.CUSTOM)
    ).scalar_one()
    assert rule.document_type == "syllabus"
    assert rule.source_prompt == "The syllabus must list the exam dates."
    assert rule.status == "proposed"
    assert rule.cel
    assert "Accept" in response.text


def test_chat_rule_can_target_several_types(client, db, member, course):
    login(client, "user@example.com")
    client.post(
        f"/courses/{course.id}/rules/chat",
        data={
            "message": "Every graded task must publish its grading criteria.",
            # Repeated fields = the multi-type checkbox picker; stored canonically.
            "document_type": ["rubric", "assessment"],
        },
    )
    rule = db.execute(
        select(CourseRule).where(CourseRule.origin == RuleOrigin.CUSTOM)
    ).scalar_one()
    assert rule.document_type == "assessment,rubric"


def test_chat_rule_requires_at_least_one_type(client, db, member, course):
    login(client, "user@example.com")
    response = client.post(
        f"/courses/{course.id}/rules/chat",
        data={"message": "All documents must mention the course code.", "document_type": ""},
    )
    assert "at least one document type" in response.text
    custom = db.execute(
        select(CourseRule).where(CourseRule.origin == RuleOrigin.CUSTOM)
    ).scalars().all()
    assert custom == []


def test_invalid_rule_input_is_rejected(client, db, member, course):
    login(client, "user@example.com")
    response = client.post(
        f"/courses/{course.id}/rules/chat",
        data={"message": "   ", "document_type": "syllabus"},
    )
    assert "Describe the rule" in response.text
    # An unrecognised type is dropped, leaving no valid selection.
    response = client.post(
        f"/courses/{course.id}/rules/chat",
        data={"message": "Some rule", "document_type": "poster"},
    )
    assert "at least one document type" in response.text
    custom = db.execute(
        select(CourseRule).where(CourseRule.origin == RuleOrigin.CUSTOM)
    ).scalars().all()
    assert custom == []


def test_rule_text_over_char_limit_is_rejected(client, db, member, course):
    from app.services.rules import RULE_CHAR_LIMIT

    login(client, "user@example.com")
    response = client.post(
        f"/courses/{course.id}/rules/chat",
        data={"message": "x" * (RULE_CHAR_LIMIT + 1), "document_type": "syllabus"},
    )
    assert "characters or fewer" in response.text
    custom = db.execute(
        select(CourseRule).where(CourseRule.origin == RuleOrigin.CUSTOM)
    ).scalars().all()
    assert custom == []


def test_default_rules_can_be_toggled_but_not_deleted(client, db, member, course):
    login(client, "user@example.com")
    client.get(f"/courses/{course.id}/rules")
    rule = db.execute(select(CourseRule)).scalars().first()

    client.post(f"/courses/{course.id}/rules/{rule.id}/toggle")
    db.refresh(rule)
    assert not rule.is_active

    response = client.post(f"/courses/{course.id}/rules/{rule.id}/delete")
    assert response.status_code == 400
    assert db.get(CourseRule, rule.id) is not None


def test_custom_rules_can_be_deleted(client, db, member, course):
    login(client, "user@example.com")
    client.post(
        f"/courses/{course.id}/rules/chat",
        data={"message": "Temporary rule", "document_type": "slide"},
    )
    rule_id = db.execute(
        select(CourseRule.id).where(CourseRule.origin == RuleOrigin.CUSTOM)
    ).scalar_one()
    client.post(f"/courses/{course.id}/rules/{rule_id}/delete")
    db.expire_all()  # the delete ran in the request's own session
    assert db.get(CourseRule, rule_id) is None


def test_edit_can_rescope_a_rule_to_different_types(client, db, member, course):
    """Editing a custom rule can change which document types it applies to; the
    stored type set is re-normalised and the rule moves to those sections."""
    login(client, "user@example.com")
    client.post(
        f"/courses/{course.id}/rules/chat",
        data={
            "message": "Reward evidence and sources, not only clarity.",
            "document_type": ["assessment"],
        },
    )
    rule = db.execute(
        select(CourseRule).where(CourseRule.origin == RuleOrigin.CUSTOM)
    ).scalar_one()
    client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": rule.text,
            "severity": rule.severity,
            "category": rule.category,
            "document_type": ["assessment", "rubric"],
            "cel": rule.cel,
        },
    )
    db.refresh(rule)
    assert rule.document_type == "assessment,rubric"


def _boolean_field_rule(db, course) -> CourseRule:
    """A ready custom rule with a boolean field, whose agg offers a real choice."""
    rule = CourseRule(
        course_id=course.id,
        document_type="slide",
        text="Every formula must be followed by a worked example.",
        cel="size(doc.formula_pages) == 0",
        extraction_contract=json.dumps(
            [
                {
                    "name": "formula_pages",
                    "type": "boolean",
                    "agg": "pages",
                    "instruction": "True if THIS page introduces a formula.",
                }
            ]
        ),
        origin=RuleOrigin.CUSTOM,
        status=RuleStatus.READY,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def test_edit_can_change_a_field_aggregation(client, db, member, course):
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": rule.text,
            "severity": rule.severity,
            "category": rule.category,
            "document_type": ["slide"],
            # `pages` yields a list, `any` a plain boolean: the CEL moves with it.
            "cel": "!doc.formula_pages",
            "agg_field": ["formula_pages"],
            "agg_value": ["any"],
        },
    )
    assert response.status_code == 200
    db.refresh(rule)
    (entry,) = json.loads(rule.extraction_contract)
    assert entry["agg"] == "any"
    assert rule.cel == "!doc.formula_pages"


def test_edit_rejects_an_aggregation_the_field_type_does_not_allow(
    client, db, member, course
):
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": rule.text,
            "severity": rule.severity,
            "category": rule.category,
            "document_type": ["slide"],
            "cel": rule.cel,
            "agg_field": ["formula_pages"],
            "agg_value": ["union"],  # list-only: iterating a boolean would crash
        },
    )
    assert response.status_code == 422
    assert "not a valid way to combine" in response.text
    db.expire_all()
    (entry,) = json.loads(db.get(CourseRule, rule.id).extraction_contract)
    assert entry["agg"] == "pages"


def test_edit_drops_contract_fields_the_cel_no_longer_reads(client, db, member, course):
    """Deleting a field from the CEL by hand silently drops its declaration —
    deterministic, no model involved."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    entries = json.loads(rule.extraction_contract)
    entries.append({
        "name": "has_summary", "type": "boolean",
        "agg": "any", "instruction": "True if THIS page shows a summary.",
    })
    rule.cel = "size(doc.formula_pages) == 0 && doc.has_summary"
    rule.extraction_contract = json.dumps(entries)
    db.commit()
    client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": rule.text, "severity": rule.severity, "category": rule.category,
            "document_type": ["slide"], "cel": "size(doc.formula_pages) == 0",
        },
    )
    db.refresh(rule)
    (entry,) = json.loads(rule.extraction_contract)
    assert entry["name"] == "formula_pages"


def test_edit_accepts_page_count_without_declaring_it(client, db, member, course):
    """`doc.page_count` is engine-supplied: readable by a hand-written CEL, and
    never treated as an undeclared field needing reconciliation."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": rule.text,
            "severity": rule.severity,
            "category": rule.category,
            "document_type": ["slide"],
            "cel": "doc.formula_pages.exists(p, p == doc.page_count)",
        },
    )
    assert response.status_code == 200
    assert "does not declare" not in response.text
    db.refresh(rule)
    assert rule.cel == "doc.formula_pages.exists(p, p == doc.page_count)"
    # The built-in must not have been persisted as a contract field.
    names = [e["name"] for e in json.loads(rule.extraction_contract)]
    assert names == ["formula_pages"]


def test_edit_rejects_declaring_page_count_as_a_contract_field(
    client, db, member, course
):
    """A tampered draft may not shadow the built-in with an extracted field."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": rule.text,
            "severity": rule.severity,
            "category": rule.category,
            "document_type": ["slide"],
            "cel": "doc.formula_pages.exists(p, p == doc.page_count)",
            "contract_draft": json.dumps([
                {"name": "formula_pages", "type": "boolean", "agg": "pages",
                 "instruction": "True if THIS page states a formula."},
                {"name": "page_count", "type": "number", "agg": "max",
                 "instruction": "The number of pages in the document."},
            ]),
        },
    )
    assert response.status_code == 422
    assert "supplied by the engine" in response.text


def test_edit_with_undeclared_field_offers_reconcile(client, db, member, course):
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": rule.text, "severity": rule.severity, "category": rule.category,
            "document_type": ["slide"],
            "cel": "size(doc.formula_pages) == 0 && doc.has_glossary",
        },
    )
    assert response.status_code == 422
    assert "does not declare" in response.text
    assert "reconcile" in response.text  # the offer to complete with the model


def test_reconcile_completes_contract_and_save_persists_it(
    client, db, member, course, monkeypatch
):
    """The professor's CEL is untouched; the worker (eager here) declares the
    missing field; the completed draft rides the form and Save persists it."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    completion = json.dumps({
        "contract": [{
            "name": "has_glossary", "type": "boolean",
            "agg": "any",
            "instruction": "True if THIS page shows a glossary of terms.",
        }]
    })
    monkeypatch.setattr(
        "app.llm.client.LLMClient.generate",
        lambda self, prompt, image_png=None, temperature=0.0, extra_body=None: completion,
    )
    cel = "size(doc.formula_pages) == 0 && doc.has_glossary"
    draft_form = {
        "text": rule.text, "severity": rule.severity, "category": rule.category,
        "document_type": ["slide"], "cel": cel,
        "agg_field": ["formula_pages"], "agg_value": ["pages"],
    }
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/reconcile", data=draft_form
    )
    assert response.status_code == 200
    assert 'name="contract_draft"' in response.text
    assert "has_glossary" in response.text  # the field the model added is shown
    db.expire_all()
    saved = db.get(CourseRule, rule.id)
    assert saved.edit_draft is None  # delivered into the form, not kept
    assert "has_glossary" not in (saved.extraction_contract or "")  # not saved yet

    contract_draft = json.dumps(
        json.loads(rule.extraction_contract or "[]")
        + json.loads(completion)["contract"]
    )
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={**draft_form, "contract_draft": contract_draft},
    )
    assert response.status_code == 200
    db.expire_all()
    saved = db.get(CourseRule, rule.id)
    assert saved.cel == cel
    names = {e["name"] for e in json.loads(saved.extraction_contract)}
    assert names == {"formula_pages", "has_glossary"}


def test_tampered_contract_draft_is_rejected(client, db, member, course):
    """The hidden contract_draft field is user-controlled input: broken JSON,
    a type-incompatible agg (which would crash the aggregator at report time),
    and an oversized instruction are all rejected before anything is stored."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    base = {
        "text": rule.text, "severity": rule.severity, "category": rule.category,
        "document_type": ["slide"], "cel": "size(doc.formula_pages) == 0",
    }
    url = f"/courses/{course.id}/rules/{rule.id}/edit"
    entry = json.loads(rule.extraction_contract)[0]

    response = client.post(url, data={**base, "contract_draft": "{broken"})
    assert response.status_code == 422 and "not valid JSON" in response.text

    bad_agg = json.dumps([{**entry, "agg": "union"}])  # union over a boolean
    response = client.post(url, data={**base, "contract_draft": bad_agg})
    assert response.status_code == 422 and "incompatible with its type" in response.text

    verbose = json.dumps([{**entry, "instruction": "x" * 800}])
    response = client.post(url, data={**base, "contract_draft": verbose})
    assert response.status_code == 422 and "700 characters" in response.text

    db.expire_all()
    saved = db.get(CourseRule, rule.id)
    assert json.loads(saved.extraction_contract)[0]["agg"] == "pages"  # untouched


def test_padded_contract_draft_is_trimmed_to_what_the_cel_reads(
    client, db, member, course
):
    """A draft stuffed with extra (valid-looking) fields cannot smuggle them in:
    everything the CEL does not read is dropped before saving."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    padded = json.loads(rule.extraction_contract) + [
        {"name": f"stuffed_{i}", "type": "boolean",
         "agg": "any", "instruction": "True if THIS page exists."}
        for i in range(25)
    ]
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": rule.text, "severity": rule.severity, "category": rule.category,
            "document_type": ["slide"], "cel": "size(doc.formula_pages) == 0",
            "contract_draft": json.dumps(padded),
        },
    )
    assert response.status_code == 200
    db.expire_all()
    saved = db.get(CourseRule, rule.id)
    assert [e["name"] for e in json.loads(saved.extraction_contract)] == ["formula_pages"]


def _draft(rule, status, **extra):
    import time as _time
    base = {
        "status": status, "queued_at": _time.time(), "text": "draft text",
        "severity": rule.severity, "category": rule.category,
        "document_types": ["slide"], "cel": "doc.ghost", "aggs": {},
        "base_contract": [], "missing": ["ghost"],
        "contract": [{"name": "ghost", "type": "boolean",
                      "agg": "any", "instruction": "True if THIS page haunts."}],
    }
    return json.dumps({**base, **extra})


def test_reopening_edit_resumes_an_in_flight_reconcile(client, db, member, course):
    """Closing the dialog mid-generation (tab close, navigation) loses nothing:
    reopening Edit shows the waiting state again and keeps polling."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    rule.edit_draft = _draft(rule, "completing")
    db.commit()
    response = client.get(f"/courses/{course.id}/rules/{rule.id}/edit")
    assert response.status_code == 200
    assert "reconcile-wait" in response.text  # the waiting state, still polling
    db.expire_all()
    assert db.get(CourseRule, rule.id).edit_draft is not None  # still in flight


def test_reopening_edit_delivers_a_finished_reconcile(client, db, member, course):
    """If the worker finished while the dialog was closed, reopening Edit shows
    the completed draft for review (not the stored rule) and consumes it."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    rule.edit_draft = _draft(rule, "ready")
    db.commit()
    response = client.get(f"/courses/{course.id}/rules/{rule.id}/edit")
    assert response.status_code == 200
    assert "ghost" in response.text  # the completed draft, not the stored rule
    assert 'name="contract_draft"' in response.text
    db.expire_all()
    assert db.get(CourseRule, rule.id).edit_draft is None  # delivered


def test_a_stuck_completing_draft_expires_with_a_clear_message(
    client, db, member, course
):
    """If the worker died mid-job, the rule must not stay behind an eternal
    spinner: past the job timeout the draft flips to a failed message."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    rule.edit_draft = _draft(rule, "completing", queued_at=0)  # long ago
    db.commit()
    response = client.get(f"/courses/{course.id}/rules/{rule.id}/edit")
    assert response.status_code == 200
    assert "took too long" in response.text
    db.expire_all()
    assert db.get(CourseRule, rule.id).edit_draft is None


def test_completion_job_ignores_delivered_or_missing_drafts(db, member, course):
    """The worker job is idempotent: once a draft is delivered (or gone), a
    late/duplicate job run must not touch it."""
    from app.services.rule_generation import complete_rule_contract

    rule = _boolean_field_rule(db, course)
    complete_rule_contract(rule.id)  # no draft at all: no-op
    rule.edit_draft = json.dumps({"status": "ready", "text": "t", "cel": "doc.x",
                                  "missing": ["x"], "base_contract": [],
                                  "severity": "low", "category": "content",
                                  "document_types": ["slide"], "aggs": {}})
    db.commit()
    complete_rule_contract(rule.id)  # already delivered: no-op
    db.expire_all()
    assert json.loads(db.get(CourseRule, rule.id).edit_draft)["status"] == "ready"


def test_cancel_stops_the_reconcile_and_restores_the_stored_rule(
    client, db, member, course
):
    """"Stop and discard" while the worker runs: the draft is dropped (so the
    job's late result is discarded) and the dialog shows the rule as stored."""
    login(client, "user@example.com")
    rule = _boolean_field_rule(db, course)
    rule.edit_draft = _draft(rule, "completing")
    db.commit()
    response = client.post(f"/courses/{course.id}/rules/{rule.id}/edit/cancel")
    assert response.status_code == 200
    assert rule.text in response.text            # the stored rule is back
    assert "The assistant is declaring" not in response.text
    db.expire_all()
    assert db.get(CourseRule, rule.id).edit_draft is None


def test_completion_job_never_resurrects_a_draft_discarded_mid_flight(
    db, member, course, monkeypatch
):
    """The draft can disappear WHILE the model call runs (save/reopen elsewhere);
    the job must throw its result away, not write the draft back."""
    from app.db import SessionLocal
    from app.services import rule_generation

    rule = _boolean_field_rule(db, course)
    rule.edit_draft = _draft(rule, "completing")
    db.commit()

    def clears_draft_then_answers(rule_arg, text, cel, missing, existing_json):
        other = SessionLocal()
        other.get(CourseRule, rule.id).edit_draft = None
        other.commit()
        other.close()
        return [{"name": "ghost", "type": "boolean",
                 "agg": "any", "instruction": "True if THIS page haunts."}]

    monkeypatch.setattr(rule_generation, "complete_contract", clears_draft_then_answers)
    rule_generation.complete_rule_contract(rule.id)
    db.expire_all()
    assert db.get(CourseRule, rule.id).edit_draft is None  # stayed discarded


def test_reconcile_and_draft_poll_are_owner_only(client, db, member, course):
    from app.models import Role, User
    from app.security import hash_password

    rule = _boolean_field_rule(db, course)
    db.add(User(email="intruder@example.com", password_hash=hash_password("password123"),
                first_name="In", last_name="Truder", role=Role.USER))
    db.commit()
    login(client, "intruder@example.com")
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/reconcile",
        data={"text": "x", "severity": "low", "category": "content",
              "document_type": ["slide"], "cel": "doc.y"},
    )
    assert response.status_code == 404
    assert client.get(f"/courses/{course.id}/rules/{rule.id}/edit/draft").status_code == 404


def test_reconcile_rejects_default_rules_and_broken_cel(client, db, member, course):
    login(client, "user@example.com")
    client.get(f"/courses/{course.id}/rules")  # seed defaults
    from sqlalchemy import select as sa_select
    default = db.execute(
        sa_select(CourseRule).where(CourseRule.origin == RuleOrigin.DEFAULT)
    ).scalars().first()
    response = client.post(
        f"/courses/{course.id}/rules/{default.id}/reconcile",
        data={"text": "x", "severity": "low", "category": "content",
              "document_type": ["slide"], "cel": "doc.y"},
    )
    assert response.status_code == 400

    rule = _boolean_field_rule(db, course)
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/reconcile",
        data={"text": rule.text, "severity": rule.severity, "category": rule.category,
              "document_type": ["slide"], "cel": "doc.x &&&& ("},
    )
    assert response.status_code == 422  # syntax first, never a wasted model call
    db.expire_all()
    assert db.get(CourseRule, rule.id).edit_draft is None


def test_other_users_cannot_see_course_rules(client, db, member, course):
    from app.models import Role, User
    from app.security import hash_password

    db.add(
        User(
            email="other@example.com",
            password_hash=hash_password("password123"),
            first_name="Other",
            last_name="Prof",
            role=Role.USER,
        )
    )
    db.commit()
    login(client, "other@example.com")
    assert client.get(f"/courses/{course.id}/rules").status_code == 404


# --- rule identity codes -------------------------------------------------------


def test_default_rules_carry_fixed_unique_codes(client, db, member, course):
    """Every seeded default gets its YAML code (same in every course)."""
    login(client, "user@example.com")
    client.get(f"/courses/{course.id}/rules")
    rules = db.execute(select(CourseRule)).scalars().all()
    codes = [r.code for r in rules]
    assert all(codes) and len(set(codes)) == len(codes)
    by_code = {r.code: r for r in rules}
    assert by_code["sy1"].document_type == "syllabus"
    assert by_code["all1"].document_type == "syllabus,slide,assessment,rubric"
    assert by_code["sya1"].document_type == "syllabus,assessment"


def test_code_prefix_covers_single_multi_and_all_types():
    from app.services.rules import code_prefix

    assert code_prefix(["syllabus"]) == "sy"
    assert code_prefix(["slide"]) == "s"
    assert code_prefix(["assessment"]) == "a"
    assert code_prefix(["rubric"]) == "r"
    # Order-independent, canonical concatenation for a subset of types.
    assert code_prefix(["assessment", "syllabus"]) == "sya"
    assert code_prefix(["syllabus", "slide", "assessment", "rubric"]) == "all"


def test_custom_rule_code_continues_the_default_sequence(client, db, member, course):
    """First custom syllabus rule of a course -> sy7 (defaults own sy1..sy6)."""
    login(client, "user@example.com")
    client.get(f"/courses/{course.id}/rules")  # seed defaults
    client.post(
        f"/courses/{course.id}/rules/chat",
        data={"message": "The syllabus must list the exam dates.",
              "document_type": "syllabus"},
    )
    rule = db.execute(
        select(CourseRule).where(CourseRule.origin == RuleOrigin.CUSTOM)
    ).scalar_one()
    assert rule.code == "sy7"
    # A second one continues the sequence; a different type set starts its own.
    client.post(
        f"/courses/{course.id}/rules/chat",
        data={"message": "The syllabus must state office hours.",
              "document_type": "syllabus"},
    )
    client.post(
        f"/courses/{course.id}/rules/chat",
        data={"message": "Graded tasks must publish criteria.",
              "document_type": ["rubric", "assessment"]},
    )
    codes = {
        r.source_prompt: r.code
        for r in db.execute(
            select(CourseRule).where(CourseRule.origin == RuleOrigin.CUSTOM)
        ).scalars()
    }
    assert codes["The syllabus must state office hours."] == "sy8"
    assert codes["Graded tasks must publish criteria."] == "ar1"


def test_codes_are_backfilled_on_a_pre_code_course(client, db, member, course):
    """A course seeded before codes existed self-heals on the next visit."""
    login(client, "user@example.com")
    client.get(f"/courses/{course.id}/rules")
    # Simulate the legacy state: wipe every code, add an uncoded custom rule.
    rules = db.execute(select(CourseRule)).scalars().all()
    for r in rules:
        r.code = None
    db.add(
        CourseRule(
            course_id=course.id, text="custom legacy rule",
            document_type="slide", origin=RuleOrigin.CUSTOM,
            status=RuleStatus.READY,
        )
    )
    db.commit()
    client.get(f"/courses/{course.id}/rules")
    db.expire_all()
    rules = db.execute(select(CourseRule)).scalars().all()
    assert all(r.code for r in rules)
    # Defaults recover their fixed YAML code; the custom rule continues s1..s4.
    by_text = {r.text: r for r in rules}
    assert by_text["custom legacy rule"].code == "s5"
