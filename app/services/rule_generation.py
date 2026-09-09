"""Compile a natural-language rule request into CEL.

The background job for custom rules. The professor's request, stored on the rule
as its source_prompt, is sent to the generation model, which answers with a JSON
object holding the CEL expression, a one-sentence description, and the rule's
extraction contract: the typed fields the expression needs, each with an
instruction for the extraction model (app/services/contracts.py).

The answer is validated in layers: CEL syntax, contract well-formedness, closure
between expression and contract in both directions, and a ban on string
functions over extracted fields. Any rejection is fed back through the
refinement prompt at a higher temperature, up to MAX_ATTEMPTS, after which the
rule is marked failed and the professor is asked to rephrase. A valid result is
stored as a proposal and waits to be accepted or rejected on the Rules page.
"""

import json
import logging
import re

import celpy

from app.db import SessionLocal
from app.llm.client import NO_THINKING, get_rules_client
from app.llm.prompts import loads_model_json, render_prompt
from app.models import CourseRule, DocumentType, RuleCategory, RuleSeverity, RuleStatus
from app.services import contracts

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# One temperature per attempt: deterministic first, more exploratory retries.
TEMPERATURES = (0.0, 0.2, 0.4)

# The only built-in name of the activation: the aggregated document object.
# Every field the CEL reads (`doc.<field>`) must be declared in the contract.
ACTIVATION_ROOTS = frozenset({"doc"})
# String matching over extractor output is not deterministic; the judgement
# belongs in a field instruction instead.
BANNED_FUNCTIONS = frozenset({"contains", "startsWith", "endsWith", "matches"})
CEL_KEYWORDS = frozenset(
    {
        "true",
        "false",
        "null",
        "in",
        "has",
        "size",
        "all",
        "exists",
        "exists_one",
        "filter",
        "map",
        "int",
        "uint",
        "double",
        "string",
        "bool",
        "bytes",
        "list",
        "dyn",
    }
)

# Constrained decoding: the vLLM endpoint is forced to emit exactly this
# shape (guided_json), so the repair budget is spent on CEL and closure
# errors, never on malformed JSON. The closed enums mirror the validators
# below, which stay in place for endpoints without guided decoding.
RULE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "minLength": 1},
        "rationale": {"type": "string", "minLength": 1},
        "cel": {"type": "string", "minLength": 1},
        "severity": {"enum": list(RuleSeverity.ALL)},
        "category": {"enum": list(RuleCategory.ALL)},
        "contract": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "type": {"enum": list(contracts.CONTRACT_TYPES)},
                    "agg": {"enum": list(contracts.AGG_KINDS)},
                    "instruction": {"type": "string", "minLength": 1},
                    "gate": {"type": "boolean"},
                },
                "required": ["name", "type", "agg", "instruction"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["description", "rationale", "cel", "severity", "category", "contract"],
    "additionalProperties": False,
}

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_STRING_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")
_MACRO_VARIABLE = re.compile(
    r"\.\s*(?:all|exists|exists_one|filter|map)\s*\(\s*([A-Za-z_]\w*)\s*,"
)
# Member access on a tagged_pages entry (`f.item`, `e.page`): the members are
# part of the aggregation's shape, not contract fields, so they are stripped
# before identifier analysis. `\b` keeps `.pages_field`/`.items_list` intact.
_TAGGED_MEMBER = re.compile(r"\.\s*(?:item|page)\b")
_SNAKE_CASE = re.compile(r"[a-z][a-z0-9_]*\Z")


def validate_cel(expression: str) -> str | None:
    """Check that an expression is a usable rule.

    Beyond parsing, this rejects an expression that reads nothing from the
    document, such as a bare true or 1 == 1. Those parse cleanly but decide the
    same verdict for every document ever analysed, so the rule stops being a
    check and quietly passes or fails the whole course. It is what a
    half-finished edit or a model that gave up leaves behind, and the professor
    has no way to tell from the report why one rule fails everywhere. Caught
    here so the edit form can say so while the rule is still being written.

    Args:
        expression: The CEL to check.

    Returns:
        None when the expression is usable, otherwise the reason it is not.
    """
    try:
        celpy.Environment().compile(expression)
    except celpy.CELParseError as exc:
        return str(exc)
    if not _reads_document(expression):
        return (
            "This check never looks at the document, so it would give the same "
            "verdict to every file. Write a condition over the facts the rule "
            "extracts, for example doc.has_ai_policy or doc.question_count > 0."
        )
    return None


def _reads_document(expression: str) -> bool:
    """Whether the expression consults the document at all.

    True when it reads a declared contract field or an engine-supplied one such
    as doc.page_count. A literal, or an expression over literals alone, reads
    nothing and cannot discriminate between documents.
    """
    if cel_field_names(expression):
        return True
    source = _STRING_LITERAL.sub("", expression)
    return any(f"doc.{name}" in source for name in contracts.BUILTIN_FIELDS)


def validate_contract(contract: object) -> str | None:
    """Check that the extraction-contract entries are well formed.

    Args:
        contract: The decoded contract, expected to be a list of entries.

    Returns:
        None when every entry is usable, otherwise the reason one is not.
    """
    if not isinstance(contract, list):
        return '"contract" must be a JSON array of field entries.'
    seen: set[str] = set()
    for entry in contract:
        if not isinstance(entry, dict):
            return "Every contract entry must be a JSON object."
        name = entry.get("name", "")
        if not isinstance(name, str) or not _SNAKE_CASE.match(name):
            return f"Contract field name {name!r} is not a snake_case identifier."
        if name in seen:
            return f'Contract declares the field "{name}" more than once.'
        if name in contracts.BUILTIN_FIELDS:
            return (
                f'"{name}" is supplied by the engine and must not be declared as a '
                "contract field; read it directly as doc." + name + "."
            )
        seen.add(name)
        if entry.get("type") not in contracts.CONTRACT_TYPES:
            return (
                f'Contract field "{name}" has invalid type; '
                f"use one of {', '.join(contracts.CONTRACT_TYPES)}."
            )
        # agg is optional in stored contracts (a type default applies); an
        # explicitly-given value must be compatible with the field's TYPE, not
        # just a known kind, e.g. "union" over a boolean would crash the
        # aggregator at report time (this also guards a tampered edit draft).
        if entry.get("agg") is not None and entry["agg"] not in contracts.AGG_CHOICES.get(
            entry["type"], ()
        ):
            allowed = ", ".join(contracts.AGG_CHOICES.get(entry["type"], ()))
            return (
                f'Contract field "{name}" has an agg incompatible with its type; '
                f"for {entry['type']} use one of: {allowed}."
            )
        # `gate` is optional; when present it must be a real boolean, since it
        # decides whether a passing verdict can be reported "not applicable".
        if "gate" in entry and not isinstance(entry["gate"], bool):
            return f'Contract field "{name}" has a non-boolean "gate" flag.'
        instruction = str(entry.get("instruction", "")).strip()
        if not instruction:
            return f'Contract field "{name}" is missing its extraction instruction.'
        if len(instruction) > 700:
            return (
                f'Contract field "{name}" has an instruction over 700 characters; '
                "keep it to one self-contained sentence."
            )
    return None


def cel_field_names(cel: str) -> set[str]:
    """The contract fields a CEL expression reads.

    Identifier-level analysis: string literals are stripped, and tagged-entry
    members, macro-bound iteration variables and built-ins are excluded.

    Args:
        cel: The expression to scan.

    Returns:
        The names it reads as doc members.
    """
    source = _STRING_LITERAL.sub("", cel)
    source = _TAGGED_MEMBER.sub("", source)
    identifiers = set(_IDENTIFIER.findall(source))
    bound = set(_MACRO_VARIABLE.findall(source))
    # Engine-supplied members are readable without a declaration, so they count
    # as neither used nor missing for closure.
    return (
        identifiers
        - CEL_KEYWORDS
        - ACTIVATION_ROOTS
        - bound
        - set(contracts.BUILTIN_FIELDS)
    )


def check_closure(cel: str, contract_names: set[str]) -> str | None:
    """Check that the expression and the contract cover the same fields.

    Args:
        cel: The expression to check.
        contract_names: The fields the contract declares.

    Returns:
        None when the two match exactly, otherwise which side has the extra
        field, or which banned string function was used.
    """
    source = _STRING_LITERAL.sub("", cel)
    identifiers = set(_IDENTIFIER.findall(source))
    banned = sorted(identifiers & BANNED_FUNCTIONS)
    if banned:
        return (
            f"String functions on extracted fields are not allowed ({', '.join(banned)}). "
            "Move the judgement into a boolean field instruction instead."
        )
    used = cel_field_names(cel)
    undeclared = sorted(used - contract_names)
    if undeclared:
        return (
            "The expression references fields the contract does not declare: "
            + ", ".join(undeclared)
        )
    unused = sorted(contract_names - used)
    if unused:
        return (
            "The contract declares fields the expression never uses: "
            + ", ".join(unused)
        )
    return None


# The field a macro iterates, as in doc.formula_pages.all(p, ...). Only a
# positional aggregation yields a list; every other one collapses to a scalar.
_MACRO_TARGET = re.compile(
    r"\bdoc\s*\.\s*([A-Za-z_]\w*)\s*\.\s*(?:all|exists|exists_one|filter|map)\s*\("
)
LIST_AGGS = frozenset({"pages", "tagged_pages"})


def check_macro_targets(cel: str, contract: object) -> str | None:
    """Reject a macro iterating a field that aggregates to a scalar.

    A macro over doc.x only evaluates when x is a list, which happens solely
    under the pages and tagged_pages aggregations. Over a boolean the expression
    is valid CEL and passes closure, but celpy raises at analysis time and the
    rule silently produces no verdict. Caught here so the generator repairs it.

    Args:
        cel: The expression to check.
        contract: The decoded contract, read for each field's aggregation.

    Returns:
        None when every macro target is a list, otherwise the reason it is not.
    """
    if not isinstance(contract, list):
        return None
    aggs = {
        e["name"]: (e.get("agg") or contracts.AGG_BY_TYPE.get(e.get("type"), ""))
        for e in contract
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    }
    source = _STRING_LITERAL.sub("", cel)
    for name in _MACRO_TARGET.findall(source):
        agg = aggs.get(name)
        if agg is not None and agg not in LIST_AGGS:
            return (
                f'The expression iterates "{name}" with an all/exists macro, but its '
                f'"{agg}" aggregation collapses it to a single value. Either read it '
                f'directly (doc.{name}) or, for a positional rule, declare it as a '
                'boolean with agg "pages".'
            )
    return None


# A CEL that is ENTIRELY one macro over one field: `doc.x.all(p, …)` and
# nothing else at the top level. The trailing `$` after the balanced-paren tail
# is what distinguishes it from `doc.a && doc.x.all(p, …)`, where the rule also
# carries an independent requirement and the iterated field is NOT a gate.
_WHOLE_MACRO = re.compile(
    r"^\s*doc\s*\.\s*([A-Za-z_]\w*)\s*\.\s*(?:all|exists|exists_one)\s*\(.*\)\s*$",
    re.DOTALL,
)


def apply_whole_macro_gate(cel: str, contract: object) -> None:
    """Set the missing gate when the whole rule iterates one field.

    A rule that is nothing but a macro over doc.formula_pages is vacuously true
    on a document with no formulas: correct CEL, but the rule checked nothing,
    and without a gate the engine counts that as a genuine pass rather than as
    not applicable.

    The generation prompt already asks for this and the model complies about
    half the time. Rejecting the other half was tried and made things worse: the
    model could not reliably repair it, so rules that used to generate fine
    failed outright after three attempts, and a professor got no rule at all
    instead of a slightly over-counted pass. Since the gate is derivable from
    the CEL's own shape, it is set here deterministically rather than asked for
    again.

    Deliberately narrow, matching the prompt's own mechanical rule: only when
    the whole expression is one macro over one field. A macro combined with an
    independent requirement is left alone, since the rule still judges something
    when the list is empty, and so is a contract of only that one field, where
    the field is the verdict and gating it would make every document not
    applicable.

    Args:
        cel: The expression to inspect.
        contract: The decoded contract, whose entry is flagged in place.
    """
    if not isinstance(contract, list):
        return
    match = _WHOLE_MACRO.match(_STRING_LITERAL.sub("", cel))
    if not match:
        return
    entries = [e for e in contract if isinstance(e, dict)]
    if len(entries) < 2:
        return
    for entry in entries:
        if entry.get("name") == match.group(1):
            entry["gate"] = True
            return


class _Rejected(Exception):
    """The model's answer was unusable.

    Attributes:
        answer: The rejected answer, fed back through the refinement prompt.
        error: Why it was rejected.
    """

    def __init__(self, answer: str, error: str):
        super().__init__(error)
        self.answer = answer
        self.error = error


def _parse_answer(
    answer: str, existing_fields: list[contracts.Field]
) -> tuple[str, str, str, str, str, list[dict]]:
    """Validate the model's answer and pull the rule out of it.

    Args:
        answer: The model's raw answer.
        existing_fields: Fields already declared by rules sharing an extraction
            pass, so a clashing new field is caught here.

    Returns:
        The description, rationale, CEL, severity, category and contract.

    Raises:
        _Rejected: The answer was unusable, with the reason for the refinement
            prompt.
    """
    try:
        data = loads_model_json(answer)
    except ValueError:
        raise _Rejected(answer, "The reply was not a single valid JSON object.")
    if not isinstance(data, dict):
        raise _Rejected(answer, "The reply was not a single valid JSON object.")
    description = str(data.get("description", "")).strip()
    rationale = " ".join(str(data.get("rationale", "")).split())
    cel = str(data.get("cel", "")).strip()
    severity = str(data.get("severity", "")).strip()
    category = str(data.get("category", "")).strip()
    contract = data.get("contract")
    if not description or not cel or contract is None:
        raise _Rejected(
            answer,
            'The JSON object must contain non-empty "description" and "cel" '
            'and a "contract" array.',
        )
    if severity not in RuleSeverity.ALL:
        raise _Rejected(answer, f'"severity" must be one of {", ".join(RuleSeverity.ALL)}.')
    if category not in RuleCategory.ALL:
        raise _Rejected(answer, f'"category" must be one of {", ".join(RuleCategory.ALL)}.')
    error = (
        validate_cel(cel)
        or validate_contract(contract)
        or check_closure(cel, {entry["name"] for entry in contract})
        or check_macro_targets(cel, contract)
    )
    if error:
        raise _Rejected(answer, error)
    # After validation, before the contract is turned into Fields: a gate the
    # model left off is derivable from the CEL's shape, so it is filled in here
    # rather than bounced back to the model (see apply_whole_macro_gate).
    apply_whole_macro_gate(cel, contract)
    new_fields = contracts.fields_from_entries(contract)
    conflict = contracts.find_field_conflict(new_fields, existing_fields)
    if conflict:
        raise _Rejected(answer, conflict)
    return description, rationale, cel, severity, category, contract


# Constrained-decoding schema for contract completion: the model may emit ONLY
# contract entries (the professor's CEL is immutable in that flow).
CONTRACT_ONLY_SCHEMA = {
    "type": "object",
    "properties": {"contract": RULE_JSON_SCHEMA["properties"]["contract"]},
    "required": ["contract"],
    "additionalProperties": False,
}


def complete_contract(
    rule: CourseRule, text: str, cel: str, missing: list[str], existing_json: str
) -> list[dict]:
    """Declare the fields a hand-written CEL references but the rule lacks.

    The professor's CEL and the existing contract entries are immutable: the
    model authors only the missing declarations, which the caller shows for
    review before anything is saved. One synchronous call, which the edit dialog
    waits on.

    Args:
        rule: Rule being edited, read for the fields its siblings declare.
        text: The rule's natural-language text, for context.
        cel: The professor's hand-written expression.
        missing: The field names it reads but nothing declares.
        existing_json: The contract as it stands, possibly an unsaved draft.

    Returns:
        The new contract entries, exactly the missing fields.

    Raises:
        ValueError: The answer was unusable, declared the wrong fields, or
            clashed with an existing one.
    """
    existing = contracts.fields_from_entries(json.loads(existing_json or "[]"))
    prompt = render_prompt(
        "rule_contract_completion.j2",
        request=text,
        cel=cel,
        existing=existing_json or "[]",
        missing=missing,
    )
    answer = get_rules_client().generate(
        prompt,
        extra_body={**NO_THINKING, "guided_json": CONTRACT_ONLY_SCHEMA},
    )
    try:
        entries = loads_model_json(answer).get("contract")
    except (ValueError, AttributeError):
        raise ValueError("The assistant's reply was not a valid JSON object.")
    error = validate_contract(entries)
    if error:
        raise ValueError(error)
    names = [e["name"] for e in entries]
    if sorted(names) != sorted(missing):
        raise ValueError(
            "The assistant declared the wrong fields "
            f"(got {', '.join(names) or 'none'}; needed {', '.join(missing)}). Try again."
        )
    new_fields = contracts.fields_from_entries(entries)
    conflict = contracts.find_field_conflict(
        new_fields, existing + _existing_fields(rule)
    )
    if conflict:
        raise ValueError(conflict)
    return entries


def complete_rule_contract(rule_id: int) -> None:
    """Declare the fields a hand-edited CEL references, on the rules worker.

    The edit dialog wrote the draft, the form values and the missing names, into
    rule.edit_draft as completing and enqueued this job; it then polls until the
    status flips to ready with the completed contract attached, or to failed
    with the error. Nothing else on the rule is touched.

    Args:
        rule_id: Rule whose draft to complete; an unknown id, or one with no
            draft, is a no-op.
    """
    db = SessionLocal()
    rule = db.get(CourseRule, rule_id)
    if rule is None or not rule.edit_draft:
        db.close()
        return
    try:
        draft = json.loads(rule.edit_draft)
    except ValueError:
        db.close()
        return
    if draft.get("status") != "completing":
        db.close()
        return
    try:
        new_entries = complete_contract(
            rule,
            draft["text"],
            draft["cel"],
            draft["missing"],
            json.dumps(draft["base_contract"]),
        )
        draft["contract"] = draft["base_contract"] + new_entries
        draft["status"] = "ready"
    except Exception as exc:  # surfaced verbatim in the dialog
        draft["status"] = "failed"
        draft["error"] = str(exc)
    # The model call took a while: meanwhile the draft may have been delivered,
    # superseded by a newer reconcile, or discarded by a save. Only the exact
    # draft this job picked up may be completed, never resurrect another.
    db.expire(rule, ["edit_draft"])
    current = rule.edit_draft
    try:
        still_ours = current and json.loads(current).get("queued_at") == draft.get("queued_at")
    except ValueError:
        still_ours = False
    if still_ours and json.loads(current).get("status") == "completing":
        rule.edit_draft = json.dumps(draft)
        db.commit()
    db.close()


def _existing_fields(rule: CourseRule) -> list[contracts.Field]:
    """The contract fields of the rules sharing an extraction pass with this one.

    A field is extracted once across all rules applicable to a document type, so
    a new rule's fields must be compared against every other rule that could be
    evaluated alongside it.

    Args:
        rule: Rule whose siblings to collect.

    Returns:
        Their contract fields.
    """
    fields: list[contracts.Field] = []
    own_types = set(DocumentType.parse(rule.document_type))
    for other in rule.course.rules:
        if other.id == rule.id:
            continue
        # Two rules share an extraction pass exactly when their target types
        # overlap, since a field is extracted once per type.
        if own_types.intersection(DocumentType.parse(other.document_type)):
            fields.extend(contracts.contract_fields(other))
    return fields


def generate_rule_cel(rule_id: int) -> None:
    """Compile a custom rule's natural-language request into CEL.

    The entry point of the rules worker. Owns its own session and records the
    outcome on the rule itself: a valid result becomes a proposal awaiting
    review, and an exhausted attempt budget marks the rule failed with the last
    error.

    Args:
        rule_id: Rule to compile; an unknown id, or one not awaiting
            generation, is a no-op.
    """
    db = SessionLocal()
    rule = db.get(CourseRule, rule_id)
    if rule is None or rule.status != RuleStatus.GENERATING:
        db.close()
        return
    try:
        client = get_rules_client()
        existing_fields = _existing_fields(rule)
        previous: _Rejected | None = None
        for attempt, temperature in enumerate(TEMPERATURES[:MAX_ATTEMPTS], start=1):
            # Expose progress so the Rules page can show what is happening.
            rule.attempts = attempt
            rule.error_message = previous.error if previous else None
            db.commit()

            if previous is None:
                prompt = render_prompt(
                    "rule_generation.j2",
                    request=rule.source_prompt,
                    document_types=DocumentType.parse(rule.document_type),
                    document_type_label=DocumentType.label(rule.document_type),
                )
            else:
                prompt = render_prompt(
                    "rule_refinement.j2",
                    request=rule.source_prompt,
                    previous=previous.answer,
                    error=previous.error,
                )
            answer = client.generate(
                prompt,
                temperature=temperature,
                extra_body={**NO_THINKING, "guided_json": RULE_JSON_SCHEMA},
            )
            try:
                description, rationale, cel, severity, category, contract = _parse_answer(
                    answer, existing_fields
                )
            except _Rejected as exc:
                logger.info(
                    "Rule %s attempt %s rejected: %s", rule_id, attempt, exc.error
                )
                previous = exc
                continue
            rule.text = description
            rule.rationale = rationale or None
            rule.cel = cel
            rule.severity = severity
            rule.category = category
            rule.extraction_contract = json.dumps(contract)
            rule.model_name = client.model
            rule.status = RuleStatus.PROPOSED
            rule.error_message = None
            db.commit()
            return
        rule.status = RuleStatus.FAILED
        rule.error_message = (
            f"No valid CEL rule after {MAX_ATTEMPTS} attempts. "
            "Please rephrase your request more precisely."
        )
        db.commit()
    except Exception as exc:
        logger.exception("Rule generation failed for rule %s", rule_id)
        db.rollback()
        rule.status = RuleStatus.FAILED
        rule.error_message = f"Generation failed: {exc}"
        db.commit()
    finally:
        db.close()
