"""Rule-to-document-type matching, including multi-type documents."""

from app.models import CourseRule, DocumentType, RuleStatus
from app.services.contracts import active_rules_for_types


def _rule(document_type, name="r"):
    return CourseRule(
        document_type=document_type,
        text=name,
        cel="true",
        status=RuleStatus.READY,
        is_active=True,
    )


# A rule that must hold for every document simply lists every type; derived
# from the model so adding a type cannot leave this silently partial.
ALL_TYPES = ",".join(DocumentType.ALL)


def test_multi_type_document_gets_rules_of_every_type():
    rules = [
        _rule("assessment", "a"),
        _rule("rubric", "r"),
        _rule("slide", "s"),
        _rule(ALL_TYPES, "all"),
    ]
    matched = {r.text for r in active_rules_for_types(rules, ["assessment", "rubric"])}
    # Both selected types plus the all-types rule; never the slide-only one.
    assert matched == {"a", "r", "all"}


def test_rule_scoped_to_several_types_matches_on_overlap():
    rules = [_rule("assessment,rubric", "ar"), _rule("syllabus", "syl")]
    assert {r.text for r in active_rules_for_types(rules, ["rubric"])} == {"ar"}
    assert {r.text for r in active_rules_for_types(rules, ["syllabus"])} == {"syl"}
    assert active_rules_for_types(rules, ["slide"]) == []


def test_single_type_document_excludes_other_types():
    rules = [_rule("assessment", "a"), _rule("rubric", "r"), _rule(ALL_TYPES, "all")]
    matched = {r.text for r in active_rules_for_types(rules, ["assessment"])}
    assert matched == {"a", "all"}


def test_shared_rule_is_not_duplicated():
    # A rule overlapping on two of the document's types applies only once.
    rules = [_rule("assessment,rubric", "ar")]
    matched = active_rules_for_types(rules, ["assessment", "rubric"])
    assert len(matched) == 1


def test_inactive_and_unready_rules_are_skipped():
    inactive = _rule("assessment", "a")
    inactive.is_active = False
    unready = _rule("assessment", "b")
    unready.status = RuleStatus.PROPOSED
    matched = active_rules_for_types([inactive, unready], ["assessment"])
    assert matched == []
