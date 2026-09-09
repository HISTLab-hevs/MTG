"""NL -> CEL compilation job, validation layers, and the review flow.

The autouse LLM stub in conftest returns a valid answer (description + CEL +
extraction contract); tests that exercise the refinement loop re-patch
LLMClient.generate per test.
"""

import json

from sqlalchemy import select

from app.models import CourseRule, RuleOrigin, RuleStatus
from app.services.rule_generation import (
    MAX_ATTEMPTS,
    check_closure,
    validate_cel,
    validate_contract,
)
from app.services.rules import load_default_rules
from tests.conftest import login


def _send_rule(client, course, message="The syllabus must list the exam dates."):
    return client.post(
        f"/courses/{course.id}/rules/chat",
        data={"message": message, "document_type": "syllabus"},
    )


def _custom_rule(db) -> CourseRule:
    return db.execute(
        select(CourseRule).where(CourseRule.origin == RuleOrigin.CUSTOM)
    ).scalar_one()


def _answer(description, cel, contract, severity="medium", category="content") -> str:
    return json.dumps(
        {
            "description": description,
            "cel": cel,
            "severity": severity,
            "category": category,
            "contract": contract,
        }
    )


EXAM_DATES_FIELD = {
    "name": "exam_dates",
    "type": "string_list",
    "agg": "union",
    "instruction": "List the exam dates on this page.",
}


def test_validate_cel():
    assert validate_cel("size(doc.learning_outcomes) > 0") is None
    assert validate_cel("size(doc.learning_outcomes >") is not None


def test_validate_contract():
    assert validate_contract([EXAM_DATES_FIELD]) is None
    assert validate_contract([]) is None
    assert "snake_case" in validate_contract([{**EXAM_DATES_FIELD, "name": "Bad-Name"}])
    assert "type" in validate_contract([{**EXAM_DATES_FIELD, "type": "object"}])
    assert "agg" in validate_contract([{**EXAM_DATES_FIELD, "agg": "concat"}])
    assert "instruction" in validate_contract([{**EXAM_DATES_FIELD, "instruction": ""}])
    assert "more than once" in validate_contract([EXAM_DATES_FIELD, EXAM_DATES_FIELD])


def test_check_closure():
    cel = "doc.mentions_ciao"
    assert check_closure(cel, {"mentions_ciao"}) is None
    assert "does not declare" in check_closure(cel, set())
    assert "never uses" in check_closure(cel, {"mentions_ciao", "extra_field"})
    banned = check_closure('doc.content.contains("ciao")', {"content"})
    assert "not allowed" in banned and "contains" in banned
    # String literals are not treated as fields.
    assert check_closure('"essay" in doc.methods', {"methods"}) is None
    # `.item`/`.page` on a tagged_pages entry are the aggregation's members,
    # not contract fields; field names merely containing those words survive.
    tagged = ("doc.formula_items.all(f, doc.example_items.exists(e, "
              "e.item == f.item && e.page > f.page && e.page <= f.page + 3))")
    assert check_closure(tagged, {"formula_items", "example_items"}) is None
    assert check_closure("size(doc.page_titles) > 0", {"page_titles"}) is None


def test_find_field_conflict():
    from app.services.contracts import Field, find_field_conflict

    existing = [Field(name="exam_dates", type="string_list", instruction="List the exam dates.", agg="union")]
    same = [Field(name="exam_dates", type="string_list", instruction="List the exam dates.", agg="union")]
    assert find_field_conflict(same, existing) is None  # identical reuse is fine
    diff_type = [Field(name="exam_dates", type="boolean", instruction="List the exam dates.", agg="any")]
    assert "different meaning" in find_field_conflict(diff_type, existing)
    diff_instr = [Field(name="exam_dates", type="string_list", instruction="List the dates differently.", agg="union")]
    assert "different meaning" in find_field_conflict(diff_instr, existing)
    fresh = [Field(name="office_hours", type="string_list", instruction="List office hours.", agg="union")]
    assert find_field_conflict(fresh, existing) is None


def test_default_rules_pass_all_validation_layers():
    for rule in load_default_rules():
        assert rule.severity in ("high", "medium", "low"), rule.cel
        assert rule.category, rule.cel
        assert validate_cel(rule.cel) is None, rule.cel
        assert validate_contract(rule.contract) is None, rule.cel
        names = {entry["name"] for entry in rule.contract}
        assert check_closure(rule.cel, names) is None, rule.cel


def test_default_rules_share_fields_consistently():
    """Same-named fields across default rules (e.g. has_questions) must agree."""
    by_name: dict[str, dict] = {}
    for rule in load_default_rules():
        for entry in rule.contract:
            prior = by_name.setdefault(entry["name"], entry)
            assert prior["type"] == entry["type"], entry["name"]
            assert prior["instruction"] == entry["instruction"], entry["name"]


def test_generation_produces_proposed_rule(client, db, member, course):
    login(client, "user@example.com")
    _send_rule(client, course)
    rule = _custom_rule(db)
    assert rule.status == RuleStatus.PROPOSED
    assert rule.text == "Stubbed rule."
    assert rule.cel == "size(doc.exam_dates) > 0"
    assert rule.severity == "medium"
    assert rule.category == "assessment"
    assert rule.attempts == 1
    assert rule.model_name == "test-generation-model"
    assert rule.error_message is None
    contract = json.loads(rule.extraction_contract)
    assert contract[0]["name"] == "exam_dates"


def test_invalid_cel_is_refined_with_higher_temperature(
    client, db, member, course, monkeypatch
):
    calls = []

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        calls.append((prompt, temperature))
        if len(calls) == 1:
            return _answer("Broken.", "size(doc. >", [])
        return _answer("Fixed rule.", "has(doc.exam_dates)", [EXAM_DATES_FIELD])

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    login(client, "user@example.com")
    _send_rule(client, course)

    rule = _custom_rule(db)
    assert rule.status == RuleStatus.PROPOSED
    assert rule.text == "Fixed rule."
    assert rule.cel == "has(doc.exam_dates)"
    assert rule.attempts == 2
    # The retry used the refinement prompt and a higher temperature.
    assert "rejected" in calls[1][0]
    assert calls[1][1] > calls[0][1]


def test_closure_error_triggers_refinement(client, db, member, course, monkeypatch):
    calls = []

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        calls.append(prompt)
        if len(calls) == 1:
            # Valid JSON and CEL, but the contract does not cover the field.
            return _answer("Uncovered.", "has(doc.exam_dates)", [])
        return _answer("Covered.", "has(doc.exam_dates)", [EXAM_DATES_FIELD])

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    login(client, "user@example.com")
    _send_rule(client, course)

    rule = _custom_rule(db)
    assert rule.status == RuleStatus.PROPOSED
    assert rule.attempts == 2
    assert "does not declare" in calls[1]


def test_generation_fails_after_max_attempts(client, db, member, course, monkeypatch):
    calls = []

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        calls.append(temperature)
        return "not even json"

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    login(client, "user@example.com")
    _send_rule(client, course)

    rule = _custom_rule(db)
    assert rule.status == RuleStatus.FAILED
    assert "rephrase" in rule.error_message
    assert len(calls) == MAX_ATTEMPTS
    assert calls == sorted(calls) and calls[0] < calls[-1]


def test_accept_moves_rule_into_rule_set(client, db, member, course):
    login(client, "user@example.com")
    _send_rule(client, course)
    rule = _custom_rule(db)

    response = client.post(f"/courses/{course.id}/rules/{rule.id}/accept")
    assert response.status_code == 200
    db.refresh(rule)
    assert rule.status == RuleStatus.READY
    # Accepted rules render in the per-type sections, not the review feed.
    assert "Stubbed rule." in response.text
    assert "Rules under review" not in response.text


def test_reject_deletes_proposed_rule(client, db, member, course):
    login(client, "user@example.com")
    _send_rule(client, course)
    rule_id = _custom_rule(db).id

    client.post(f"/courses/{course.id}/rules/{rule_id}/reject")
    db.expire_all()
    assert db.get(CourseRule, rule_id) is None


def test_accept_requires_proposed_status(client, db, member, course):
    login(client, "user@example.com")
    client.get(f"/courses/{course.id}/rules")  # seed defaults (status ready)
    default_rule = db.execute(select(CourseRule)).scalars().first()
    response = client.post(f"/courses/{course.id}/rules/{default_rule.id}/accept")
    assert response.status_code == 400


def test_edit_custom_rule_saves_valid_change(client, db, member, course):
    login(client, "user@example.com")
    _send_rule(client, course)
    rule = _custom_rule(db)

    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": "The syllabus must list at least two exam dates.",
            "severity": "high",
            "category": "clarity",
            "document_type": ["syllabus", "slide"],
            "cel": "size(doc.exam_dates) > 1",
        },
    )
    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#rules-area"
    db.refresh(rule)
    assert rule.severity == "high"
    assert rule.category == "clarity"
    # The professor re-scoped the rule to two types; stored canonically.
    assert rule.document_type == "syllabus,slide"
    assert rule.cel == "size(doc.exam_dates) > 1"
    assert "two exam dates" in rule.text


def test_edit_custom_rule_rejects_invalid_cel(client, db, member, course):
    login(client, "user@example.com")
    _send_rule(client, course)
    rule = _custom_rule(db)
    original_cel = rule.cel

    # References a field the contract does not declare -> closure error.
    response = client.post(
        f"/courses/{course.id}/rules/{rule.id}/edit",
        data={
            "text": "Bad rule",
            "severity": "low",
            "category": "content",
            "document_type": ["syllabus"],
            "cel": "size(doc.undeclared_field) > 0",
        },
    )
    assert response.status_code == 422
    assert "Validation failed" in response.text
    db.refresh(rule)
    # Nothing was persisted.
    assert rule.cel == original_cel


def test_default_rules_cannot_be_edited(client, db, member, course):
    login(client, "user@example.com")
    client.get(f"/courses/{course.id}/rules")  # seed defaults
    default_rule = db.execute(
        select(CourseRule).where(CourseRule.origin == RuleOrigin.DEFAULT)
    ).scalars().first()
    response = client.get(f"/courses/{course.id}/rules/{default_rule.id}/edit")
    assert response.status_code == 400
    response = client.post(f"/courses/{course.id}/rules/{default_rule.id}/reject")
    assert response.status_code == 400
