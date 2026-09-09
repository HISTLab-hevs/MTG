"""Analysis run: per-page extraction -> doc aggregation -> CEL -> verdicts.

JOB_EAGER=true runs both the upload pipeline and the analysis job inline, so
by the time the analyze POST returns the RuleEvaluation rows exist. The
extractor is stubbed to return per-page JSON facts for the union schema.
"""

import json
import threading
import time

from sqlalchemy import select

from app.models import (
    Course,
    CourseRule,
    DocumentType,
    EvaluationStatus,
    RuleEvaluation,
    RuleOrigin,
    RuleStatus,
)
from app.services.rules import ensure_default_rules
from tests.conftest import login, make_pdf, upload_pdf


def _seed_rules(db, course_id):
    """Default rules plus one custom slide rule whose verdict we control."""
    course = db.get(Course, course_id)
    ensure_default_rules(db, course)
    db.add(
        CourseRule(
            course_id=course_id,
            document_type=DocumentType.SLIDE,
            text="No slide may be about risk assessment.",
            cel="!doc.is_about_risk_assessment",
            extraction_contract=json.dumps(
                [
                    {
                        "name": "is_about_risk_assessment",
                        "type": "boolean",
                        "agg": "any",
                        "instruction": "True if this page is about risk assessment.",
                    }
                ]
            ),
            origin=RuleOrigin.CUSTOM,
            status=RuleStatus.READY,
        )
    )
    db.commit()


def _run(
    client, db, course, monkeypatch, values: dict,
    quotes: dict | None = None, markdown: str = "stubbed extraction markdown",
):
    """Stub the extractor to return one {value, quote} object per field per page."""
    quotes = quotes or {}
    page_facts = {
        name: {"value": v, "quote": quotes.get(name, "")} for name, v in values.items()
    }

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        if extra_body and "guided_json" in extra_body:
            return json.dumps(page_facts)
        if image_png:
            return markdown
        return ""

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Topic"]))
    _seed_rules(db, course.id)
    client.post(f"/courses/{course.id}/report/analyze", follow_redirects=False)


def _verdict(db, cel: str) -> RuleEvaluation:
    rule = db.execute(
        select(CourseRule).where(CourseRule.cel == cel)
    ).scalar_one()
    return db.execute(
        select(RuleEvaluation).where(RuleEvaluation.rule_id == rule.id)
    ).scalar_one()


def test_rule_fails_when_aggregated_fact_is_true(client, db, member, course, monkeypatch):
    # A page IS about risk assessment -> agg "any" -> doc true -> rule fails.
    _run(client, db, course, monkeypatch, {"is_about_risk_assessment": True})
    db.expire_all()
    ev = _verdict(db, "!doc.is_about_risk_assessment")
    assert ev.status == EvaluationStatus.EVALUATED
    assert ev.value is False


def test_rule_passes_when_aggregated_fact_is_false(client, db, member, course, monkeypatch):
    _run(client, db, course, monkeypatch, {"is_about_risk_assessment": False})
    db.expire_all()
    ev = _verdict(db, "!doc.is_about_risk_assessment")
    assert ev.status == EvaluationStatus.EVALUATED
    assert ev.value is True


def test_report_page_renders_verdicts(client, db, member, course, monkeypatch):
    _run(client, db, course, monkeypatch, {"is_about_risk_assessment": True})
    page = client.get(f"/courses/{course.id}/report")
    assert page.status_code == 200
    body = page.text
    assert "slides.pdf" in body
    assert "Fail" in body
    # Per-document counts replace the old course-wide stat row.
    assert "passed" in body


def test_passing_verdict_shows_evidence_when_quoted(
    client, db, member, course, monkeypatch
):
    # A pass with a supporting quote exposes its evidence the same way a fail
    # does — the grounding panel is shown for any verdict the extractor cited.
    _run(
        client, db, course, monkeypatch,
        {"is_about_risk_assessment": False},
        quotes={"is_about_risk_assessment": "stubbed extraction markdown"},
    )
    page = client.get(f"/courses/{course.id}/report")
    assert page.status_code == 200
    assert "Evidence" in page.text


def test_grounding_keeps_matching_quote_and_drops_hallucination(
    client, db, member, course, monkeypatch
):
    # The markdown contains the quote, so it survives into the stored evidence;
    # the value is still aggregated correctly and the verdict is unchanged.
    _run(
        client, db, course, monkeypatch,
        {"is_about_risk_assessment": True},
        quotes={"is_about_risk_assessment": "stubbed extraction markdown"},
    )
    db.expire_all()
    ev = _verdict(db, "!doc.is_about_risk_assessment")
    assert ev.value is False  # verdict unchanged by the grounding channel
    activation = json.loads(ev.activation)
    assert activation["doc"]["is_about_risk_assessment"] is True
    spans = activation["evidence"]["is_about_risk_assessment"]
    assert spans and spans[0]["quote"] == "stubbed extraction markdown"


def test_grounding_keeps_quote_absent_from_markdown(
    client, db, member, course, monkeypatch
):
    # The image is the ground truth and the markdown is lossy (it clips words at
    # line/page boundaries), so a quote the model read from the image is kept even
    # when this transcription lacks it — dropping it would force grounding back
    # onto the very transcription it is meant to correct.
    _run(
        client, db, course, monkeypatch,
        {"is_about_risk_assessment": True},
        quotes={"is_about_risk_assessment": "a span only on the page image"},
    )
    db.expire_all()
    ev = _verdict(db, "!doc.is_about_risk_assessment")
    activation = json.loads(ev.activation)
    assert activation["doc"]["is_about_risk_assessment"] is True
    spans = activation["evidence"]["is_about_risk_assessment"]
    assert spans and spans[0]["quote"] == "a span only on the page image"


def test_run_tracks_document_progress(client, db, member, course, monkeypatch):
    """After analysis the run's progress counters are fully and consistently set."""
    from app.models import ReportRun

    _run(client, db, course, monkeypatch, {"is_about_risk_assessment": True})
    db.expire_all()
    run = db.execute(select(ReportRun)).scalars().first()
    assert run.status == "completed"
    assert run.total_documents == 1
    assert run.documents_done == 1
    assert run.current_document is None  # cleared when finished
    assert run.progress_pct == 100


def test_rerun_reuses_verdicts_without_re_extracting(
    client, db, member, course, monkeypatch
):
    """A second run over unchanged documents/rules makes no new extraction calls."""
    from app.models import ReportRun

    _run(client, db, course, monkeypatch, {"is_about_risk_assessment": True})
    db.expire_all()
    first = db.execute(
        select(RuleEvaluation).where(RuleEvaluation.value.is_not(None))
    ).scalars().all()
    assert first  # verdicts exist after the first run

    # Count extractor calls on the re-run: with nothing changed there must be none.
    calls = {"n": 0}
    import app.services.analysis as analysis

    real = analysis.extract_page_facts

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(analysis, "extract_page_facts", counting)
    run = ReportRun(course_id=course.id, status="pending")
    db.add(run)
    db.commit()
    analysis.run_analysis(run.id)
    assert calls["n"] == 0  # untouched version reused, no model calls

    db.expire_all()
    run = db.get(ReportRun, run.id)
    # The reused document counts as done immediately; the bar is full from the
    # start of the document pass and never re-walks what was already analysed.
    assert run.total_documents == 1
    assert run.documents_done == 1
    assert run.progress_pct == 100


def test_adding_a_rule_reanalyses_documents_of_its_type(
    client, db, member, course, monkeypatch
):
    """A new READY rule for a document type forces a full re-analysis of that
    type's documents (extractor runs again), and the new rule gets a verdict."""
    from app.models import CourseRule, DocumentType, RuleOrigin, RuleStatus

    _run(client, db, course, monkeypatch, {"is_about_risk_assessment": True})

    # Add a second slide rule (READY) — the slide document must now be re-run.
    db.add(
        CourseRule(
            course_id=course.id,
            document_type=DocumentType.SLIDE,
            text="No slide may be empty.",
            cel="!doc.is_empty",
            extraction_contract=json.dumps([
                {
                    "name": "is_empty",
                    "type": "boolean",
                    "agg": "all",
                    "instruction": "True if this page has no content.",
                }
            ]),
            origin=RuleOrigin.CUSTOM,
            status=RuleStatus.READY,
        )
    )
    db.commit()

    import app.services.analysis as analysis
    from app.models import ReportRun

    calls = {"n": 0}
    real = analysis.extract_page_facts

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(analysis, "extract_page_facts", counting)
    run = ReportRun(course_id=course.id, status="pending")
    db.add(run)
    db.commit()
    analysis.run_analysis(run.id)

    assert calls["n"] > 0  # the slide document was re-extracted for the new rule
    new_ev = _verdict(db, "!doc.is_empty")
    assert new_ev.status == EvaluationStatus.EVALUATED


def test_rerun_counts_reused_documents_as_done_in_progress(
    client, db, member, course, monkeypatch
):
    """Adding one document to an analysed course: the bar starts at 1/2, not 0/2."""
    from app.models import ReportRun

    # First run analyses one document.
    _run(client, db, course, monkeypatch, {"is_about_risk_assessment": True})

    # A second document is uploaded; only it should be analysed on the next run.
    upload_pdf(client, course.id, "more.pdf", make_pdf(["Another"]))

    # Capture the run's progress at the moment the document pass begins, before
    # the (only) new document is processed.
    import app.services.analysis as analysis

    seen = {}
    real = analysis._analyse_version

    def capture(db_, version, doc_type, rules, run):
        seen.setdefault("documents_done", run.documents_done)
        seen.setdefault("total", run.total_documents)
        return real(db_, version, doc_type, rules, run)

    monkeypatch.setattr(analysis, "_analyse_version", capture)
    run = ReportRun(course_id=course.id, status="pending")
    db.add(run)
    db.commit()
    analysis.run_analysis(run.id)

    # The reused document is already counted; the pass resumes from the new one.
    assert seen["total"] == 2
    assert seen["documents_done"] == 1
    db.expire_all()
    run = db.get(ReportRun, run.id)
    assert run.documents_done == 2  # both done when finished


def test_progress_pct_accounts_for_partway_pages():
    """Overall percent blends whole documents with the current one's pages."""
    from app.models import ReportRun

    run = ReportRun(
        total_documents=2,
        documents_done=1,
        current_pages_total=4,
        current_pages_done=2,
    )
    # 1 whole doc + half of the second = 1.5 / 2 = 75%.
    assert run.progress_pct == 75


def test_evidence_caption_is_coherent_for_every_outcome():
    """The evidence caption is deterministic and sensible whether or not a quote
    exists, so the report is never left with a bare empty quote."""
    from app.models import EvaluationStatus
    from app.services.report_view import RuleFact, RuleVerdict

    def verdict(value, quotes):
        return RuleVerdict(
            rule=None,  # caption never touches the rule
            status=EvaluationStatus.EVALUATED,
            value=value,
            error_message=None,
            facts=[RuleFact(field="f", value=value, quotes=quotes)],
        )

    q = [{"quote": "states the policy clearly", "page": 1}]
    # pass + quote / pass + no quote
    assert verdict(True, q).evidence_caption == "What satisfied the rule"
    assert "passed" in verdict(True, []).evidence_caption.lower()
    # fail + quote / fail + no quote. The quoteless failure must not claim the
    # element is "missing" outright: a document-wide judgement rule (e.g. "must
    # be stimulating") also fails with nothing to cite, so the caption names
    # both readings.
    assert verdict(False, q).evidence_caption == "Where the rule was not met"
    no_quote = verdict(False, []).evidence_caption.lower()
    assert "absent" in no_quote and "document-wide" in no_quote


def test_reasoning_ratio_default_rule_semantics():
    """The reasoning-ratio assessment rule (a1) must keep its SHAPE.

    What matters, and what a careless edit breaks, is the behaviour: a document
    with no graded task passes vacuously, a document at or above the threshold
    passes, one below it fails, and the comparison is a ratio rather than a
    raw count.

    The threshold itself is deliberately NOT hardcoded here. It has already been
    retuned once (60% to 50%, with the reasoning recorded in the YAML), and a
    test that pins the number turns every legitimate retune into a failure while
    testing nothing about correctness. The rule is found by its stable code and
    the threshold is read out of the CEL it actually ships with.
    """
    import re

    from app.services import rule_engine
    from app.services.rules import load_default_rules

    rules = [r for r in load_default_rules() if r.code == "a1"]
    assert len(rules) == 1, "expected exactly one rule with code a1"
    cel = rules[0].cel
    rule = CourseRule(cel=cel)

    threshold = int(re.search(r">=\s*(\d+)", cel).group(1))
    assert 0 < threshold <= 100, f"implausible ratio threshold: {threshold}"

    def doc(has_task, total, reasoning):
        return {
            "has_assessment_task": has_task,
            "question_count": total,
            "reasoning_question_count": reasoning,
        }

    def at(pct, total=100):
        """A document whose reasoning share is exactly `pct` percent."""
        return doc(True, total, round(total * pct / 100))

    # No graded task -> vacuous pass (the has_assessment_task guard). Without
    # it, every non-assessment document would be flagged.
    assert rule_engine.evaluate(rule, doc(False, 0, 0)) is True
    assert rule_engine.evaluate(rule, doc(False, 10, 0)) is True
    # At the threshold passes, one point under fails: the boundary is inclusive.
    assert rule_engine.evaluate(rule, at(threshold)) is True
    assert rule_engine.evaluate(rule, at(threshold - 1)) is False
    # It is a RATIO, not a count: the same share passes at any size.
    assert rule_engine.evaluate(rule, at(threshold, total=10)) is True
    # The extremes behave.
    assert rule_engine.evaluate(rule, doc(True, 4, 4)) is True
    assert rule_engine.evaluate(rule, doc(True, 4, 0)) is False


def test_focus_group_default_rules_have_expected_conditional_semantics():
    """Focus-group rules fail only when their triggering context is present and
    the corresponding safeguard or pedagogical feature is absent."""
    from app.services import rule_engine
    from app.services.rules import load_default_rules

    cases = [
        (
            "required to use a specific AI tool",
            {"requires_specific_ai_tool": True, "provides_equitable_ai_access": False},
            {"requires_specific_ai_tool": True, "provides_equitable_ai_access": True},
        ),
        (
            "address bias or stereotype reproduction",
            {"mentions_ai_use": True, "teaches_ai_bias_awareness": False},
            {"mentions_ai_use": True, "teaches_ai_bias_awareness": True},
        ),
        (
            "compare, critique, or challenge AI output",
            {"uses_ai_in_teaching": True, "has_critical_ai_activity": False},
            {"uses_ai_in_teaching": True, "has_critical_ai_activity": True},
        ),
        (
            "meaningful disciplinary problem",
            {"has_assessment_task": True, "has_meaningful_task_context": False},
            {"has_assessment_task": True, "has_meaningful_task_context": True},
        ),
        (
            "clear ambiguity",
            {"has_assessment_task": True, "has_invalid_assessment_item": True},
            {"has_assessment_task": True, "has_invalid_assessment_item": False},
        ),
        (
            "confidential or sensitive external data",
            {
                "involves_sensitive_external_data": True,
                "states_ai_confidentiality_safeguard": False,
            },
            {
                "involves_sensitive_external_data": True,
                "states_ai_confidentiality_safeguard": True,
            },
        ),
        (
            "AI-generated content",
            {"requires_ai_generated_content": True, "states_ai_ip_guidance": False},
            {"requires_ai_generated_content": True, "states_ai_ip_guidance": True},
        ),
    ]

    shipped = load_default_rules()
    for text_fragment, failing_doc, passing_doc in cases:
        matches = [r for r in shipped if text_fragment.lower() in r.text.lower()]
        assert len(matches) == 1, text_fragment
        rule = CourseRule(cel=matches[0].cel)
        assert rule_engine.evaluate(rule, failing_doc) is False, text_fragment
        assert rule_engine.evaluate(rule, passing_doc) is True, text_fragment


def test_list_facts_group_items_and_merge_pages():
    """A tagged-pages list fact renders as per-item lines with pages merged —
    'safety property (p. 13, 14, 15)' — never one run-on line per occurrence."""
    from app.services.report_view import RuleFact

    fact = RuleFact(
        field="formula_items",
        value=[
            {"item": "safety property", "page": 13},
            {"item": "safety property", "page": 14},
            {"item": "reachability", "page": 5},
            {"item": "safety property", "page": 15},
        ],
        quotes=[],
    )
    assert fact.value_display_items == [
        "safety property (p. 13, 14, 15)",
        "reachability (p. 5)",
    ]
    # Scalars keep using value_display; the items list stays empty.
    assert RuleFact(field="n", value=3, quotes=[]).value_display_items == []
    # Plain (untagged) lists still get one line per item.
    assert RuleFact(field="l", value=["a", "b"], quotes=[]).value_display_items == ["a", "b"]


def test_rule_reading_only_page_count_skips_extraction(
    client, db, member, course, monkeypatch
):
    """A rule over engine-supplied members alone declares no contract fields, so
    there is nothing to extract: the per-page model calls must be skipped
    entirely (they would ask for an empty schema and return nothing), while the
    verdict still gets the document's real length."""
    import app.services.analysis as analysis

    calls = []

    def counting_extract(image_path, markdown, page_number, fields):
        calls.append(page_number)
        return {}

    monkeypatch.setattr(analysis, "extract_page_facts", counting_extract)
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["A", "B", "C"]))
    course_row = db.get(Course, course.id)
    db.add(
        CourseRule(
            course_id=course.id,
            document_type=DocumentType.SLIDE,
            text="The deck must not exceed two slides.",
            cel="doc.page_count <= 2",
            extraction_contract="[]",
            origin=RuleOrigin.CUSTOM,
            status=RuleStatus.READY,
        )
    )
    db.commit()
    client.post(f"/courses/{course.id}/report/analyze", follow_redirects=False)

    assert calls == [], "no page should have been sent to the extractor"
    row = _verdict(db, "doc.page_count <= 2")
    assert row.status == EvaluationStatus.EVALUATED
    # Three pages uploaded, so the rule genuinely fails rather than erroring.
    assert row.value is False
    assert json.loads(row.activation)["doc"]["page_count"] == 3


def test_evidence_extraction_runs_pages_in_parallel(
    client, db, member, course, monkeypatch
):
    """Evidence extraction (analysis._extract) fans out across pages instead of
    calling the model one page at a time, same pattern as markdown extraction."""
    import app.services.analysis as analysis

    from app.config import get_settings

    page_count = 5
    # Let every page run at once so overlap is guaranteed regardless of the
    # configured default.
    monkeypatch.setattr(get_settings(), "evidence_extraction_concurrency", page_count)

    in_flight = {"count": 0, "max": 0}
    lock = threading.Lock()

    def slow_extract(image_path, markdown, page_number, fields):
        with lock:
            in_flight["count"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["count"])
        try:
            # Later pages return sooner, forcing out-of-order completion.
            time.sleep(0.05 * (page_count - page_number))
            return {"is_about_risk_assessment": {"value": page_number == 1, "quote": ""}}
        finally:
            with lock:
                in_flight["count"] -= 1

    monkeypatch.setattr(analysis, "extract_page_facts", slow_extract)
    login(client, "user@example.com")
    upload_pdf(
        client, course.id, "slides.pdf", make_pdf([f"Page {i}" for i in range(page_count)])
    )
    _seed_rules(db, course.id)
    client.post(f"/courses/{course.id}/report/analyze", follow_redirects=False)

    # At least two pages were genuinely in flight at once (real concurrency,
    # not just a thread pool that happens to run one at a time).
    assert in_flight["max"] > 1

    db.expire_all()
    ev = _verdict(db, "!doc.is_about_risk_assessment")
    assert ev.status == EvaluationStatus.EVALUATED
    # Aggregation ("any") must see page 1's True regardless of completion
    # order: results are re-sorted back into page order before aggregation.
    assert ev.value is False
