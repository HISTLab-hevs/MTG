"""Course rules.

A rule is a requirement attached to one or more document types, in two
formulations: the natural-language description shown to the user, and the formal
CEL condition the rule engine evaluates. Every course gets the default rule set
from app/rules/default_rules.yaml, which ships with both forms, copied into its
own rows so each course can manage its own copy. Custom rules are added from the
Rules page in natural language; a background job
(app.services.rule_generation) compiles the request into CEL, and the result
waits as a proposal until the professor accepts or rejects it.

A CEL expression is evaluated per document against a single root doc: the facts
extracted from that document's pages, collapsed to one value per field
(app/services/aggregation.py). Every field it reads is declared in the rule's
extraction contract (app/services/contracts.py), default rules included, so the
engine has one code path for both origins.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Course,
    CourseRule,
    DocumentType,
    RuleCategory,
    RuleOrigin,
    RuleSeverity,
    RuleStatus,
)

# Kept as YAML beside the package so the rule set can be edited without touching
# code; this module only loads and seeds it.
DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "rules" / "default_rules.yaml"


@dataclass(frozen=True)
class DefaultRule:
    """One entry of app/rules/default_rules.yaml, seeded into every course.

    Attributes:
        code: Stable identity declared in the YAML, identical in every course.
            See code_prefix and next_rule_code below for the scheme.
        document_type: Canonical comma-separated subset of DocumentType.ALL,
            never empty: a rule applying to every document lists all four.
    """

    code: str
    document_type: str
    severity: str
    category: str
    text: str
    rationale: str
    cel: str
    contract: list[dict]


# Per-type code prefixes, in DocumentType.ALL order. A rule's code prefix is
# the concatenation of the prefixes of the types it targets ("syllabus" -> sy,
# "syllabus,assessment" -> sya), or "all" when it targets every type, the
# concatenation of single-type prefixes in this fixed order is unambiguous.
_TYPE_PREFIX = {
    DocumentType.SYLLABUS: "sy",
    DocumentType.SLIDE: "s",
    DocumentType.ASSESSMENT: "a",
    DocumentType.RUBRIC: "r",
}
_CODE_RE = re.compile(r"^([a-z]+)([0-9]+)$")


def code_prefix(types: list[str]) -> str:
    """The code prefix for a rule targeting a set of document types.

    Args:
        types: A subset of DocumentType.ALL.

    Returns:
        The concatenated per-type prefixes, or "all" when every type is
        targeted.
    """
    chosen = [t for t in DocumentType.ALL if t in types]
    if set(chosen) == set(DocumentType.ALL):
        return "all"
    return "".join(_TYPE_PREFIX[t] for t in chosen) or "all"


def next_rule_code(course: Course, types: list[str]) -> str:
    """The next free code for a new rule of the course.

    The prefix comes from the type set and the number continues the highest
    existing one under that prefix, default codes included, so custom rules
    continue the default sequence: a course's first custom syllabus rule becomes
    sy7. A code is assigned once, at creation, and survives edits; even a later
    re-scope keeps it, because the code is the rule's identity rather than its
    classification.

    If the highest-numbered rule of a prefix is deleted, its number can be
    reused. The report history guards against that by comparing the frozen
    definitions and not just the codes (see report_history.diff_reports).

    Args:
        course: Course the rule will belong to.
        types: The types the rule targets.

    Returns:
        The code to assign.
    """
    prefix = code_prefix(types)
    highest = 0
    for rule in course.rules:
        m = _CODE_RE.match(rule.code or "")
        if m and m.group(1) == prefix:
            highest = max(highest, int(m.group(2)))
    return f"{prefix}{highest + 1}"


@lru_cache
def load_default_rules() -> tuple[DefaultRule, ...]:
    """Parse and validate the default rule set, cached after the first read.

    Returns:
        The rules as declared in app/rules/default_rules.yaml.

    Raises:
        RuntimeError: The file is missing, malformed, or a code is absent,
            duplicated, or does not match its declared types.
    """
    raw = yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8")) or []
    rules: list[DefaultRule] = []
    seen_codes: set[str] = set()
    for i, entry in enumerate(raw):
        types = DocumentType.parse(entry.get("document_type"))
        document_type = DocumentType.normalize(types)
        severity = entry.get("severity")
        category = entry.get("category")
        code = str(entry.get("code", "")).strip()
        if not document_type:
            raise ValueError(
                f"default rule #{i}: document_type must list one or more of "
                f"{DocumentType.ALL}"
            )
        # The code is the rule's cross-course identity: it must exist, be
        # unique, and have a prefix matching the declared types, or reports from
        # different courses and rule-file versions stop being comparable.
        m = _CODE_RE.match(code)
        if not m or m.group(1) != code_prefix(types):
            raise ValueError(
                f"default rule #{i}: code {code!r} must be "
                f"{code_prefix(types)!r} + a number"
            )
        if code in seen_codes:
            raise ValueError(f"default rule #{i}: duplicate code {code!r}")
        seen_codes.add(code)
        if severity not in RuleSeverity.ALL:
            raise ValueError(f"default rule #{i}: invalid severity {severity!r}")
        if category not in RuleCategory.ALL:
            raise ValueError(f"default rule #{i}: invalid category {category!r}")
        rules.append(
            DefaultRule(
                code=code,
                document_type=document_type,
                severity=severity,
                category=category,
                text=" ".join(str(entry.get("text", "")).split()),
                rationale=" ".join(str(entry.get("rationale", "")).split()),
                cel=str(entry.get("cel", "")).strip(),
                contract=list(entry.get("contract", [])),
            )
        )
    return tuple(rules)


def ensure_default_rules(db: Session, course: Course) -> None:
    """Seed the default rules once per course.

    On a course that already has them, this refreshes the codes and wording
    instead. The check and the insert are not atomic, so two concurrent requests
    for a fresh course can both decide to seed; the unique index on course and
    code makes the loser fail rather than duplicate the set. That conflict means
    the winner seeded the same rows, so it is not reported: the loser rolls back
    and adopts them.

    Args:
        db: Session to commit on.
        course: Course to seed.
    """
    has_defaults = any(r.origin == RuleOrigin.DEFAULT for r in course.rules)
    if has_defaults:
        _backfill_codes(db, course)
        _refresh_default_wording(db, course)
        return
    for rule in load_default_rules():
        db.add(
            CourseRule(
                course_id=course.id,
                code=rule.code,
                document_type=rule.document_type,
                text=rule.text,
                rationale=rule.rationale or None,
                cel=rule.cel,
                extraction_contract=json.dumps(rule.contract),
                severity=rule.severity,
                category=rule.category,
                origin=RuleOrigin.DEFAULT,
            )
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    db.refresh(course)


def _backfill_codes(db: Session, course: Course) -> None:
    """Assign codes to a course seeded before codes existed.

    Default rules take their fixed code from the YAML, matched by text since the
    seeded copy is verbatim; custom rules get the next free number in creation
    order, exactly as if codes had existed when they were added. A no-op once
    every rule has one, which is the case on every request after the first.

    Args:
        db: Session to commit on.
        course: Course to backfill.
    """
    missing = [r for r in course.rules if not r.code]
    if not missing:
        return
    by_text = {d.text: d.code for d in load_default_rules()}
    for rule in sorted(missing, key=lambda r: r.id):
        if rule.origin == RuleOrigin.DEFAULT and rule.text in by_text:
            rule.code = by_text[rule.text]
        else:
            rule.code = next_rule_code(course, DocumentType.parse(rule.document_type))
    db.commit()


def _refresh_default_wording(db: Session, course: Course) -> None:
    """Re-adopt the YAML wording of default rules that were reworded there.

    A default rule's text is the project's, not the professor's: a course seeded
    before an editorial fix would otherwise keep the old sentence forever. Only
    the text and rationale are touched, and only on default rules matched by
    code, so nothing a professor wrote is ever rewritten. What the rule checks,
    its CEL and contract, is deliberately left alone: changing that would
    silently alter verdicts, which belongs to a rule edit rather than to
    seeding.

    A no-op once the wording matches, which is the case on every request after
    the first.

    Args:
        db: Session to commit on.
        course: Course to refresh.
    """
    by_code = {d.code: d for d in load_default_rules() if d.code}
    changed = False
    for rule in course.rules:
        if rule.origin != RuleOrigin.DEFAULT or not rule.code:
            continue
        default = by_code.get(rule.code)
        if default is None:
            continue
        if rule.text != default.text:
            rule.text = default.text
            changed = True
        if (rule.rationale or None) != (default.rationale or None):
            rule.rationale = default.rationale or None
            changed = True
    if changed:
        db.commit()


class RuleError(Exception):
    """A rule the professor must fix: too long, untyped, or invalid CEL."""
    pass


# A rule request is meant to be one checkable requirement rather than a whole
# policy, but real ones carry their own qualifications, an exemption clause or a
# worked example of what counts, and 200 characters cut those off mid sentence.
# Shared by the add form and the server-side check so the two never disagree,
# and kept equal to RULE_TEXT_MAXLEN below: a rule that can be created must
# remain editable.
RULE_CHAR_LIMIT = 500


def add_custom_rule(
    db: Session, course: Course, text: str, document_types: list[str]
) -> CourseRule:
    """Store the professor's request and hand it to the generation job.

    Args:
        db: Session to commit on.
        course: Course the rule belongs to.
        text: The requirement in natural language.
        document_types: A non-empty subset of DocumentType.ALL. As with uploads,
            "all documents" is expressed by selecting every type, never by an
            empty set.

    Returns:
        The stored rule, awaiting compilation.

    Raises:
        RuleError: The text is empty or too long, or no type was chosen.
    """
    text = " ".join(text.split())
    if not text:
        raise RuleError("Describe the rule before sending it.")
    if len(text) > RULE_CHAR_LIMIT:
        raise RuleError(f"Keep the rule to {RULE_CHAR_LIMIT} characters or fewer.")
    chosen = [t for t in document_types if t in DocumentType.ALL]
    if not chosen:
        raise RuleError("Please select at least one document type.")
    document_type = DocumentType.normalize(chosen)
    # next_rule_code reads course.rules in Python with no locking, so two
    # near-simultaneous requests can compute the same code. The unique index on
    # course and code is the actual guard; on a collision, recompute against the
    # now-committed sibling row and retry once.
    for attempt in range(2):
        rule = CourseRule(
            course_id=course.id,
            code=next_rule_code(course, chosen),
            document_type=document_type,
            text=text,
            source_prompt=text,
            origin=RuleOrigin.CUSTOM,
            status=RuleStatus.GENERATING,
        )
        db.add(rule)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            db.refresh(course)
            if attempt == 1:
                raise
            continue
        db.refresh(course)
        return rule


# Edit limits for a custom rule, shared by the form and the server-side guard so
# the two never disagree. The text limit tracks RULE_CHAR_LIMIT: a request
# accepted at creation must still fit when reopened.
RULE_TEXT_MAXLEN = RULE_CHAR_LIMIT
RULE_CEL_MAXLEN = 300
# The reconciled-contract draft that round-trips through a hidden form field.
# Unlike the text and CEL this has no natural per-character UI limit, so it needs
# its own cap: generous enough for a real contract, since each entry's own
# instruction is already capped in validate_contract, and small enough that a
# malicious or broken client cannot force a large JSON parse and validation pass.
CONTRACT_DRAFT_MAXLEN = 20_000


def contract_with_aggs(contract_json: str | None, aggs: dict[str, str]) -> str | None:
    """Replace the aggregation of the named contract fields.

    Used both to persist a validated edit and to keep the professor's draft
    choices on screen when the save fails validation for another reason.

    Args:
        contract_json: The contract to rewrite.
        aggs: New aggregation per field name.

    Returns:
        The rewritten contract, or the input unchanged when it cannot be parsed.
    """
    if not contract_json or not aggs:
        return contract_json
    try:
        entries = json.loads(contract_json)
    except ValueError:
        return contract_json
    if not isinstance(entries, list):
        return contract_json
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") in aggs:
            entry["agg"] = aggs[entry["name"]]
    return json.dumps(entries)


def update_rule(
    db: Session,
    rule: CourseRule,
    text: str,
    severity: str,
    category: str,
    document_types: list[str],
    cel: str,
    aggs: dict[str, str] | None = None,
    contract_json: str | None = None,
) -> CourseRule:
    """Apply a professor's edit to a custom rule.

    The contract follows the CEL, which keeps hand-edits free: a field the new
    expression no longer reads is dropped automatically, while a field it reads
    but the rule does not declare raises a RuleError the edit dialog turns into
    an offer to have the assistant declare it (see
    rule_generation.complete_contract).

    Args:
        db: Session to commit on.
        rule: Rule to edit.
        text: Its new natural-language text.
        severity: Its new severity.
        category: Its new report area.
        document_types: The types it now applies to.
        cel: Its new CEL expression.
        aggs: New aggregation per contract field name. Changing one needs no
            re-extraction, since aggregation is deterministic and per-rule.
        contract_json: A completed contract to adopt, as round-tripped through
            the dialog. Untrusted, so it gets the full validation a generated
            contract would.

    Returns:
        The edited rule.

    Raises:
        RuleError: Any field of the edit is invalid, or the CEL reads a fact the
            contract does not declare.
    """
    # Imported here to avoid a circular import: rule_generation imports rules
    # indirectly through the contracts and engine path.
    from app.services.contracts import (
        AGG_CHOICES,
        fields_from_entries,
        find_field_conflict,
    )
    from app.services.rule_generation import (
        _existing_fields,
        cel_field_names,
        check_closure,
        check_macro_targets,
        validate_cel,
        validate_contract,
    )

    if rule.origin != RuleOrigin.CUSTOM:
        raise RuleError("Only custom rules can be edited.")
    text = " ".join(text.split())
    cel = cel.strip()
    if not text:
        raise RuleError("The rule description cannot be empty.")
    if len(text) > RULE_TEXT_MAXLEN:
        raise RuleError(f"The description must be {RULE_TEXT_MAXLEN} characters or fewer.")
    if severity not in RuleSeverity.ALL:
        raise RuleError("Choose a valid severity.")
    if category not in RuleCategory.ALL:
        raise RuleError("Choose a valid area.")
    chosen = [t for t in document_types if t in DocumentType.ALL]
    if not chosen:
        raise RuleError("Please select at least one document type.")
    if not cel:
        raise RuleError("The CEL expression cannot be empty.")
    if len(cel) > RULE_CEL_MAXLEN:
        raise RuleError(f"The CEL expression must be {RULE_CEL_MAXLEN} characters or fewer.")
    # The reconciled draft when one was round-tripped through the form, the
    # stored contract otherwise.
    if contract_json is not None:
        if len(contract_json) > CONTRACT_DRAFT_MAXLEN:
            raise RuleError(
                f"The contract draft must be {CONTRACT_DRAFT_MAXLEN} characters or fewer."
            )
        try:
            entries = json.loads(contract_json)
        except ValueError:
            raise RuleError("The contract draft is not valid JSON.")
        error = validate_contract(entries)
        if error:
            raise RuleError(error)
        conflict = find_field_conflict(fields_from_entries(entries), _existing_fields(rule))
        if conflict:
            raise RuleError(conflict)
    else:
        entries = json.loads(rule.extraction_contract) if rule.extraction_contract else []

    error = validate_cel(cel)
    if error:
        raise RuleError(error)
    used = cel_field_names(cel)
    # Dropping a field the expression stopped reading is safe and
    # deterministic; a field it reads but nobody declares surfaces below as the
    # repairable error the dialog offers to complete with the model.
    entries = [e for e in entries if e["name"] in used]
    fields_by_name = {f.name: f for f in fields_from_entries(entries)}
    for name, agg in (aggs or {}).items():
        field = fields_by_name.get(name)
        if field is None:
            continue  # its field was dropped along with the CEL that read it
        if agg not in AGG_CHOICES.get(field.type, ()):
            raise RuleError(
                f'"{agg}" is not a valid way to combine "{name}" across pages.'
            )
    error = check_closure(cel, set(fields_by_name))
    if error:
        raise RuleError(error)
    # Validated against the contract as it will be saved, with the form's aggs
    # applied, since switching a field off the pages aggregation is exactly what
    # turns a working macro into one that cannot evaluate.
    final_contract = contract_with_aggs(json.dumps(entries), aggs or {})
    error = check_macro_targets(cel, json.loads(final_contract))
    if error:
        raise RuleError(error)
    rule.text = text
    rule.severity = severity
    rule.category = category
    rule.document_type = DocumentType.normalize(chosen)
    rule.cel = cel
    rule.extraction_contract = final_contract
    rule.edit_draft = None  # any in-flight reconcile is superseded by this save
    db.commit()
    db.refresh(rule)
    return rule


def rules_by_type(course: Course) -> list[tuple[str | None, list[CourseRule]]]:
    """Group accepted rules for display, a section per document type.

    A rule scoped to some but not all types is listed under each of those types,
    so the professor sees it wherever it applies.

    Args:
        course: Course whose rules to group.

    Returns:
        One (document type, rules) pair per non-empty section. A rule targeting
        every type appears once in a trailing section keyed None rather than
        being repeated in all four.
    """
    ready = [r for r in course.rules if r.status == RuleStatus.READY]
    all_types = set(DocumentType.ALL)

    sections: list[tuple[str | None, list[CourseRule]]] = []
    for document_type in DocumentType.ALL:
        rules = [
            r
            for r in ready
            if set(DocumentType.parse(r.document_type)) != all_types
            and document_type in DocumentType.parse(r.document_type)
        ]
        if rules:
            sections.append((document_type, rules))

    # A legacy untyped rule, from before the all-types convention, is surfaced
    # in the same trailing section so no rule is ever invisible.
    all_docs = [
        r
        for r in ready
        if set(DocumentType.parse(r.document_type)) in (all_types, set())
    ]
    if all_docs:
        sections.append((None, all_docs))
    return sections


def rules_in_review(course: Course) -> list[CourseRule]:
    """The rules being generated or awaiting the professor's decision."""
    return [r for r in course.rules if r.status != RuleStatus.READY]

