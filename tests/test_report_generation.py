"""Report generation chained after analysis (JOB_EAGER): per-document sections
plus a course-level summary, grounded in the deterministic rule verdicts.

The extractor and the report model are stubbed via the global generate() patch;
here we branch on the prompt so report calls return report JSON and extraction
calls return facts, exercising the real two-pass generator and persistence.
"""

import json
import threading
import time

from sqlalchemy import select

from app.models import (
    Course,
    CourseRule,
    DocumentReportSection,
    DocumentType,
    ReportStatus,
    RuleOrigin,
    RuleStatus,
)
from app.services.report_view import latest_course_report
from app.services.rules import ensure_default_rules
from tests.conftest import login, make_pdf, upload_pdf


def _seed_rules(db, course_id):
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


def _run(client, db, course, monkeypatch):
    """Fail the custom rule, and route report calls to report JSON."""
    page_facts = {"is_about_risk_assessment": {"value": True, "quote": "risk assessment"}}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        # Image calls are extraction: markdown when unconstrained, page facts
        # when guided. Text-only guided calls are the two report passes.
        if image_png:
            if extra_body and "guided_json" in extra_body:
                return json.dumps(page_facts)
            return "this page is about risk assessment"
        prompt = prompt or ""
        if '"issues"' in prompt:
            return json.dumps(
                {"issues": "Issue: risk assessment slide.", "solutions": "Remove it."}
            )
        if '"summary"' in prompt:
            return json.dumps(
                {"summary": "Course summary.", "solutions": "Course advice."}
            )
        return ""

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Topic"]))
    _seed_rules(db, course.id)
    client.post(f"/courses/{course.id}/report/analyze", follow_redirects=False)


def test_analysis_chains_a_course_report(client, db, member, course, monkeypatch):
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    assert report is not None
    assert report.status == ReportStatus.COMPLETED
    assert report.summary_md == "Course summary."
    assert report.solutions_md == "Course advice."
    # The never-applied checks live on the metrics (rendered as their own
    # plain-language block), never inside the Overall summary.
    metrics = json.loads(report.metrics_json)
    assert metrics["never_applicable"], "gated default rules should be n/a here"
    assert all("rule" in n and "documents" in n for n in metrics["never_applicable"])

    sections = db.execute(
        select(DocumentReportSection).where(
            DocumentReportSection.course_report_id == report.id
        )
    ).scalars().all()
    assert len(sections) == 1
    assert sections[0].filename == "slides.pdf"
    assert "risk assessment" in sections[0].issues_md
    assert sections[0].solutions_md == "Remove it."


def test_report_retries_when_model_underfills_the_findings_array(
    client, db, member, course, monkeypatch
):
    """guided_json's minItems/maxItems is not always honoured: the model can
    close the issues/solutions array early (schema-valid JSON, fewer entries
    than findings). One retry at a different temperature is tried before any
    entry is allowed to fall back to the generic 'Address the failed check'
    line — this reproduces the exact shape seen in production (first call
    returns only 1 entry regardless of how many findings there really are)."""
    from app.models import CourseRule, DocumentType, RuleOrigin, RuleStatus

    calls = {"n": 0}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        if image_png:
            if extra_body and "guided_json" in extra_body:
                return json.dumps(
                    {"is_about_risk_assessment": {"value": True, "quote": "risk"}}
                )
            return "this page is about risk assessment"
        prompt = prompt or ""
        if '"issues"' in prompt:
            calls["n"] += 1
            # "FINDING N: <rule text>" lines are only emitted once per real
            # finding (the fixed instructions below them say "FINDING i" or
            # "FINDING 1, ... FINDING 2" without a trailing colon+space).
            import re as _re

            finding_count = len(_re.findall(r"FINDING \d+: ", prompt))
            if temperature == 0.0:
                # First attempt: under-fills both arrays to 1 entry regardless
                # of how many findings there really are — schema-valid JSON,
                # no parse error, exactly the failure mode found in production.
                return json.dumps({"issues": ["Only one."], "solutions": ["Only one."]})
            return json.dumps(
                {
                    "issues": [f"Issue {i + 1}, in full." for i in range(finding_count)],
                    "solutions": [f"Fix {i + 1}, in full." for i in range(finding_count)],
                }
            )
        if '"summary"' in prompt:
            return json.dumps({"summary": "Course summary.", "solutions": "Course advice."})
        return ""

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Topic"]))
    course_obj = db.get(Course, course.id)
    ensure_default_rules(db, course_obj)
    db.add_all(
        [
            CourseRule(
                course_id=course.id,
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
            ),
            CourseRule(
                course_id=course.id,
                document_type=DocumentType.SLIDE,
                text="No slide may be empty.",
                cel="doc.is_about_risk_assessment",  # also fails: same fact, true
                extraction_contract=json.dumps(
                    [
                        {
                            "name": "is_about_risk_assessment",
                            "type": "boolean",
                            "agg": "any",
                            "instruction": "True if this page has no content.",
                        }
                    ]
                ),
                origin=RuleOrigin.CUSTOM,
                status=RuleStatus.READY,
            ),
        ]
    )
    db.commit()
    client.post(f"/courses/{course.id}/report/analyze", follow_redirects=False)

    assert calls["n"] == 2  # the under-filled first call triggered exactly one retry
    db.expire_all()
    report = latest_course_report(db, course)
    section = db.execute(
        select(DocumentReportSection).where(
            DocumentReportSection.course_report_id == report.id
        )
    ).scalars().one()
    # Every finding's real text made it in — from the retry, not the generic
    # "Address the failed check" fallback.
    assert "Issue 1, in full." in section.issues_md
    assert "Fix 1, in full." in section.solutions_md
    assert "Address the failed check" not in section.solutions_md


def test_document_reports_run_in_parallel_and_stay_in_order(
    client, db, member, course, monkeypatch
):
    """Per-document report prose (report_generation._document_reports) fans out
    across documents instead of one model call at a time, same pattern as
    evidence extraction — and sections still land in the course's document
    order regardless of which call finishes first."""
    from app.config import get_settings

    doc_count = 4
    monkeypatch.setattr(get_settings(), "report_generation_concurrency", doc_count)

    page_facts = {"is_about_risk_assessment": {"value": True, "quote": "risk"}}
    in_flight = {"count": 0, "max": 0}
    lock = threading.Lock()

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        if image_png:
            if extra_body and "guided_json" in extra_body:
                return json.dumps(page_facts)
            return "this page is about risk assessment"
        prompt = prompt or ""
        if '"issues"' in prompt:
            with lock:
                in_flight["count"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["count"])
            try:
                # Reverse order: the document named last in the prompt returns
                # first, forcing out-of-order completion.
                for n in range(doc_count, 0, -1):
                    if f"doc{n}.pdf" in prompt:
                        time.sleep(0.03 * (doc_count - n))
                        break
                return json.dumps(
                    {"issues": "Issue: risk assessment slide.", "solutions": "Remove it."}
                )
            finally:
                with lock:
                    in_flight["count"] -= 1
        if '"summary"' in prompt:
            return json.dumps({"summary": "Course summary.", "solutions": "Course advice."})
        return ""

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    login(client, "user@example.com")
    for n in range(1, doc_count + 1):
        upload_pdf(client, course.id, f"doc{n}.pdf", make_pdf(["Intro", "Topic"]))
    _seed_rules(db, course.id)
    client.post(f"/courses/{course.id}/report/analyze", follow_redirects=False)

    # At least two documents' report calls were genuinely in flight at once.
    assert in_flight["max"] > 1

    db.expire_all()
    report = latest_course_report(db, course)
    assert report.status == ReportStatus.COMPLETED
    sections = db.execute(
        select(DocumentReportSection)
        .where(DocumentReportSection.course_report_id == report.id)
        .order_by(DocumentReportSection.id)
    ).scalars().all()
    # Sections keep the course's document order (Course.documents is newest
    # first: doc4, doc3, doc2, doc1), not the model calls' finish order (doc1
    # finishes last, doc4 first, per the sleep schedule above — so a
    # finish-order bug would reverse this list).
    assert [s.filename for s in sections] == [f"doc{n}.pdf" for n in range(doc_count, 0, -1)]


def test_report_view_page_renders(client, db, member, course, monkeypatch):
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    page = client.get(f"/courses/{course.id}/report/view")
    assert page.status_code == 200
    body = page.text
    # The report's own content reached the page (this is data, not UI copy).
    assert "Course summary." in body
    assert "Remove it." in body
    # The refinement chat is present and active (posts to the chat endpoint).
    assert f"/courses/{course.id}/report/{report.id}/chat" in body
    # Deterministic diagnostics render from the persisted metrics.
    assert 'class="metric-score"' in body
    # The chat is signposted with starter chips so the professor discovers what
    # the assistant can do, and can clear the conversation and restart.
    assert 'class="chat-starter"' in body
    assert "data-prompt=" in body
    assert 'id="report-chat-new"' in body
    assert 'id="report-chat-message"' in body
    assert 'data-max-chars="16000"' in body
    # The never-applicable checks render as a collapsed appendix at the END of
    # the report pane (not among the findings), opened on demand — they judged
    # nothing, so they must not read as results.
    na_at = body.index('class="report-block report-na"')
    assert "<details" in body[na_at - 40 : na_at]
    assert " open" not in body[na_at - 40 : na_at]  # collapsed by default
    assert na_at > body.index("Suggested fix")  # after the document sections


def test_report_view_redirects_without_report(client, db, member, course, monkeypatch):
    login(client, "user@example.com")
    resp = client.get(
        f"/courses/{course.id}/report/view", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"/courses/{course.id}/report")


def test_fallback_report_without_model(client, db, member, course, monkeypatch):
    """When the report endpoint is unconfigured, deterministic fallback runs."""
    page_facts = {"is_about_risk_assessment": {"value": True, "quote": "risk assessment"}}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        if extra_body and "guided_json" in extra_body:
            return json.dumps(page_facts)
        if image_png:
            return "this page is about risk assessment"
        return ""

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)

    # Make only the REPORT client report itself as unconfigured, so extraction
    # (the generation client) still runs but report generation takes the
    # deterministic fallback path.
    class _Unconfigured:
        is_configured = False

    monkeypatch.setattr(
        "app.services.report_generation.get_report_client",
        lambda: _Unconfigured(),
    )
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Topic"]))
    _seed_rules(db, course.id)
    client.post(f"/courses/{course.id}/report/analyze", follow_redirects=False)

    db.expire_all()
    report = latest_course_report(db, course)
    assert report is not None
    sections = report.sections
    assert len(sections) == 1
    # Deterministic fallback lists the failed rule and its grounded quote.
    assert "No slide may be about risk assessment." in sections[0].issues_md
    assert "risk assessment" in sections[0].issues_md


def test_evidence_facts_explain_the_verdict_and_filter_guard_noise():
    """The report shows the extracted values that decided a verdict (counts,
    levels), reads them in plain language, and drops a satisfied guard field's
    redundant quote. Deterministic, no model call."""
    from app.services.report_view import RuleFact, RuleVerdict

    # The screenshot case: a graded quiz, 1 of 5 questions reasoning -> 20% < 60%.
    # The guard role is declared by the contract (`gate: true`), never inferred
    # from the field name.
    guard = RuleFact("has_assessment_task", True,
                     [{"quote": "In-class quiz (graded, 5%)", "page": 4}],
                     gate=True)
    total = RuleFact("question_count", 5,
                     [{"quote": "Answer all five questions", "page": 4}])
    reasoning = RuleFact("reasoning_question_count", 1,
                         [{"quote": "take a side and justify your position", "page": 4}])
    v = RuleVerdict(rule=None, status="evaluated", value=False, error_message=None,
                    facts=[guard, total, reasoning])

    # Readable labels + values for the deciding fields.
    labels = {f.label: f.value_display for f in v.informative_facts}
    assert labels == {"Question count": "5", "Reasoning question count": "1"}
    # The satisfied guard (has_assessment_task = yes) is filtered out as noise.
    assert all("assessment task" not in f.label.lower() for f in v.informative_facts)
    # And its redundant quote is dropped from the evidence, leaving the two that
    # actually ground the count.
    quotes = [q["quote"] for q in v.evidence_quotes]
    assert "In-class quiz (graded, 5%)" not in quotes
    assert "take a side and justify your position" in quotes


def test_evidence_keeps_a_false_guard_and_absence_facts():
    """A guard that is FALSE (the reason a rule passed vacuously) is informative;
    an absence field (e.g. has_explicit_criterion = no) is shown so the
    professor sees exactly what was missing."""
    from app.services.report_view import RuleFact, RuleVerdict

    # Vacuous pass: no AI mention, so the AI-evaluation rule passes -> show why.
    false_guard = RuleFact("mentions_ai_use", False, [], gate=True)
    v1 = RuleVerdict(rule=None, status="evaluated", value=True, error_message=None,
                     facts=[false_guard])
    assert [(f.label, f.value_display) for f in v1.informative_facts] == \
        [("Mentions AI use", "no")]

    # Absence fail: the criterion field is False with no quote -> still informative.
    absent = RuleFact("has_explicit_criterion", False, [])
    v2 = RuleVerdict(rule=None, status="evaluated", value=False, error_message=None,
                     facts=[absent])
    assert [(f.label, f.value_display) for f in v2.informative_facts] == \
        [("Has explicit criterion", "no")]
    assert not v2.has_evidence_quotes  # no span -> caption explains the absence


def test_build_metrics_is_deterministic_from_verdicts():
    """Severity-weighted health, per-category bars, high-severity count."""
    from types import SimpleNamespace

    from app.services.report_metrics import build_metrics

    def verdict(outcome, severity, category):
        rule = SimpleNamespace(severity=severity, category=category)
        # build_metrics reads v.severity (the per-document effective severity);
        # these verdicts are not on slides, so it equals the rule's severity.
        return SimpleNamespace(outcome=outcome, rule=rule, severity=severity)

    doc = SimpleNamespace(
        verdicts=[
            verdict("pass", "low", "clarity"),
            verdict("fail", "high", "assessment"),
            verdict("pass", "high", "assessment"),
            verdict("skipped", "medium", "content"),  # ignored
        ]
    )
    view = SimpleNamespace(documents=[doc])
    m = build_metrics(view)

    assert m.checked == 3 and m.passed == 2
    assert m.high_severity_failures == 1
    # weighted: passed low(1)+high(3)=4 of total low(1)+high(3)+high(3)=7 -> 57
    assert m.health_score == 57
    bars = {c.category: c for c in m.categories}
    assert bars["assessment"].passed == 1 and bars["assessment"].failed == 1
    assert bars["clarity"].pass_pct == 100
    assert "content" not in bars  # only a skipped verdict, not charted
    # Survives a round-trip through the persisted JSON.
    assert json.loads(m.to_json())["health_score"] == 57


def test_report_chat_edits_editable_block(client, db, course, member, monkeypatch):
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        return json.dumps(
            {
                "action": "edit",
                "reply": "**Done.** Updated the advice.",
                "edits": [{"target": "course", "markdown": "New course advice."}],
            }
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "make it more concrete", "history": "[]"},
    )
    assert resp.status_code == 200
    # The assistant reply renders as Markdown (bold becomes <strong>), like the
    # report and the materials chat — not raw asterisks.
    assert "<strong>Done.</strong>" in resp.text
    assert "**Done.**" not in resp.text
    assert 'hx-swap-oob="true"' in resp.text
    assert "New course advice." in resp.text

    db.expire_all()
    refreshed = latest_course_report(db, course)
    assert refreshed.solutions_md == "New course advice."


def test_report_chat_explains_without_editing(client, db, course, member, monkeypatch):
    """Asking WHY (a question) must not rewrite any block — explaining ≠ editing."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    before = report.solutions_md

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        # A question turn: reply only, no edits.
        return json.dumps(
            {"action": "answer", "reply": "It's flagged because the rule on AI policy failed.", "edits": []}
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "why is this flagged?", "history": "[]"},
    )
    assert resp.status_code == 200
    assert "flagged because" in resp.text
    # No live block swap, and the stored suggestion is unchanged.
    assert 'hx-swap-oob="true"' not in resp.text
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == before


def test_answer_action_cannot_apply_an_edit(client, db, course, member, monkeypatch):
    """The action field is authoritative: semantically inconsistent model JSON
    must not turn an explanation into a silent report mutation."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    before = report.solutions_md

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        return json.dumps({
            "action": "answer",
            "edits": [{"target": "course", "markdown": "MUST NOT LAND"}],
            "reply": "This is only an explanation.",
        })

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "why is this flagged?", "history": "[]"},
    )
    assert resp.status_code == 200
    assert 'hx-swap-oob="true"' not in resp.text
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == before


def test_report_chat_noop_edit_is_not_reported_as_updated(
    client, db, course, member, monkeypatch
):
    """Safety net: if the model returns an `edits` entry that repeats the block
    verbatim, the chat must NOT claim it updated anything — no live swap and no
    "Updated" line — so an "Updated" badge always means a real change. (The prompt
    pushes the model to make a genuine change or to explain instead; this only
    guards the rare no-op slipping through.)"""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    current = report.solutions_md  # what the model will echo back verbatim

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        return json.dumps(
            {
                "action": "edit",
                "reply": "That suggestion is already solid as written.",
                "edits": [{"target": "course", "markdown": current}],
            }
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "rewrite the advice", "history": "[]"},
    )
    assert resp.status_code == 200
    # No block was swapped live and no "Updated:" line, because nothing changed.
    assert 'hx-swap-oob="true"' not in resp.text
    assert "Updated:" not in resp.text
    assert "identical to the block" in resp.text
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == current


def test_edited_suggestion_is_reflected_in_downloaded_pdf(client, db, course, member, monkeypatch):
    """End-to-end: editing a suggestion block changes what the PDF download
    contains. The PDF is built live from the stored Markdown, so the bytes before
    and after the edit must differ, and the new text must be the source the PDF
    renders from (verified via the stored block, which the renderer reads)."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    before = client.get(f"/courses/{course.id}/report/view/pdf").content
    assert before.startswith(b"%PDF")

    marker = "ZEBRA UNIQUE ADVICE 42"
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/solutions",
        data={"solutions_md": f"- {marker}"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    db.expire_all()
    # The stored Markdown the PDF renders from now holds the edit...
    assert latest_course_report(db, course).solutions_md == f"- {marker}"
    # ...and the downloaded PDF actually changes as a result (built live, not cached).
    after = client.get(f"/courses/{course.id}/report/view/pdf").content
    assert after.startswith(b"%PDF")
    assert after != before


def test_chat_edit_is_reflected_in_downloaded_pdf(client, db, course, member, monkeypatch):
    """Editing through the assistant chat also flows into the downloaded PDF."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    before = client.get(f"/courses/{course.id}/report/view/pdf").content

    marker = "GIRAFFE FROM CHAT 99"

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        return json.dumps(
            {"action": "edit", "reply": "Updated.", "edits": [{"target": "course", "markdown": f"- {marker}"}]}
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "make the course advice more concrete", "history": "[]"},
    )
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == f"- {marker}"
    after = client.get(f"/courses/{course.id}/report/view/pdf").content
    assert after.startswith(b"%PDF")
    assert after != before


def test_manual_block_edit_swaps_in_place_for_htmx(client, db, course, member, monkeypatch):
    """A manual Save of a suggestion block from the page is an HTMX request: it must
    return just that block (so it swaps in place and the professor keeps their
    scroll position), not a full-page redirect."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/solutions",
        data={"solutions_md": "Hand-edited course advice."},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    # Returns the single editable block (its stable dom id), not the whole page.
    assert 'id="editable-course"' in resp.text
    assert "Hand-edited course advice." in resp.text
    # In-place target swap, so it is NOT an out-of-band swap.
    assert 'hx-swap-oob="true"' not in resp.text
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == "Hand-edited course advice."


def test_manual_block_edit_redirects_without_htmx(client, db, course, member, monkeypatch):
    """A non-HTMX submit still works and falls back to a normal redirect."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/solutions",
        data={"solutions_md": "Plain submit advice."},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == "Plain submit advice."




def test_report_chat_keeps_every_block_but_condenses_other_findings(
    client, db, course, member, monkeypatch
):
    """An @-mention says which document to READ, not which block may be edited.

    Every suggestion block therefore stays in the prompt, so the professor can
    tag one document and still ask for a change to another ("fix @slides.pdf and
    the syllabus too"). Only the CHECKS of the other documents are condensed to
    their failures, which is what keeps a large course affordable without ever
    letting the assistant claim a document is missing from the report."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    from app.services.report_chat import (
        _build_mention_context,
        _editable_blocks,
        _findings_context,
        _scope_blocks,
        _scope_findings,
    )

    # A second document, so "everything stays" is a real claim and not vacuous.
    db.add(DocumentReportSection(
        course_report_id=report.id,
        document_id=999,
        filename="other.pdf",
        document_type="slide",
        issues_md="Other issue",
        solutions_md="Other advice",
    ))
    db.commit()
    db.expire_all()
    report = latest_course_report(db, course)

    mention = _build_mention_context(db, report, "rewrite @slides.pdf", [])
    blocks = _scope_blocks(_editable_blocks(report), mention)
    findings = _scope_findings(_findings_context(db, report), mention)

    # Every block remains editable, including the document that was not tagged.
    assert [b["label"] for b in blocks] == [
        "Course-level suggestions",
        "Suggestions for slides.pdf",
        "Suggestions for other.pdf",
    ]
    # The mentioned document keeps its full check list; any other document that
    # has findings is kept too, flagged as a condensed digest.
    by_name = {d["filename"]: d for d in findings}
    assert "slides.pdf" in by_name
    assert not by_name["slides.pdf"].get("summarised")
    for name, doc in by_name.items():
        if name != "slides.pdf":
            assert doc["summarised"] is True
            assert all(c["outcome"] == "fail" for c in doc["checks"])

def test_report_chat_at_mention_injects_document_text(
    client, db, course, member, monkeypatch
):
    """Typing @slides.pdf pulls that document's full markdown into the prompt and
    the turn confirms which file was read — so the assistant answers in the
    document's real context, one document at a time."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    captured = {}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        captured["prompt"] = prompt
        return json.dumps({"action": "answer", "reply": "Looking at the deck now.", "edits": []})

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "summarise @slides.pdf for me", "history": "[]"},
    )
    assert resp.status_code == 200
    # The prompt carries the referenced document's full text, clearly delimited.
    assert "@slides.pdf" in captured["prompt"]
    assert "--- slides.pdf ---" in captured["prompt"]
    assert "--- end of slides.pdf ---" in captured["prompt"]
    # The stub extractor's page markdown is injected as grounding.
    assert "this page is about risk assessment" in captured["prompt"]
    # The turn tells the professor which document was read.
    assert "slides.pdf" in resp.text
    assert "Read the full text" in resp.text


def test_at_mention_edit_routes_to_that_documents_block(
    client, db, course, member, monkeypatch
):
    """An @-mention on a CHANGE request resolves to that document's editable block
    target deterministically, and the prompt tells the model to edit THAT target —
    so the model cannot edit the course-level block by mistake or merely 'talk'."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    # The section target for the mentioned document, resolved by the service.
    from app.services.report_chat import _build_mention_context

    mention = _build_mention_context(db, report, "@slides.pdf rewrite it")
    assert mention.target is not None
    expected = next(s.id for s in report.sections if s.filename == mention.filename)
    assert mention.target == f"section:{expected}"

    captured = {}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        captured["prompt"] = prompt
        # Model does the right thing: edits the mentioned document's block.
        return json.dumps({"action": "edit", "reply": "Rewritten.",
                           "edits": [{"target": mention.target, "markdown": "New, sharper advice."}]})

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "@slides.pdf rewrite the suggestion to be more concrete", "history": "[]"},
    )
    assert resp.status_code == 200
    # The prompt explicitly names the target the @-mention maps to.
    assert mention.target in captured["prompt"]
    assert "maps to target" in captured["prompt"]


def test_undo_restores_block_after_chat_clobbers_it(
    client, db, course, member, monkeypatch
):
    """If an edit (chat or manual) overwrites a multi-part block, one Undo brings
    the previous text back, and a second click redoes it. This is the safety net
    that replaced the heuristic truncation guard."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    original = (
        "- Outcomes: rewrite a third to be higher-order.\n"
        "- Weights: essay 25%, paper 45%, presentation 30%.\n"
        "- Accessibility: add an accommodations section.\n"
        "- Sources: require five primary sources."
    )
    report.solutions_md = original
    report.solutions_md_prev = None
    db.commit()

    # The model clobbers the block, keeping only the outcomes part.
    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        return json.dumps({"action": "edit", "reply": "Rewrote the outcomes.",
                           "edits": [{"target": "course",
                                      "markdown": "- Outcomes: analyse, evaluate, create."}]})

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "rewrite the outcomes", "history": "[]"},
    )
    db.expire_all()
    # The block was overwritten (the clobber happened) but prev is saved.
    assert "Weights" not in latest_course_report(db, course).solutions_md

    # Undo restores the full original, with all four suggestions back.
    resp = client.post(f"/courses/{course.id}/report/{report.id}/solutions/undo")
    assert resp.status_code == 200
    db.expire_all()
    restored = latest_course_report(db, course).solutions_md
    assert restored == original
    assert "Weights" in restored and "Accessibility" in restored

    # A second Undo redoes it (one-step swap is reversible).
    client.post(f"/courses/{course.id}/report/{report.id}/solutions/undo")
    db.expire_all()
    assert "Weights" not in latest_course_report(db, course).solutions_md


def test_duplicate_target_is_one_undo_redo_step(
    client, db, course, member, monkeypatch
):
    """If guided JSON repeats a target, the final value wins but the prior
    value remains the professor's pre-turn text: one Undo back, one Redo forward."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    original = report.solutions_md

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        return json.dumps({
            "action": "edit",
            "edits": [
                {"target": "course", "markdown": "intermediate model draft"},
                {"target": "course", "markdown": "final model draft"},
            ],
            "reply": "Updated.",
        })

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "rewrite the course suggestions", "history": "[]"},
    )
    db.expire_all()
    fresh = latest_course_report(db, course)
    assert fresh.solutions_md == "final model draft"
    assert fresh.solutions_md_prev == original

    client.post(f"/courses/{course.id}/report/{report.id}/solutions/undo")
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == original
    client.post(f"/courses/{course.id}/report/{report.id}/solutions/undo")
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == "final model draft"

def test_mention_parsing_handles_bare_tokens_quotes_and_emails():
    """@-mention parsing: extension, bare token (prefix-resolved later), quoted
    name with spaces — but never an email address."""
    from app.services.report_chat import _mentioned_names

    assert _mentioned_names("rewrite @syllabus.pdf now") == ["syllabus.pdf"]
    assert _mentioned_names("open @07_rubric and stop") == ["07_rubric"]
    assert _mentioned_names('see @"two words.pdf" here') == ["two words.pdf"]
    assert _mentioned_names("dedup @a.pdf and @a.pdf") == ["a.pdf"]
    # An email is not a mention (the "@" is preceded by a word character).
    assert _mentioned_names("mail me at prof@uni.edu") == []
    # A quoted mention works even attached to a word (Italian "dell'@…", or a
    # word run right up against it) — you never quote an email, so it is safe.
    assert _mentioned_names('parlami dell\'@"Assessment 1.pdf"') == ["Assessment 1.pdf"]
    assert _mentioned_names('x@"Assessment 1.pdf"') == ["Assessment 1.pdf"]


def test_chat_prompt_budget_sheds_oldest_history_then_clips_document():
    """The chat prompt is kept within the model's context: oversized history sheds
    its OLDEST turns first (the current message always survives), and only then is
    an @-mentioned document clipped."""
    from app.services.report_chat import (
        MentionContext,
        _fit_chat_prompt,
        _prompt_char_budget,
    )

    budget = _prompt_char_budget()
    blocks = [{"target": "course", "label": "L", "markdown": "advice"}]
    no_doc = MentionContext(filename=None, markdown="", unresolved=[], ignored=[])
    # 12 turns each ~1/5 of the budget → ~2.4× over → must shed oldest.
    per = budget // 5
    hist = [{"role": "user", "content": ("X" * per) + f"__turn{i}"} for i in range(12)]

    prompt = _fit_chat_prompt(blocks, [], "CURRENT-MSG", hist, no_doc)
    assert len(prompt) <= budget
    assert "CURRENT-MSG" in prompt  # the current message is never trimmed
    assert "__turn11" in prompt and "__turn0" not in prompt  # recent kept, oldest shed

    # Oversized document on top of oversized history: both are reduced to fit.
    doc = MentionContext(
        filename="big.pdf", markdown="D" * (budget * 2), unresolved=[], ignored=[]
    )
    prompt2 = _fit_chat_prompt(blocks, [], "MSG", hist, doc)
    assert len(prompt2) <= budget
    assert "MSG" in prompt2 and "truncated for length" in prompt2


def test_report_chat_unresolved_mention_is_reported(
    client, db, course, member, monkeypatch
):
    """An @-name matching none of the course's documents is surfaced, not silently
    dropped, and no document text is injected."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    captured = {}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        captured["prompt"] = prompt
        return json.dumps({"action": "answer", "reply": "I don't see that document.", "edits": []})

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "open @missing.pdf", "history": "[]"},
    )
    assert resp.status_code == 200
    assert "Couldn't find" in resp.text and "missing.pdf" in resp.text
    # No document block was injected.
    assert "--- missing.pdf ---" not in captured["prompt"]


def test_report_chat_degrades_gracefully_when_model_unavailable(
    client, db, course, member, monkeypatch
):
    """If the model call fails (endpoint down/overloaded after retries), the chat
    returns a calm retryable message instead of a 500, and edits nothing."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    before = report.solutions_md

    def boom(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        raise RuntimeError("model endpoint unreachable")

    monkeypatch.setattr("app.llm.client.LLMClient.generate", boom)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "rewrite the advice", "history": "[]"},
    )
    assert resp.status_code == 200
    assert "temporarily unavailable" in resp.text.lower()
    assert 'hx-swap-oob="true"' not in resp.text  # nothing edited
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == before


def test_report_chat_reads_one_document_per_message(
    client, db, course, member, monkeypatch
):
    """Two resolvable @-mentions: only the first is read into context; the turn
    tells the professor to ask about the other separately (one at a time)."""
    _run(client, db, course, monkeypatch)
    # Add a second document so two mentions can both resolve.
    upload_pdf(client, course.id, "rubric.pdf", make_pdf(["Levels"]))
    db.expire_all()
    report = latest_course_report(db, course)

    captured = {}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        captured["prompt"] = prompt
        return json.dumps({"action": "answer", "reply": "Reading the first one.", "edits": []})

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "compare @slides.pdf and @rubric.pdf", "history": "[]"},
    )
    assert resp.status_code == 200
    # The first document is injected; the second is not.
    assert "--- slides.pdf ---" in captured["prompt"]
    assert "--- rubric.pdf ---" not in captured["prompt"]
    # The professor is told only one was read and to ask about the other.
    assert "one document's full text is read per message" in resp.text.lower()
    assert "rubric.pdf" in resp.text


def test_render_markdown_flattens_latex_math_to_plain_text():
    """The model sometimes wraps numbers/units in LaTeX; the professor must never
    see $…$, \\text{}, or \\times — only the plain reading."""
    from app.templating import render_markdown, strip_latex

    # The carbon-footprint example from a real run.
    assert strip_latex(r"$0.20\text{ kg CO}_2$ per km") == "0.20 kg CO₂ per km"
    assert strip_latex(r"$60 \times 0.20 = 12\text{ kg CO}_2$") == "60 × 0.20 = 12 kg CO₂"
    assert strip_latex(r"area $x^2$ and ref $a_{12}$") == "area x² and ref a₁₂"
    # No leftover LaTeX syntax once rendered to HTML.
    html = str(render_markdown(r"The cost is $12\text{ kg CO}_2$ total."))
    assert "$" not in html and "text{" not in html and "\\times" not in html
    assert "12 kg CO₂" in html
    # Legitimate Markdown (emphasis, filenames with underscores) is untouched.
    assert strip_latex("a **bold** and _file_name_.pdf") == "a **bold** and _file_name_.pdf"


def test_clean_markdown_repairs_brace_pseudo_list():
    """A {"a","b"} field the small model sometimes emits becomes a Markdown list."""
    from app.services.markdown_utils import clean_markdown

    out = clean_markdown('{"Revise the syllabus.","Add a proofreading pass."}')
    assert out == "- Revise the syllabus.\n- Add a proofreading pass."
    # Bracket form too, and prose / real lists are left untouched.
    assert clean_markdown('["one","two"]') == "- one\n- two"
    assert clean_markdown("Plain **prose** advice.") == "Plain **prose** advice."
    assert clean_markdown("- already\n- a list") == "- already\n- a list"


def test_clean_markdown_collapses_doubled_bullet_markers():
    """A "- - text" line the small model sometimes emits renders as a stray empty
    bullet; collapse the doubled marker to a single one (indentation preserved)."""
    from app.services.markdown_utils import clean_markdown

    assert clean_markdown("- - **Label:** body") == "- **Label:** body"
    assert clean_markdown("  - - indented") == "  - indented"
    # A genuine single bullet and prose are untouched.
    assert clean_markdown("- one\n- two") == "- one\n- two"


def test_render_markdown_flattens_doubled_bullet_nesting():
    """A "- - text" glitch renders as a bullet whose only child is another bullet
    ("•" then "◦") — a needless second indent level. render_markdown repairs it on
    read (for rows stored before the write-time repair existed), while leaving a
    genuinely nested list alone."""
    from app.templating import render_markdown

    flat = render_markdown("- - **Label:** body text")
    assert flat.count("<ul>") == 1
    assert "<ul>\n<li>\n<ul>" not in flat
    # A real two-level list keeps both levels.
    assert render_markdown("- parent\n    - child").count("<ul>") == 2


def test_alignment_evidence_needs_both_syllabus_and_assessment():
    """The constructive-alignment evidence is built only when there is both a
    syllabus (the objectives) and at least one assessment (what is tested) with
    real text — otherwise there is nothing to align and the section is omitted."""
    from types import SimpleNamespace

    from app.services.report_generation import _alignment_evidence

    def doc(doc_id, dtype):
        return SimpleNamespace(document_id=doc_id, filename=f"{dtype}.pdf", document_type=dtype)

    syllabus = doc(1, "syllabus")
    assessment = doc(2, "assessment")
    rubric = doc(3, "rubric")
    md = {1: "Objective: evaluate fairness.", 2: "Write an essay analysing bias.", 3: "criteria"}

    # Both present -> evidence, carrying the real grounding text.
    view = SimpleNamespace(documents=[syllabus, assessment, rubric])
    ev = _alignment_evidence(view, md)
    assert ev is not None
    assert ev["syllabus_filename"] == "syllabus.pdf"
    assert "evaluate fairness" in ev["syllabus_text"]
    assert ev["assessments"][0]["filename"] == "assessment.pdf"
    assert "analysing bias" in ev["assessments"][0]["text"]

    # No assessment -> None (nothing to align against).
    assert _alignment_evidence(SimpleNamespace(documents=[syllabus, rubric]), md) is None
    # No syllabus -> None (no objectives to align to).
    assert _alignment_evidence(SimpleNamespace(documents=[assessment, rubric]), md) is None
    # Syllabus present but empty markdown -> None (no objectives text to ground on).
    assert _alignment_evidence(SimpleNamespace(documents=[syllabus, assessment]), {1: "  ", 2: "x"}) is None


def test_fit_markdown_leaves_normal_documents_untouched():
    """A realistic document fits the context window, so its markdown is passed
    through verbatim — the budgeting path is a no-op in the common case."""
    from app.services.report_generation import _fit_markdown_to_context

    md = "## Page 1\n" + ("Some real syllabus prose. " * 200)  # a few KB
    rendered = _fit_markdown_to_context(lambda body: f"FIXED PROMPT\n{body}", md)
    assert md in rendered  # nothing clipped
    assert "truncated" not in rendered


def test_fit_markdown_clips_only_an_oversized_document():
    """A document large enough to overflow the model's context is clipped — but
    only its markdown, and with a visible marker, so the run never fails with a
    hard context-overflow 400 and the verdicts (the fixed part) are untouched."""
    from app.config import get_settings
    from app.llm.prompts import CHARS_PER_TOKEN
    from app.services.report_generation import _fit_markdown_to_context

    settings = get_settings()
    char_budget = int(
        (settings.generation_max_model_len - settings.report_output_token_budget)
        * CHARS_PER_TOKEN
    )
    fixed = "VERDICTS AND INSTRUCTIONS (must never be cut)\n"
    huge = "x" * (char_budget * 2)  # twice the whole budget
    rendered = _fit_markdown_to_context(lambda body: fixed + body, huge)

    assert fixed in rendered  # the authoritative part survives
    assert "[document text truncated for length]" in rendered
    assert len(rendered) <= char_budget  # the prompt now fits the window
    assert len(rendered) < len(fixed + huge)  # something was actually clipped


def test_normalize_field_coerces_lists_to_markdown():
    """The model sometimes returns a field as a real JSON array, not a string."""
    from app.services.report_generation import _normalize_field

    # A parsed list (json.loads turned the array into a Python list) -> bullets.
    assert _normalize_field(["First fix.", "Second fix."]) == "- First fix.\n- Second fix."
    # Empty / whitespace items are dropped.
    assert _normalize_field(["keep", "", "  "]) == "- keep"
    # Strings still pass through the pseudo-list repair, prose untouched.
    assert _normalize_field('{"a","b"}') == "- a\n- b"
    assert _normalize_field("Plain advice.") == "Plain advice."


def test_chat_schema_pins_targets_and_orders_edits_before_reply():
    """The per-turn schema is the structural fix for "says it edited but didn't":
    the model must commit to an action first and write `edits` BEFORE `reply`
    (guided decoding emits properties in schema order), and `target` is an enum
    of the report's real block ids so an invented target cannot be emitted."""
    from app.services.report_chat import _chat_schema

    schema = _chat_schema(["course", "section:1"])
    assert list(schema["properties"]) == ["action", "edits", "reply"]
    assert schema["properties"]["action"]["enum"] == ["edit", "answer"]
    target = schema["properties"]["edits"]["items"]["properties"]["target"]
    assert target["enum"] == ["course", "section:1"]




def test_report_chat_question_turn_keeps_answer_possible(client, db, course, member, monkeypatch):
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    captured = {}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        captured["schema"] = extra_body["guided_json"]
        return json.dumps({"action": "answer", "edits": [], "reply": "Because the rule failed."})

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "why is this flagged?", "history": "[]"},
    )

    assert resp.status_code == 200
    assert captured["schema"]["properties"]["action"]["enum"] == ["edit", "answer"]
    assert "minItems" not in captured["schema"]["properties"]["edits"]

def test_report_chat_retries_when_edit_intent_lands_no_edit(
    client, db, course, member, monkeypatch
):
    """The observed failure: the model says "ecco il blocco aggiornato" while
    `edits` is empty. Declaring "action": "edit" with no usable edit triggers ONE
    corrective retry that shows the model its own reply (the new text often lives
    there), and the retry's edit is applied and reported."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    calls = []

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        calls.append((prompt, extra_body))
        if len(calls) == 1:
            return json.dumps(
                {
                    "action": "edit",
                    "edits": [],
                    "reply": "Ecco il blocco aggiornato con gli esempi.",
                }
            )
        return json.dumps(
            {
                "action": "edit",
                "edits": [{"target": "course", "markdown": "Advice with worked examples."}],
                "reply": "Ho aggiunto un esempio per ogni formula.",
            }
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "aggiungi un esempio per ogni formula", "history": "[]"},
    )
    assert resp.status_code == 200
    assert len(calls) == 2
    # The retry carries the model's own empty-handed reply back to it.
    assert "Ecco il blocco aggiornato" in calls[1][0]
    # …and the retry grammar leaves no way out: the action is pinned to "edit"
    # and at least one edits entry must be decoded.
    retry_schema = calls[1][1]["guided_json"]
    assert retry_schema["properties"]["action"]["enum"] == ["edit"]
    assert retry_schema["properties"]["edits"]["minItems"] == 1
    # Because the message is clearly an edit request, the first call is already
    # pinned to an edit grammar instead of asking the model to choose answer/edit.
    first_schema = calls[0][1]["guided_json"]
    assert first_schema["properties"]["action"]["enum"] == ["edit"]
    assert first_schema["properties"]["edits"]["minItems"] == 1
    # Every chat call caps its output, so a guided-decoding runaway can never
    # generate until the wall-clock timeout kills the request. The chat gets its
    # own (much larger) budget: a rewritten block full of worked examples needs
    # far more room than a report pass.
    from app.config import get_settings

    settings = get_settings()
    assert all(
        body["max_tokens"] == settings.chat_output_token_budget for _, body in calls
    )
    assert settings.chat_output_token_budget > settings.report_output_token_budget
    # The retry's edit really landed: live swap + persisted block.
    assert 'hx-swap-oob="true"' in resp.text
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == "Advice with worked examples."


def test_report_chat_never_claims_an_edit_it_did_not_make(
    client, db, course, member, monkeypatch
):
    """If even the corrective retry produces no edit, the professor gets an honest
    "nothing was changed" — never a reply that claims an update that didn't happen."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    before = report.solutions_md

    calls = []

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        calls.append(prompt)
        return json.dumps(
            {"action": "edit", "edits": [], "reply": "Ho aggiornato il blocco."}
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "fallo effettivamente", "history": "[]"},
    )
    assert resp.status_code == 200
    assert len(calls) == 2  # one corrective retry, then we stop
    # The lying reply is replaced by the honest fallback; nothing swapped or stored.
    assert "Ho aggiornato" not in resp.text
    assert "nothing was changed" in resp.text
    assert 'hx-swap-oob="true"' not in resp.text
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == before


def test_chat_followup_without_mention_keeps_last_documents_target(
    client, db, course, member, monkeypatch
):
    """A follow-up like "fallo effettivamente" has no @-mention, but the professor
    mentioned the document a turn ago: the target hint must survive from the
    history so the edit routes to THAT document's block, not one the model picks."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    from app.services.report_chat import _build_mention_context

    history = [
        {"role": "user", "content": "per @slides.pdf aggiungi un esempio per formula"},
        {"role": "assistant", "content": "Ecco gli esempi per ogni formula…"},
    ]
    mention = _build_mention_context(
        db, report, "non lo hai fatto, fallo effettivamente", history
    )
    expected = next(s.id for s in report.sections if s.filename == "slides.pdf")
    assert mention.target == f"section:{expected}"
    # The document text is re-injected: the browser history only contains visible
    # chat turns, not the hidden full-text context from the previous prompt.
    assert mention.filename == "slides.pdf"
    assert "risk assessment" in mention.markdown

    # End-to-end the prompt tells the model which target the follow-up means and
    # still carries the source text needed to make the edit concrete.
    captured = {}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        captured["prompt"] = prompt
        return json.dumps(
            {
                "action": "edit",
                "edits": [{"target": mention.target, "markdown": "Concrete examples added."}],
                "reply": "Fatto.",
            }
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={
            "message": "non lo hai fatto, fallo effettivamente",
            "history": json.dumps(history),
        },
    )
    assert resp.status_code == 200
    assert f'maps to target "{mention.target}"' in captured["prompt"]
    assert 'Full text of "@slides.pdf"' in captured["prompt"]
    assert "risk assessment" in captured["prompt"]
    db.expire_all()
    section = next(
        s for s in latest_course_report(db, course).sections if s.filename == "slides.pdf"
    )
    assert section.solutions_md == "Concrete examples added."


def test_report_chat_prompt_includes_read_only_issues_for_each_suggestion(
    client, db, course, member, monkeypatch
):
    """A refinement like "add one example for every formula" needs the exact
    issue list the report already produced. The prompt must therefore carry the
    immutable issues beside the editable suggestion block; otherwise the model
    only sees generic advice and has to rediscover the quantity from scratch."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    captured = {}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        captured["prompt"] = prompt
        return json.dumps(
            {
                "action": "answer",
                "edits": [],
                "reply": "I can see the fixed issue list.",
            }
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "what exact issue is behind the slides suggestion?", "history": "[]"},
    )
    assert resp.status_code == 200
    # The report's own issue text and the block being edited both reach the
    # model; the wording of the prompt's labels is not what is under test.
    prompt = captured["prompt"]
    assert "risk assessment slide." in prompt
    assert "Remove it." in prompt


def test_report_chat_echo_after_retry_gets_the_already_covered_reply(
    client, db, course, member, monkeypatch
):
    """The live failure of 2026-07-06: the forced retry DID return an edit, but
    its markdown was byte-identical to the block (the block already described
    what was asked). The professor must be told THAT — the block already covers
    it, ask for the next level of detail — not a bare "nothing was changed"."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    current = report.solutions_md  # what the model echoes back, twice

    calls = []

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        calls.append(prompt)
        return json.dumps(
            {
                "action": "edit",
                "edits": [{"target": "course", "markdown": current}],
                "reply": "Ecco il blocco aggiornato.",
            }
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "riporta un esempio per ogni formula", "history": "[]"},
    )
    assert resp.status_code == 200
    assert len(calls) == 2
    # The echo-specific honest reply, no claimed update, block untouched.
    assert "already cover" in resp.text
    assert "Ecco il blocco aggiornato" not in resp.text
    assert 'hx-swap-oob="true"' not in resp.text
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == current


def test_edited_labels_parses_updated_markers():
    """The "[Updated: …]" records the UI appends to assistant turns are parsed
    back into an ordered, de-duplicated list of block labels — the authoritative
    answer to "what did you change?"."""
    from app.services.report_chat import _edited_labels

    history = [
        {"role": "user", "content": "rewrite @slides.pdf"},
        {
            "role": "assistant",
            "content": "Fatto. [Updated: Suggestions for slides.pdf.]",
        },
        {"role": "user", "content": "also the course advice"},
        {
            "role": "assistant",
            "content": "Done. [Updated: Course-level suggestions, Suggestions for slides.pdf.]",
        },
        {"role": "assistant", "content": "Just an explanation, no marker."},
    ]
    assert _edited_labels(history) == [
        "Suggestions for slides.pdf",
        "Course-level suggestions",
    ]
    assert _edited_labels([]) == []


def test_chat_prompt_carries_authoritative_edited_list(
    client, db, course, member, monkeypatch
):
    """"Cosa hai modificato?" must be answered from facts: the prompt carries a
    server-derived list of the blocks THIS conversation edited, and instructs the
    model to claim those edits and nothing else — so it can neither deny its own
    edit nor present the whole report's suggestions as its changes."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    captured = {}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        captured["prompt"] = prompt
        return json.dumps(
            {"action": "answer", "edits": [], "reply": "Ho modificato solo slides.pdf."}
        )

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    history = [
        {"role": "user", "content": "rewrite the suggestions for @slides.pdf"},
        {"role": "assistant", "content": "Fatto. [Updated: Suggestions for slides.pdf.]"},
    ]
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "mi fai un riassunto di cosa hai modificato?",
              "history": json.dumps(history)},
    )
    assert resp.status_code == 200
    # The block this conversation edited is named in the prompt, derived from
    # the history by the server rather than left to the model to remember.
    assert "Suggestions for slides.pdf" in captured["prompt"]

    # A conversation that never edited anything says so explicitly.
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "cosa hai modificato?",
              "history": json.dumps([{"role": "user", "content": "ciao"},
                                     {"role": "assistant", "content": "Ciao!"}])},
    )
    assert resp.status_code == 200
    assert "you have not modified anything in this conversation yet" in captured["prompt"]


def test_parse_chat_history_preserves_summary_timestamps_and_no_hard_turn_cap():
    """Long chat memory is compacted deliberately, not silently sliced to 12 turns."""
    from app.routers.courses import _parse_chat_history

    raw = [
        {"role": "summary", "content": "Older context mentions @slides.pdf", "at": 1000},
        *[{"role": "user", "content": f"turn {i}", "at": 2000 + i} for i in range(20)],
    ]

    parsed = _parse_chat_history(json.dumps(raw))
    assert len(parsed) == 21
    assert parsed[0]["role"] == "summary"
    assert parsed[0]["content"] == "Older context mentions @slides.pdf"
    assert parsed[0]["at"] == 1000
    assert parsed[0]["time"].startswith("1970-01-01T00:00:01")
    assert parsed[-1]["content"] == "turn 19"
    assert parsed[-1]["time"]


def test_parse_chat_history_ignores_valid_non_array_json():
    from app.routers.courses import _parse_chat_history

    for raw in ("null", "42", '"hello"', "{}"):
        assert _parse_chat_history(raw) == []


def test_report_chat_rejects_a_message_over_character_limit():
    from app.llm.prompts import CHAT_MESSAGE_MAX_CHARS
    from app.services.report_chat import run_report_chat

    result = run_report_chat(None, None, "x" * (CHAT_MESSAGE_MAX_CHARS + 1), [])
    assert result.changed == set()
    assert "too long" in result.reply.lower()
    assert "16,000 characters" in result.reply


def test_chat_compaction_keeps_recent_turns_and_preserves_report_markers(monkeypatch):
    """The model writes the compact prose, but exact edit markers and document
    mentions are appended deterministically so report follow-ups stay grounded."""
    from app.services import chat_compaction

    class StubClient:
        is_configured = True

        def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
            assert "@slides.pdf" in prompt  # the turns to compact reached it
            return "The professor wanted examples added to the slides."

    monkeypatch.setattr(chat_compaction, "history_char_threshold", lambda: 100)
    monkeypatch.setattr(chat_compaction, "get_generation_client", lambda: StubClient())
    history = [
        {"role": "user", "content": "please edit @slides.pdf " + ("x" * 80), "at": 1000},
        {"role": "assistant", "content": "Done. [Updated: Suggestions for slides.pdf.]", "at": 1001},
        *[{"role": "user", "content": f"recent {i}", "at": 2000 + i} for i in range(7)],
    ]

    result = chat_compaction.compact_history(history, "report refinement")

    assert result.compacted is True
    assert result.compacted_until_at == 2000
    assert len(result.history) == 7  # one hidden summary + six recent turns
    summary = result.history[0]
    assert summary["role"] == "summary"
    assert "[Updated: Suggestions for slides.pdf.]" in summary["content"]
    assert "@slides.pdf" in summary["content"]
    assert [t["content"] for t in result.history[1:]][0] == "recent 1"


def test_report_chat_prompt_renders_compacted_memory_separately():
    from app.services.report_chat import MentionContext, _fit_chat_prompt

    history = [
        {"role": "summary", "content": "Older context: professor mentioned @slides.pdf."},
        {"role": "user", "content": "what did we decide?", "time": "2026-07-08T10:00:00+00:00"},
    ]
    prompt = _fit_chat_prompt(
        [{"target": "course", "label": "Course", "markdown": "Advice", "issues": "Issue"}],
        [],
        "continue",
        history,
        MentionContext(unresolved=[]),
    )

    # Both the compacted memory and the recent turn reach the model.
    assert "Older context: professor mentioned @slides.pdf." in prompt
    assert "[2026-07-08T10:00:00+00:00] Professor: what did we decide?" in prompt


def test_report_chat_compact_endpoint_returns_payload(client, db, course, member, monkeypatch):
    from types import SimpleNamespace

    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    def fake_compact(history, chat_kind):
        assert chat_kind == "report refinement"
        assert history[0]["role"] == "user"
        return SimpleNamespace(as_payload=lambda: {
            "compacted": True,
            "history": [{"role": "summary", "content": "short"}],
            "compacted_until_at": 123,
            "message": "ok",
        })

    monkeypatch.setattr("app.routers.courses.compact_history", fake_compact)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat/compact",
        data={"history": json.dumps([{"role": "user", "content": "hello", "at": 123}])},
    )

    assert resp.status_code == 200
    assert resp.json()["compacted"] is True
    assert resp.json()["history"][0]["role"] == "summary"


def test_report_chat_empty_message_is_a_no_op(client, db, course, member, monkeypatch):
    """Send on an empty box must not put a validation blob in the conversation.

    This endpoint's body is swapped into the chat log, so a required-field
    rejection rendered FastAPI's raw JSON as if the assistant had said it. It
    also must not reach the model: an empty prompt costs a call and answers
    nothing.
    """
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    def boom(self, prompt, **kw):
        raise AssertionError("the model must not be called for an empty message")

    monkeypatch.setattr("app.llm.client.LLMClient.generate", boom)
    for body in ({}, {"message": ""}, {"message": "   "}):
        resp = client.post(
            f"/courses/{course.id}/report/{report.id}/chat", data=body
        )
        assert resp.status_code == 204, body
        assert resp.text.strip() == ""


def test_undo_then_redo_steps_the_text_back_and_forward(
    client, db, course, member, monkeypatch
):
    """One press restores the old text, a second brings the new one back.

    The stored state is a plain swap of current/previous, so it carries no
    direction: the control posts the direction it is currently offering and the
    route flips it. Asserted on the TEXT that comes back and on the flag the
    control will post next, not on its wording, which is free to change.
    """
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    hx = {"HX-Request": "true"}
    base = f"/courses/{course.id}/report/{report.id}"
    original = report.solutions_md

    # An edit creates the history the control steps through.
    first = client.post(
        f"{base}/solutions", data={"solutions_md": "Version two."}, headers=hx
    )
    assert '"undone": false' in first.text  # next press will undo

    # Undo: the previous text is back, and the next press would redo.
    undone = client.post(f"{base}/solutions/undo", data={"undone": "false"}, headers=hx)
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == original
    assert '"undone": true' in undone.text

    # Redo: the newer text returns, and the next press would undo again.
    redone = client.post(f"{base}/solutions/undo", data={"undone": "true"}, headers=hx)
    db.expire_all()
    assert latest_course_report(db, course).solutions_md == "Version two."
    assert '"undone": false' in redone.text


def test_prose_filename_switches_scope_to_that_document(
    client, db, course, member, monkeypatch
):
    """Naming another document in PROSE ("modifica l'assessment 1") re-points the
    turn at that document, even though an earlier turn @-mentioned a different
    one. Before this, the mention carried from history kept the scope, the other
    document's block never reached the model, and the assistant answered that a
    document the professor had just named was not in the report."""
    _run(client, db, course, monkeypatch)
    upload_pdf(client, course.id, "Assessment 1 - Short Essay.pdf", make_pdf(["Essay"]))
    db.expire_all()
    report = latest_course_report(db, course)
    section = DocumentReportSection(
        course_report_id=report.id,
        document_id=next(
            d.id for d in course.documents
            if d.filename == "Assessment 1 - Short Essay.pdf"
        ),
        filename="Assessment 1 - Short Essay.pdf",
        document_type="assessment",
        issues_md="No AI policy.",
        solutions_md="Old advice.",
    )
    db.add(section)
    db.commit()
    db.expire_all()
    report = latest_course_report(db, course)

    from app.services.report_chat import _build_mention_context

    history = [
        {"role": "user", "content": "explain @slides.pdf"},
        {"role": "assistant", "content": "Here are the findings for the deck."},
    ]
    mention = _build_mention_context(
        db, report, "modifica i suggerimenti dell'assessment 1", history
    )
    assert mention.filename == "Assessment 1 - Short Essay.pdf"
    # Named here, so the turn points at it; but no @, so it is not read.
    assert mention.from_prose is True
    assert mention.markdown == ""


def test_prose_match_ignores_vague_and_ambiguous_references(
    client, db, course, member, monkeypatch
):
    """The prose matcher must not invent a document. Ordinary chat keeps the
    scope inherited from history, so a wrong block is never edited."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)

    from app.services.report_chat import _build_mention_context

    history = [{"role": "user", "content": "explain @slides.pdf"}]
    for vague in ("rendile piu concrete", "sistema tutto", "va bene cosi"):
        mention = _build_mention_context(db, report, vague, history)
        # Inherited from history, not matched from prose.
        assert mention.filename == "slides.pdf"
        assert mention.from_prose is False


def test_at_mention_does_not_block_edits_to_other_blocks(
    client, db, course, member, monkeypatch
):
    """@-mentioning a document says which one to READ, not which one may be
    written. "fix @slides.pdf and the course-level advice too" must edit BOTH.

    The enum used to be pinned to the mentioned document, so the second half of
    such a request was impossible to express and the assistant replied that it
    could not do it. Every block stays writable for exactly this reason."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    slides_id = next(s.id for s in report.sections if s.filename == "slides.pdf")

    captured = {}

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        captured["schema"] = (extra_body or {}).get("guided_json")
        return json.dumps({
            "action": "edit",
            "edits": [
                {"target": f"section:{slides_id}", "markdown": "Sharper deck advice."},
                {"target": "course", "markdown": "Sharper course advice."},
            ],
            "reply": "Updated both.",
        })

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={
            "message": "fix @slides.pdf and the course-level advice too",
            "history": "[]",
        },
    )
    assert resp.status_code == 200
    # The grammar offered every block, not just the mentioned document's.
    enum = captured["schema"]["properties"]["edits"]["items"]["properties"]["target"]["enum"]
    assert "course" in enum and f"section:{slides_id}" in enum
    db.expire_all()
    fresh = latest_course_report(db, course)
    assert next(s for s in fresh.sections if s.id == slides_id).solutions_md == (
        "Sharper deck advice."
    )
    assert fresh.solutions_md == "Sharper course advice."


def test_chat_drops_edits_on_targets_that_do_not_exist(
    client, db, course, member, monkeypatch
):
    """A target the model invented (or a stale id from an earlier report) is
    discarded rather than silently dropping the whole turn. This is a guard
    against bad ids, NOT a restriction on which real blocks may be edited."""
    _run(client, db, course, monkeypatch)
    db.expire_all()
    report = latest_course_report(db, course)
    slides_id = next(s.id for s in report.sections if s.filename == "slides.pdf")

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        return json.dumps({
            "action": "edit",
            "edits": [
                {"target": f"section:{slides_id}", "markdown": "Real edit."},
                {"target": "section:999999", "markdown": "SHOULD NOT BE SAVED"},
            ],
            "reply": "Done.",
        })

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)
    resp = client.post(
        f"/courses/{course.id}/report/{report.id}/chat",
        data={"message": "rendi piu concreto @slides.pdf", "history": "[]"},
    )
    assert resp.status_code == 200
    db.expire_all()
    fresh = latest_course_report(db, course)
    assert next(s for s in fresh.sections if s.id == slides_id).solutions_md == "Real edit."
