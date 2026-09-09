"""The report view, history, and compare pages render end to end.

Drives a real analysis run (JOB_EAGER) with a stubbed extractor, then GETs the
new pages. The report model is unconfigured in tests, so prose falls back to the
deterministic templates — which is exactly what we want to assert renders.
"""

import json
import re

from app.models import Course, CourseRule, DocumentType, RuleOrigin, RuleStatus
from app.services.rules import ensure_default_rules
from tests.conftest import login, make_pdf, upload_pdf


def _seed_failing_rule(db, course_id):
    course = db.get(Course, course_id)
    ensure_default_rules(db, course)
    db.add(
        CourseRule(
            course_id=course_id,
            document_type=DocumentType.SLIDE,
            text="No slide may be about risk assessment.",
            rationale="Risk content is out of scope here.",
            cel="!doc.is_about_risk_assessment",
            severity="high",
            category="content",
            extraction_contract=json.dumps([
                {
                    "name": "is_about_risk_assessment",
                    "type": "boolean",
                    "agg": "any",
                    "instruction": "True if this page is about risk assessment.",
                }
            ]),
            origin=RuleOrigin.CUSTOM,
            status=RuleStatus.READY,
        )
    )
    db.commit()


def _compare_dates(text):
    """The two run dates as the compare page prints them, left side first."""
    return re.findall(r'compare-direction-date">([^<]+)<', text)


def _analyze(client, db, course, monkeypatch, fail=True):
    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        if extra_body and "guided_json" in extra_body:
            return json.dumps({
                "is_about_risk_assessment": {
                    "value": fail, "quote": "risk assessment" if fail else ""
                }
            })
        if image_png:
            return "this page covers risk assessment in depth"
        return ""

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    client.post(f"/courses/{course.id}/report/analyze", follow_redirects=False)


def test_report_view_renders_grid_priorities_and_charts(
    client, db, member, course, monkeypatch
):
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Risk"]))
    _seed_failing_rule(db, course.id)
    _analyze(client, db, course, monkeypatch, fail=True)

    page = client.get(f"/courses/{course.id}/report/view")
    assert page.status_code == 200
    body = page.text
    assert "report-summary-grid" in body   # the document x category grid
    assert "report-priorities" in body     # the priority block (failures exist)


def test_overview_shows_analysis_when_present(client, db, member, course, monkeypatch):
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Risk"]))
    _seed_failing_rule(db, course.id)

    # Before any run, the overview is the empty state.
    before = client.get(f"/courses/{course.id}/overview")
    assert "empty-state" in before.text
    assert "overview-scores" not in before.text

    _analyze(client, db, course, monkeypatch, fail=True)

    # After a run it carries the scores, the priorities, and the way in.
    after = client.get(f"/courses/{course.id}/overview")
    assert after.status_code == 200
    assert "empty-state" not in after.text
    assert "overview-score-val" in after.text
    assert "overview-priorities" in after.text
    assert f"/courses/{course.id}/report/view" in after.text


def test_history_and_compare_pages_render(client, db, member, course, monkeypatch):
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Risk"]))
    _seed_failing_rule(db, course.id)
    _analyze(client, db, course, monkeypatch, fail=True)
    # A second document appears before the next run, so the diff has a real,
    # observable change (a document added) even though the first one's immutable
    # verdicts are reused.
    upload_pdf(client, course.id, "more.pdf", make_pdf(["Extra"]))
    _analyze(client, db, course, monkeypatch, fail=True)

    # The old history page is now the report list on the Analysis page, which
    # the legacy URL redirects to.
    history = client.get(f"/courses/{course.id}/report/history")
    assert history.status_code == 200
    assert "Reports" in history.text

    from sqlalchemy import select

    from app.models import CourseReport

    ids = db.execute(
        select(CourseReport.id).order_by(CourseReport.id)
    ).scalars().all()
    assert len(ids) >= 2
    # Compare takes an unordered pair; the page decides the direction.
    compare = client.get(
        f"/courses/{course.id}/report/compare?ids={ids[0]}&ids={ids[-1]}"
    )
    assert compare.status_code == 200
    assert "What changed" in compare.text
    assert "more.pdf" in compare.text  # the added document shows in the doc diff
    # Chronological by default: the older run is the "before".
    older, newer = (
        db.get(CourseReport, ids[0]).created_at,
        db.get(CourseReport, ids[-1]).created_at,
    )
    assert older <= newer
    # reverse=1 really flips the direction: the two dates swap sides, so the
    # comparison is recomputed rather than relabelled.
    straight = client.get(
        f"/courses/{course.id}/report/compare?ids={ids[0]}&ids={ids[-1]}"
    )
    flipped = client.get(
        f"/courses/{course.id}/report/compare?ids={ids[0]}&ids={ids[-1]}&reverse=1"
    )
    assert flipped.status_code == 200
    assert _compare_dates(flipped.text) == list(
        reversed(_compare_dates(straight.text))
    )
    # Anything that is not a pair has nothing to compare, so it goes back to
    # the list instead of rendering an empty diff.
    for bad in (f"ids={ids[0]}", f"ids={ids[0]}&ids={ids[0]}", ""):
        single = client.get(
            f"/courses/{course.id}/report/compare?{bad}", follow_redirects=False
        )
        assert single.status_code == 303
