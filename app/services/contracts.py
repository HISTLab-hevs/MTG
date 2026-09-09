"""Extraction contracts and the per-document fact schema.

Pure functions over a rule's extraction_contract JSON. The engine extracts facts
page by page, then aggregates them into a single doc object the CEL evaluates
(see app/services/aggregation.py and rule_engine.py), so a rule's CEL reads
doc.<field>, never a per-page list. See Field for what a contract entry
declares.

Besides the declared fields, doc always carries the engine-supplied members in
BUILTIN_FIELDS, which no contract declares and no model extracts.
"""

import json
from dataclasses import dataclass

from app.models import CourseRule, DocumentType, RuleStatus

CONTRACT_TYPES = ("boolean", "number", "string", "string_list")

BUILTIN_FIELDS = ("page_count",)

AGG_KINDS = ("any", "all", "sum", "max", "min", "union", "first", "pages", "tagged_pages")

TYPE_DEFAULT = {
    "boolean": False,
    "number": 0,
    "string": "",
    "string_list": [],
}

AGG_BY_TYPE = {
    "boolean": "any",
    "number": "sum",
    "string": "first",
    "string_list": "union",
}


AGG_CHOICES = {
    "boolean": ("any", "all", "sum", "pages"),
    "number": ("sum", "max", "min", "first", "pages"),
    "string": ("first",),
    "string_list": ("union", "pages", "tagged_pages"),
}


@dataclass(frozen=True, kw_only=True)
class Field:
    """One fact a rule's CEL needs, as declared by its contract.

    Attributes:
        name: snake_case identifier, the member name under doc.
        type: One of CONTRACT_TYPES, the per-page value's type.
        instruction: One self-contained sentence for the per-page extractor.
        agg: How the per-page values collapse into the document value; see
            AGG_BY_TYPE for the default per type.
        gate: Whether this field is the rule's precondition, the "if" of a
            conditional rule such as !doc.mentions_ai || doc.explains_ai, rather
            than the substance of its verdict. When every gate field of a rule
            aggregates to a falsy value, the situation the rule checks for never
            occurs in the document, and a passing verdict is reported as not
            applicable rather than a pass (see report_view.RuleVerdict.outcome).
    """

    name: str
    type: str
    instruction: str
    agg: str
    gate: bool = False


def fields_from_entries(entries: object) -> list[Field]:
    """Parse a raw contract-entry list into fields.

    The gate flag comes only from the contract itself, declared by the default
    rules (app/rules/default_rules.yaml) and by the rule generator for custom
    ones. No field name is ever special-cased in code: a contract without the
    flag simply has no gates, and its rule can never be reported not applicable.

    Args:
        entries: The decoded contract, normally a list of entry dicts.

    Returns:
        The fields, skipping any malformed entry.
    """
    if not isinstance(entries, list):
        return []
    fields = []
    for e in entries:
        if not isinstance(e, dict) or not {"name", "type", "instruction"} <= e.keys():
            continue
        fields.append(
            Field(
                name=e["name"],
                type=e["type"],
                instruction=e["instruction"],
                agg=e.get("agg") or AGG_BY_TYPE.get(e["type"], "first"),
                gate=bool(e.get("gate")),
            )
        )
    return fields


def contract_fields(rule: CourseRule) -> list[Field]:
    """Parsed contract of a rule.

    Args:
        rule: Rule whose contract to read.

    Returns:
        Its fields; empty when the contract is absent or unreadable.
    """
    if not rule.extraction_contract:
        return []
    try:
        return fields_from_entries(json.loads(rule.extraction_contract))
    except ValueError:
        return []


def active_rules_for_types(
    rules: list[CourseRule], document_types: list[str]
) -> list[CourseRule]:
    """The rules that apply to a document, by type.

    Both sides may carry several types at once: a document can be several types,
    and a rule can be scoped to several. A rule that must hold for every
    document simply lists all four.

    Args:
        rules: Candidate rules, normally the course's.
        document_types: The document's types.

    Returns:
        The ready, active rules whose target types intersect the document's,
        each at most once even when they overlap on more than one type.
    """
    wanted = set(document_types)
    return [
        r
        for r in rules
        if r.is_active
        and r.status == RuleStatus.READY
        and r.cel
        and wanted.intersection(DocumentType.parse(r.document_type))
    ]


def union_fields(rules: list[CourseRule]) -> list[Field]:
    """Every field the rules need, deduplicated by name.

    Two rules may legitimately share a field, as several assessment rules each
    needing has_questions do, and it is then extracted once. A clash on type or
    instruction is caught earlier, at rule-generation time (find_field_conflict),
    so here the first definition is authoritative.

    Args:
        rules: Rules whose contracts to merge.

    Returns:
        The union of their fields.
    """
    seen: dict[str, Field] = {}
    for rule in rules:
        for field in contract_fields(rule):
            seen.setdefault(field.name, field)
    return list(seen.values())


def find_field_conflict(new_fields: list[Field], existing: list[Field]) -> str | None:
    """Reject a new field that reuses an existing name with a different meaning.

    Fields are extracted once per name across all rules of a document type, so a
    rule may safely reuse an existing field only if its type and instruction
    match exactly. A same-named field with a different type or instruction would
    silently win or lose the shared extraction slot.

    Args:
        new_fields: Fields the new or edited rule declares.
        existing: Fields already declared for the same document type.

    Returns:
        A message the professor can act on by renaming, or None when there is
        no conflict.
    """
    by_name = {f.name: f for f in existing}
    for f in new_fields:
        other = by_name.get(f.name)
        if other is None:
            continue
        if f.type != other.type or f.instruction.strip() != other.instruction.strip():
            return (
                f'Field "{f.name}" already exists for this document type with a '
                "different meaning. Rename this field, or reuse the existing one "
                "with the same type and instruction."
            )
    return None


def schema_for_fields(fields: list[Field]) -> dict:
    """Build the JSON schema for one page's extraction of these fields.

    Each field becomes an object of applicable, value and quote. Applicable says
    whether this page bears on the field at all, false when the page is silent
    on it, so a feature stated once on a multi-page document is not voted down
    by the pages that never mention it. Value carries the field's primitive
    type, the only thing the CEL ever sees. Quote is a short verbatim span from
    the page justifying the value, required whenever applicable is true and
    empty when the page is silent; aggregation keeps only the values, but the
    quote grounds the later report in the source text.

    The page number is known by the caller, so it is attached in code rather
    than asked of the model.

    Args:
        fields: Fields to extract from the page.

    Returns:
        The schema, passed to the model as guided_json.
    """
    json_type = {
        "boolean": {"type": "boolean"},
        "number": {"type": "number"},
        "string": {"type": "string"},
        "string_list": {"type": "array", "items": {"type": "string"}},
    }
    return {
        "type": "object",
        "properties": {
            f.name: {
                "type": "object",
                "properties": {
                    "applicable": {"type": "boolean"},
                    "value": json_type[f.type],
                    "quote": {"type": "string"},
                },
                "required": ["applicable", "value", "quote"],
                "additionalProperties": False,
            }
            for f in fields
        },
        "required": [f.name for f in fields],
        "additionalProperties": False,
    }
