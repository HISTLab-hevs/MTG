"""Gate fields and the "not applicable" verdict.

A conditional rule (`!doc.condition || doc.requirement`) marks its condition
field with `gate: true` in the extraction contract. When every gate field of a
rule aggregates to a falsy value, the situation the rule checks for never
occurs in the document: the CEL passes vacuously and the verdict is reported
as "na" instead of "pass". These tests pin the exact boundaries of that
conversion — a fail is never touched, a rule without gates is never touched,
and a gate-only rule (whose gate IS its verdict) is never touched — plus the
plumbing: contract parsing, metrics, the course-level synthesis, the chat
context, and the generation schema/validators.
"""

import json
from types import SimpleNamespace

from app.services import contracts
from app.services.report_view import (
    DocumentReport,
    ReportView,
    RuleFact,
    RuleVerdict,
)


def _verdict(value, facts, rule=None, status="evaluated"):
    return RuleVerdict(
        rule=rule, status=status, value=value, error_message=None, facts=facts
    )


# --- contract parsing --------------------------------------------------------


def test_gate_comes_only_from_the_contract_flag():
    """`gate` is read from the entry; no field name is ever special-cased, so a
    user-defined rule whose verdict field happens to be named like a guard
    (`has_assessment_task`, `mentions_ai_…`) is never misread as conditional."""
    entries = [
        {"name": "has_assessment_task", "type": "boolean",
         "instruction": "x", "gate": True},
        {"name": "mentions_ai_use", "type": "boolean",
         "instruction": "x"},  # same name as a default gate, but no flag
        {"name": "quality_count", "type": "number",
         "instruction": "x"},
    ]
    fields = contracts.fields_from_entries(entries)
    assert [f.gate for f in fields] == [True, False, False]


# --- the outcome conversion and its boundaries -------------------------------


def test_vacuous_pass_with_falsy_gates_is_not_applicable():
    """Pass + all gates falsy + real substance fields -> "na"."""
    v = _verdict(True, [
        RuleFact("has_assessment_task", False, [], gate=True),
        RuleFact("ai_policy_level", 0, []),
    ])
    assert v.outcome == "na"
    assert "Not applicable" in v.evidence_caption


def test_pass_with_a_truthy_gate_stays_a_pass():
    v = _verdict(True, [
        RuleFact("has_assessment_task", True, [], gate=True),
        RuleFact("ai_policy_level", 2, []),
    ])
    assert v.outcome == "pass"


def test_fail_is_never_converted_even_with_falsy_gates():
    """A failing rule reports its failure no matter what its gates say."""
    v = _verdict(False, [
        RuleFact("has_assessment_task", False, [], gate=True),
        RuleFact("requires_justified_decision", False, []),
    ])
    assert v.outcome == "fail"


def test_rule_without_gates_is_never_converted():
    """An unconditional rule (e.g. `!doc.has_language_error`) passes as a real
    pass even when its only field is falsy — falsy IS the good outcome."""
    v = _verdict(True, [RuleFact("has_language_error", False, [])])
    assert v.outcome == "pass"


def test_gate_only_contract_is_never_converted():
    """When the gate IS the verdict (e.g. "slides must not use AI":
    `!doc.uses_ai` with the field marked gate), a falsy gate is a real pass,
    not an absence of subject."""
    v = _verdict(True, [RuleFact("uses_ai_in_teaching", False, [], gate=True)])
    assert v.outcome == "pass"


def test_multiple_gates_all_must_be_falsy():
    """One truthy gate means the subject occurred: no conversion."""
    v = _verdict(True, [
        RuleFact("has_assessment_task", True, [], gate=True),
        RuleFact("is_take_home_essay", False, [], gate=True),
        RuleFact("requires_process_evidence", False, []),
    ])
    assert v.outcome == "pass"
    v2 = _verdict(True, [
        RuleFact("has_assessment_task", False, [], gate=True),
        RuleFact("is_take_home_essay", False, [], gate=True),
        RuleFact("requires_process_evidence", False, []),
    ])
    assert v2.outcome == "na"


def test_empty_page_list_gate_counts_as_falsy():
    """Gates aggregated positionally (agg=pages -> []) are falsy when empty."""
    v = _verdict(True, [
        RuleFact("formula_pages", [], [], gate=True),
        RuleFact("example_pages", [], []),
    ])
    assert v.outcome == "na"


# --- counts, metrics, and the course-level synthesis --------------------------


def _rule(id=1, text="rule", category="ai_readiness", severity="high"):
    return SimpleNamespace(
        id=id, code=f"a{id}", text=text, category=category, severity=severity,
        document_type="assessment", rationale="", cel="true",
    )


def _doc_report(filename, verdicts):
    d = DocumentReport(document_id=1, filename=filename, document_type="assessment")
    d.verdicts = verdicts
    return d


def test_document_counts_exclude_not_applicable():
    rule = _rule()
    na = _verdict(True, [
        RuleFact("has_assessment_task", False, [], gate=True),
        RuleFact("x", 0, []),
    ], rule=rule)
    ok = _verdict(True, [RuleFact("y", True, [])], rule=rule)
    bad = _verdict(False, [RuleFact("z", False, [])], rule=rule)
    doc = _doc_report("a.pdf", [na, ok, bad])
    assert (doc.passed, doc.failed, doc.checked, doc.not_applicable) == (1, 1, 2, 1)


def test_metrics_exclude_not_applicable_from_health_and_bars():
    from app.services.report_metrics import build_metrics

    rule = _rule()
    view = ReportView(documents=[
        _doc_report("a.pdf", [
            _verdict(True, [
                RuleFact("has_assessment_task", False, [], gate=True),
                RuleFact("x", 0, []),
            ], rule=rule),
            _verdict(True, [RuleFact("y", True, [])], rule=rule),
            _verdict(False, [RuleFact("z", False, [])], rule=rule),
        ]),
    ])
    m = build_metrics(view)
    assert m.checked == 2  # the n/a verdict never reaches the denominator
    assert m.passed == 1
    assert m.ai_readiness_checked == 2


def test_never_applicable_rules_lists_only_rules_na_everywhere():
    r1, r2 = _rule(id=1, text="if assessment then policy"), _rule(id=2, text="no typos")
    na1 = _verdict(True, [
        RuleFact("has_assessment_task", False, [], gate=True),
        RuleFact("x", 0, []),
    ], rule=r1)
    pass2 = _verdict(True, [RuleFact("has_language_error", False, [])], rule=r2)
    # r1 n/a on BOTH documents -> listed; r2 has real verdicts -> not listed.
    view = ReportView(documents=[
        _doc_report("a.pdf", [na1, pass2]),
        _doc_report("b.pdf", [na1]),
    ])
    out = view.never_applicable_rules()
    assert out == [
        {
            "code": "a1",
            "rule": "if assessment then policy",
            "documents": 2,
            "missing": ["Has assessment task"],
        }
    ]

    # A rule that is n/a on one document but judged on another is NOT listed.
    judged = _verdict(True, [
        RuleFact("has_assessment_task", True, [], gate=True),
        RuleFact("x", 2, []),
    ], rule=r1)
    view2 = ReportView(documents=[
        _doc_report("a.pdf", [na1]),
        _doc_report("b.pdf", [judged]),
    ])
    assert view2.never_applicable_rules() == []


# --- report chat context ------------------------------------------------------


def test_chat_context_is_complete_and_bounded(monkeypatch):
    """The chat context carries EVERY evaluated check (pass, fail, n/a) so the
    assistant can answer "what passed?"; quotes/rationale ride only on fails."""
    from app.services import report_chat

    rule = _rule(text="policy rule")
    ok = _verdict(True, [RuleFact("y", True, [])], rule=_rule(id=2, text="ok rule"))
    na = _verdict(True, [
        RuleFact("has_assessment_task", False, [], gate=True),
        RuleFact("x", 0, []),
    ], rule=_rule(id=3, text="conditional rule"))
    bad = _verdict(False, [
        RuleFact("z", False, [{"quote": "the bad span", "page": 3}]),
    ], rule=rule)
    view = ReportView(documents=[_doc_report("a.pdf", [ok, na, bad])])

    monkeypatch.setattr(report_chat, "build_report_view", lambda db, course: view)
    db = SimpleNamespace(get=lambda cls, id: SimpleNamespace(id=6))
    report = SimpleNamespace(report_run=SimpleNamespace(course_id=6))

    out = report_chat._findings_context(db, report)
    assert len(out) == 1
    # Each check is presented as "[code] text" so the professor can ask by id.
    checks = {c["rule"]: c for c in out[0]["checks"]}
    assert checks["[a2] ok rule"]["outcome"] == "pass"
    assert checks["[a3] conditional rule"]["outcome"] == "not applicable"
    assert checks["[a1] policy rule"]["outcome"] == "fail"
    # Quotes only on the failure; passes stay light.
    assert '"the bad span" (p.3)' in checks["[a1] policy rule"]["quotes"]
    assert "quotes" not in checks["[a2] ok rule"]


# --- rule generation: schema, validation, defaults ----------------------------


def test_generation_schema_and_validator_accept_gate():
    from app.services.rule_generation import RULE_JSON_SCHEMA, validate_contract

    props = RULE_JSON_SCHEMA["properties"]["contract"]["items"]["properties"]
    assert props["gate"] == {"type": "boolean"}

    entry = {"name": "mentions_ai_use", "type": "boolean",
             "agg": "any", "instruction": "x", "gate": True}
    assert validate_contract([entry]) is None
    # A tampered non-boolean gate is rejected, not silently coerced.
    assert validate_contract([{**entry, "gate": "yes"}]) is not None


def test_contract_edit_preserves_gate():
    """Re-tuning a field's agg from the edit modal must not drop the flag."""
    from app.services.rules import contract_with_aggs

    contract = json.dumps([
        {"name": "has_assessment_task", "type": "boolean",
         "agg": "any", "instruction": "x", "gate": True},
    ])
    out = json.loads(contract_with_aggs(contract, {"has_assessment_task": "all"}))
    assert out[0]["agg"] == "all"
    assert out[0]["gate"] is True


def test_every_conditional_default_rule_declares_a_gate():
    """Consistency of the shipped defaults: every `!doc.X || …` conditional CEL
    marks at least one gate, and no unconditional rule marks any."""
    from app.services.rules import load_default_rules

    for rule in load_default_rules():
        fields = contracts.fields_from_entries(rule.contract)
        gates = [f for f in fields if f.gate]
        conditional = rule.cel.lstrip().startswith("!doc.") and "||" in rule.cel
        if conditional:
            assert gates, f"conditional default rule lacks a gate: {rule.text!r}"
            # The gate must never be the only field: the rule still needs a
            # substance field to judge when the gate is satisfied.
            assert any(not f.gate for f in fields), rule.text
        else:
            assert not gates, f"unconditional default rule marks a gate: {rule.text!r}"


# --- markdown normalisation ---------------------------------------------------


def test_normalize_field_strips_item_level_bullets():
    from app.services.report_generation import _normalize_field

    md = _normalize_field(["- **Grammar:** fix p.3", "* second", "third"])
    assert md == "- **Grammar:** fix p.3\n- second\n- third"


# --- per-rule document prose completeness --------------------------------------


def test_doc_schema_pins_one_entry_per_finding():
    from app.services.report_generation import _doc_schema

    s = _doc_schema(3)
    for key in ("issues", "solutions"):
        assert s["properties"][key]["minItems"] == 3
        assert s["properties"][key]["maxItems"] == 3


def test_per_rule_bullets_never_drop_a_finding():
    """Entry i belongs to failure i; a blank/short entry is replaced by the
    deterministic fallback for that rule, so no failed rule can vanish."""
    from app.services.report_generation import _per_rule_bullets

    r1 = _rule(id=1, text="Every formula needs an example")
    r2 = _rule(id=2, text="No spelling mistakes")
    f1 = _verdict(False, [RuleFact("x", False, [])], rule=r1)
    f2 = _verdict(False, [RuleFact("y", True, [{"quote": "teh", "page": 3}])], rule=r2)

    md = _per_rule_bullets(["**Formulas:** pages 3-5 lack examples.", ""], [f1, f2], is_issue=True)
    lines = md.split("\n")
    assert len(lines) == 2
    # Each bullet opens with its rule's code, prepended deterministically.
    assert lines[0] == "- **[a1]** **Formulas:** pages 3-5 lack examples."
    # The dropped second entry is reconstructed from the rule + its quote.
    assert lines[1].startswith("- **[a2]**")
    assert "No spelling mistakes" in lines[1] and "teh" in lines[1]

    # A model that returns fewer entries than findings still yields N bullets.
    md_short = _per_rule_bullets(["only one"], [f1, f2], is_issue=False)
    assert md_short.count("\n- ") + 1 == 2

    # Legacy string payloads (old reports, fallback models) pass through.
    assert _per_rule_bullets("plain text", [f1], is_issue=True) == "plain text"


def _entry(name, gate=False):
    e = {"name": name, "type": "boolean", "agg": "pages", "instruction": "x"}
    if gate:
        e["gate"] = True
    return e


def _gates(contract):
    return [e["name"] for e in contract if e.get("gate")]


def test_whole_macro_gate_is_filled_in_when_the_model_omits_it():
    """A CEL that is entirely one macro over one field passes vacuously on a
    document with no such item, so that field is the rule's precondition. The
    generation prompt asks for the flag and the model supplies it about half
    the time; the rest is filled in deterministically rather than rejected,
    because rejecting it made the model burn all three attempts and produce no
    rule at all."""
    from app.services.rule_generation import apply_whole_macro_gate

    contract = [_entry("homework_task_pages"), _entry("deadline_pages")]
    apply_whole_macro_gate(
        "doc.homework_task_pages.all(p, p in doc.deadline_pages)", contract
    )
    assert _gates(contract) == ["homework_task_pages"]

    # Nested exists over a second list is still one whole-CEL macro.
    contract = [_entry("quiz_pages"), _entry("answer_pages")]
    apply_whole_macro_gate(
        "doc.quiz_pages.all(p, doc.answer_pages.exists(a, a == p || a == p + 1))",
        contract,
    )
    assert _gates(contract) == ["quiz_pages"]


def test_whole_macro_gate_leaves_everything_else_alone():
    """The narrow cases that must NOT gain a gate."""
    from app.services.rule_generation import apply_whole_macro_gate

    # A macro combined with an independent requirement still judges the
    # document when the list is empty, so its field is not a precondition.
    for cel in ("doc.a && doc.x.all(p, p in doc.y)", "doc.x.all(p, p in doc.y) && doc.a"):
        contract = [_entry("a"), _entry("x"), _entry("y")]
        apply_whole_macro_gate(cel, contract)
        assert _gates(contract) == [], cel

    # A single-field contract: the field IS the verdict. Gating it would make
    # every document "not applicable".
    contract = [_entry("x")]
    apply_whole_macro_gate("doc.x.all(p, p > 0)", contract)
    assert _gates(contract) == []

    # Scalar CEL, no macro at all.
    contract = [_entry("g"), _entry("v")]
    apply_whole_macro_gate("!doc.g || doc.v", contract)
    assert _gates(contract) == []

    # An explicit gate from the model is preserved, not duplicated or moved.
    contract = [_entry("group_pages", gate=True), _entry("y")]
    apply_whole_macro_gate("doc.group_pages.all(p, p in doc.y)", contract)
    assert _gates(contract) == ["group_pages"]


def test_default_rules_keep_their_gates_unchanged():
    """The shipped defaults are already correctly gated: the auto-fix must be a
    no-op over every one of them."""
    import copy
    from pathlib import Path

    import yaml

    from app.services.rule_generation import apply_whole_macro_gate

    data = yaml.safe_load(Path("app/rules/default_rules.yaml").read_text(encoding="utf-8"))
    rules = data if isinstance(data, list) else data.get("rules", data)
    for rule in rules:
        contract = copy.deepcopy(rule.get("contract") or [])
        before = [(e["name"], e.get("gate", False)) for e in contract]
        apply_whole_macro_gate(rule.get("cel", ""), contract)
        after = [(e["name"], e.get("gate", False)) for e in contract]
        assert before == after, rule.get("code")
