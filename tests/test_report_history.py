"""Deterministic report triage and the history diff between two runs."""

import json
from types import SimpleNamespace

from app.models import RuleCategory, RuleSeverity
from app.services.report_history import build_snapshot, diff_reports
from app.services.report_view import effective_severity
from app.services.report_priorities import (
    QUICK_WIN,
    STRUCTURAL,
    collect_failures,
    effort_class,
    top_priorities,
)


def _verdict(outcome, severity, category, filename="d.pdf", quotes=None,
             document_type="assessment", rule_type="assessment", code=None):
    rule = SimpleNamespace(
        code=code, text=f"rule for {category}", rationale="why",
        severity=severity, category=category, document_type=rule_type,
        cel="true", extraction_contract="[]",
    )
    facts = [
        SimpleNamespace(
            field="observed_condition",
            value=outcome == "pass",
            quotes=quotes or [],
            gate=False,
        )
    ]
    # Mirror RuleVerdict.severity: softened when an assessment rule lands on a
    # slide, the rule's own severity otherwise.
    eff = effective_severity(rule, document_type)
    return SimpleNamespace(
        outcome=outcome, rule=rule, facts=facts, severity=eff,
        document_type=document_type,
    )


def _view(*docs):
    documents = []
    for index, (filename, verdicts) in enumerate(docs, start=1):
        documents.append(
            SimpleNamespace(
                document_id=index,
                document_version_id=100 + index,
                version_number=1,
                filename=filename,
                document_type="assessment",
                verdicts=verdicts,
            )
        )
    return SimpleNamespace(documents=documents)


def test_effective_severity_softens_assessment_rules_on_slides():
    rule = SimpleNamespace(
        severity=RuleSeverity.HIGH, category=RuleCategory.AI_READINESS,
        document_type="assessment",
    )
    # A real assessment keeps full severity; a slide+assessment deck softens it.
    assert effective_severity(rule, "assessment") == RuleSeverity.HIGH
    assert effective_severity(rule, "slide,assessment") == RuleSeverity.MEDIUM
    # A slide-native rule on the same deck is untouched.
    slide_rule = SimpleNamespace(
        severity=RuleSeverity.MEDIUM, category=RuleCategory.CONTENT,
        document_type="slide",
    )
    assert effective_severity(slide_rule, "slide,assessment") == RuleSeverity.MEDIUM


def test_effort_class_quick_win_vs_structural():
    high = _verdict("fail", RuleSeverity.HIGH, RuleCategory.CLARITY)
    low_clarity = _verdict("fail", RuleSeverity.LOW, RuleCategory.CLARITY)
    med_assessment = _verdict("fail", RuleSeverity.MEDIUM, RuleCategory.ASSESSMENT)
    assert effort_class(high) == STRUCTURAL  # any high is structural
    assert effort_class(low_clarity) == QUICK_WIN  # low + clarity is a polish fix
    assert effort_class(med_assessment) == STRUCTURAL  # assessment theme is structural


def test_collect_failures_orders_by_severity():
    view = _view(
        ("a.pdf", [
            _verdict("fail", RuleSeverity.LOW, RuleCategory.CLARITY),
            _verdict("pass", RuleSeverity.HIGH, RuleCategory.ASSESSMENT),
            _verdict("fail", RuleSeverity.HIGH, RuleCategory.AI_READINESS),
        ]),
    )
    items = collect_failures(view)
    # Only the two failures, HIGH first.
    assert [i.severity for i in items] == [RuleSeverity.HIGH, RuleSeverity.LOW]


def test_top_priorities_capped():
    fails = [_verdict("fail", RuleSeverity.MEDIUM, RuleCategory.CONTENT) for _ in range(8)]
    assert len(top_priorities(_view(("a.pdf", fails)), limit=5)) == 5


def _report(snapshot, metrics):
    return SimpleNamespace(
        snapshot_json=json.dumps(snapshot), metrics_json=json.dumps(metrics)
    )


def test_diff_reports_classifies_transitions_and_docs():
    older = _report(
        {
            "documents": [
                {"filename": "a.pdf", "document_type": "assessment", "verdicts": [
                    {"rule": "R1", "category": "ai_readiness", "severity": "high", "outcome": "fail"},
                    {"rule": "R2", "category": "clarity", "severity": "low", "outcome": "pass"},
                ]},
                {"filename": "gone.pdf", "document_type": "slide", "verdicts": [
                    {"rule": "R3", "category": "content", "severity": "medium", "outcome": "fail"},
                ]},
            ]
        },
        {"health_score": 40, "ai_readiness_score": 20},
    )
    newer = _report(
        {
            "documents": [
                {"filename": "a.pdf", "document_type": "assessment", "verdicts": [
                    {"rule": "R1", "category": "ai_readiness", "severity": "high", "outcome": "pass"},
                    {"rule": "R2", "category": "clarity", "severity": "low", "outcome": "fail"},
                ]},
                {"filename": "new.pdf", "document_type": "rubric", "verdicts": [
                    {"rule": "R4", "category": "assessment", "severity": "high", "outcome": "fail"},
                ]},
            ]
        },
        {"health_score": 65, "ai_readiness_score": 80},
    )
    diff = diff_reports(older, newer)
    assert [c.rule for c in diff.resolved] == ["R1"]
    assert [c.rule for c in diff.broken] == ["R2"]
    assert diff.score_delta == 25
    assert diff.ai_before == 20 and diff.ai_after == 80
    assert diff.ai_delta == 60
    changes = {d.filename: d.change for d in diff.docs}
    assert changes == {"a.pdf": "unchanged", "gone.pdf": "removed", "new.pdf": "added"}


def test_build_snapshot_keeps_only_evaluated_verdicts():
    view = _view(
        ("a.pdf", [
            _verdict("fail", RuleSeverity.HIGH, RuleCategory.AI_READINESS),
            _verdict("skipped", RuleSeverity.LOW, RuleCategory.CLARITY),
        ]),
    )
    snap = json.loads(build_snapshot(view))
    outcomes = [v["outcome"] for v in snap["documents"][0]["verdicts"]]
    assert outcomes == ["fail"]  # skipped verdict dropped


def test_build_snapshot_freezes_version_and_evidence_provenance():
    verdict = _verdict(
        "fail",
        RuleSeverity.HIGH,
        RuleCategory.AI_READINESS,
        quotes=[{"quote": "AI use is unrestricted.", "page": 3}],
        code="a1",
    )
    snap = json.loads(build_snapshot(_view(("a.pdf", [verdict]))))
    document = snap["documents"][0]
    assert document["document_id"] == 1
    assert document["document_version_id"] == 101
    assert document["version_number"] == 1
    stored = document["verdicts"][0]
    assert stored["facts"] == [
        {
            "field": "observed_condition",
            "value": False,
            "gate": False,
            "quotes": [{"quote": "AI use is unrestricted.", "page": 3}],
        }
    ]


def _coded_snapshot(outcome_a1, a2_text="check two", a2_cel="x > 0", extra=None):
    rules = [
        {"code": "a1", "text": "check one", "cel": "y", "severity": "high",
         "category": "content", "document_type": "assessment"},
        {"code": "a2", "text": a2_text, "cel": a2_cel, "severity": "low",
         "category": "clarity", "document_type": "assessment"},
    ] + (extra or [])
    verdicts = [
        {"code": "a1", "rule": "check one", "category": "content",
         "severity": "high", "outcome": outcome_a1},
        {"code": "a2", "rule": a2_text, "category": "clarity",
         "severity": "low", "outcome": "fail"},
    ]
    return {
        "documents": [
            {"filename": "a.pdf", "document_type": "assessment",
             "verdicts": verdicts}
        ],
        "rules": rules,
    }


def test_diff_marks_a_new_document_version_as_edited():
    older = _coded_snapshot("fail")
    newer = _coded_snapshot("fail")
    older["documents"][0]["document_version_id"] = 10
    newer["documents"][0]["document_version_id"] = 11
    diff = diff_reports(
        _report(older, {"health_score": 40}),
        _report(newer, {"health_score": 40}),
    )
    assert [(d.filename, d.change) for d in diff.docs] == [("a.pdf", "edited")]


def test_build_snapshot_freezes_codes_and_rule_definitions():
    v1 = _verdict("fail", RuleSeverity.HIGH, RuleCategory.AI_READINESS, code="a1")
    v2 = _verdict("pass", RuleSeverity.LOW, RuleCategory.CLARITY, code="all1")
    snap = json.loads(build_snapshot(_view(("a.pdf", [v1, v2]))))
    assert [v["code"] for v in snap["documents"][0]["verdicts"]] == ["a1", "all1"]
    # The frozen rule set of the run: definition included, so later edits or a
    # deleted rule whose number gets reused are detectable in the diff.
    frozen = {r["code"]: r for r in snap["rules"]}
    assert set(frozen) == {"a1", "all1"}
    assert frozen["a1"]["text"] == "rule for ai_readiness"
    assert frozen["a1"]["cel"] == "true"


def test_diff_pairs_by_code_and_quarantines_changed_definitions():
    """Same code + same frozen definition -> a real transition. Same code but a
    DIFFERENT definition (edit, or delete + recreate reusing the number) is not
    the same check: reported once under rule changes, never as resolved/broken."""
    older = _report(_coded_snapshot("fail"), {"health_score": 40})
    newer = _report(
        _coded_snapshot(
            "pass", a2_text="a brand new requirement", a2_cel="z != 0",
            extra=[{"code": "s1", "text": "new slide rule", "cel": "k",
                    "severity": "low", "category": "content",
                    "document_type": "slide"}],
        ),
        {"health_score": 65},
    )
    diff = diff_reports(older, newer)
    # a1: unchanged definition, fail -> pass = resolved, tagged with its code.
    assert [(c.code, c.change) for c in diff.resolved] == [("a1", "resolved")]
    assert diff.broken == [] and diff.still_failing == []
    # a2 changed definition; s1 exists only in the newer run.
    changes = {r.code: r for r in diff.rules}
    assert changes["a2"].change == "changed"
    assert changes["a2"].text == "a brand new requirement"
    assert changes["a2"].text_before == "check two"
    assert changes["s1"].change == "added"


def test_diff_falls_back_to_text_keys_against_a_precode_snapshot():
    """Comparing with a report from before codes existed still works by text."""
    old_style = {
        "documents": [
            {"filename": "a.pdf", "document_type": "assessment", "verdicts": [
                {"rule": "check one", "category": "content",
                 "severity": "high", "outcome": "fail"},
            ]},
        ]
    }
    diff = diff_reports(
        _report(old_style, {"health_score": 40}),
        _report(_coded_snapshot("pass"), {"health_score": 65}),
    )
    assert [c.rule for c in diff.resolved] == ["check one"]
    assert diff.rules == []  # rule-set movement needs codes on both sides
