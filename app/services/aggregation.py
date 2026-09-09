"""Aggregate per-page extracted facts into one document object.

The extractor returns one fact object per page, each field a {value, quote, page}
entry. Aggregation collapses those pages into a single doc object whose fields
are the per-page values combined by each field's declared agg (see
app/services/contracts.py): any and all for booleans, sum, max and min for
numbers, union for string lists, first for a single representative string. The
rule's CEL then reads doc.<field>, a scalar for these, so the empty-document edge
cases collapse to honest defaults instead of vacuous truth.

Two aggregations keep positional structure instead. The pages kind keeps the
ordered page numbers where a boolean field is true, so a CEL can reason about
position and adjacency across pages, a formula on page N followed by an example
on N or N+1. The tagged_pages kind keeps {item, page} pairs from a per-page list
of canonical tags, so a CEL can also match which item responds to which. The CEL
reads either list with an all or exists macro.

The quotes ride a parallel channel (build_evidence) that the CEL never sees. All
deterministic, no model call.
"""

import re
from difflib import SequenceMatcher

from app.services.contracts import TYPE_DEFAULT, Field


_ORDINALS = {"1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth",
             "5th": "fifth", "6th": "sixth", "7th": "seventh", "8th": "eighth",
             "9th": "ninth"}
_NUMERAL_WORDS = frozenset(_ORDINALS.values()) | frozenset(
    {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine"}
)
_STOPWORDS = frozenset({"the", "a", "an", "of", "for", "and", "to", "s"})
TAG_MATCH_THRESHOLD = 0.75

_WORD_SPLIT = re.compile(r"[\s']+")


def _tag_tokens(tag: str) -> list[str]:
    """Split a tag into comparable words, dropping stopwords and ordinals."""
    words = [_ORDINALS.get(w, w) for w in _WORD_SPLIT.split(tag) if w]
    kept = [w for w in words if w not in _STOPWORDS]
    return kept or words


def _is_numeral(word: str) -> bool:
    """Whether a word carries a number, spelled out or in digits."""
    return word in _NUMERAL_WORDS or word.isdigit()


def tag_similarity(a: str, b: str) -> float:
    """Compare two tags, so differently worded ones can still be paired.

    Tags carrying different numbers never match, since "first law" and "second
    law" name different things however alike they read.

    Args:
        a: One tag.
        b: The other.

    Returns:
        Their similarity, 0 to 1, computed deterministically.
    """
    ta, tb = _tag_tokens(a), _tag_tokens(b)
    if {w for w in ta if _is_numeral(w)} != {w for w in tb if _is_numeral(w)}:
        return 0.0

    def same_word(w: str, x: str) -> bool:
        stem = min(4, len(w), len(x))
        return w[:stem] == x[:stem] and SequenceMatcher(None, w, x).ratio() >= 0.8

    matched_a: set[int] = set()
    used: set[int] = set()
    for ai, w in enumerate(ta):
        i = next((i for i, x in enumerate(tb) if i not in used and same_word(w, x)), -1)
        if i >= 0:
            matched_a.add(ai)
            used.add(i)
    if any(len(ta[i]) <= 3 for i in range(len(ta)) if i not in matched_a) and any(
        len(tb[i]) <= 3 for i in range(len(tb)) if i not in used
    ):
        return 0.0
    token_ratio = 2 * len(matched_a) / (len(ta) + len(tb))

    if len(matched_a) == len(ta) or len(used) == len(tb):
        char_ratio = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
        return max(token_ratio, char_ratio)
    return token_ratio


def _canonicalize_tags(document: dict, fields: list[Field]) -> dict:
    """Rewrite similar tags across a rule's tagged_pages fields to one spelling.

    The extractor may name the same thing differently on two pages, which would
    stop the CEL from pairing them. Single-linkage clustering over the sorted
    distinct tags keeps this deterministic: each cluster's lexicographically
    smallest tag becomes the spelling used everywhere.

    Args:
        document: The aggregated document, rewritten in place.
        fields: The rule's fields; only the tagged_pages ones are touched.

    Returns:
        The same document, with its pairs rewritten and re-deduplicated.
    """
    tagged = [f.name for f in fields if f.agg == "tagged_pages"]
    tags = sorted({p["item"] for name in tagged for p in document[name]})
    if len(tags) < 2:
        return document
    canon: dict[str, str] = {}
    clusters: list[list[str]] = []
    for tag in tags:
        home = next(
            (c for c in clusters
             if any(tag_similarity(tag, member) >= TAG_MATCH_THRESHOLD for member in c)),
            None,
        )
        (clusters.append([tag]) if home is None else home.append(tag))
    for cluster in clusters:
        for member in cluster:
            canon[member] = cluster[0]  # sorted input, so the smallest is first
    for name in tagged:
        seen: list = []
        for pair in document[name]:
            rewritten = {"item": canon[pair["item"]], "page": pair["page"]}
            if rewritten not in seen:
                seen.append(rewritten)
        document[name] = seen
    return document


def _entry(page_fact: dict, name: str) -> dict:
    """A field's entry on one page, or an empty one when the page has none."""
    entry = page_fact.get(name)
    return entry if isinstance(entry, dict) else {}


def _bears_evidence(entry: dict) -> bool:
    """Whether this page speaks to the field, rather than being silent on it.

    The extractor states this directly through the applicable flag. Entries
    stored before that flag existed fall back to the evidence: a quote, or a
    positive value, means the page took a position.
    """
    if isinstance(entry.get("applicable"), bool):
        return entry["applicable"]
    return bool(entry.get("quote")) or bool(entry.get("value"))


def _aggregate(field: Field, entries: list[dict]) -> object:
    """Collapse one field's per-page entries by its declared aggregation.

    Args:
        field: Field being aggregated.
        entries: Its entry on each page, in page order.

    Returns:
        The document-level value the CEL will read, defaulted to the field's
        type when no page carried one.
    """
    agg = field.agg
    values = [e.get("value") for e in entries]
    if agg == "pages":
        # Positional: the ordered 1-based page numbers where the field is true,
        # read by the CEL with a macro, as in
        # doc.formula_pages.all(p, p in doc.example_pages || ...).
        pages = [
            e["page"]
            for e in entries
            if _bears_evidence(e) and bool(e.get("value")) and isinstance(e.get("page"), int)
        ]
        return sorted(dict.fromkeys(pages))
    if agg == "tagged_pages":
        # Identity-carrying positional: page-ordered {item, page} pairs, letting
        # the CEL match which item responds to which, as in
        # doc.formula_items.all(f, doc.example_items.exists(e, e.item == f.item
        # && e.page > f.page && e.page <= f.page + 3)). Tags are lowercased and
        # whitespace-collapsed so pairing does not hinge on capitalisation drift.
        pairs: list = []
        for e in entries:
            if not (_bears_evidence(e) and isinstance(e.get("page"), int)):
                continue
            for tag in e.get("value") or []:
                item = " ".join(str(tag).lower().split())
                pair = {"item": item, "page": e["page"]}
                if item and pair not in pairs:
                    pairs.append(pair)
        return sorted(pairs, key=lambda p: (p["page"], p["item"]))
    if field.type == "string_list" or agg == "union":
        seen: list = []
        for value in values:
            for item in value or []:
                if item not in seen:
                    seen.append(item)
        return seen
    if agg == "any":
        return any(bool(v) for v in values)
    if agg == "all":
        # Every page that bears on the field must satisfy it; pages silent on it
        # do not count as a failure, so a feature stated once on a multi-page
        # document is not voted down by the pages that never mention it. A field
        # genuinely absent everywhere is false.
        relevant = [e for e in entries if _bears_evidence(e)]
        return all(bool(e.get("value")) for e in relevant) if relevant else False
    if agg in ("sum", "max", "min"):
        numbers = [v for v in values if isinstance(v, (int, float))]
        if not numbers:
            return 0
        return {"sum": sum, "max": max, "min": min}[agg](numbers)
    for value in values:
        if value not in (None, TYPE_DEFAULT[field.type]):
            return value
    return TYPE_DEFAULT[field.type]


def _page_count(page_facts: list[dict]) -> int:
    """How many pages this document has, read off the facts themselves.

    Every entry carries the page it came from, set in code and never by the
    model, so the highest one is the document's length.

    Args:
        page_facts: One fact object per page.

    Returns:
        The page count, falling back to the number of fact objects when no entry
        carries a page number, as older stored facts do not.
    """
    numbers = [
        entry["page"]
        for page in page_facts
        for entry in page.values()
        if isinstance(entry, dict) and isinstance(entry.get("page"), int)
    ]
    return max(numbers) if numbers else len(page_facts)


def build_document(page_facts: list[dict], fields: list[Field]) -> dict:
    """Collapse per-page facts into the single doc object the CEL evaluates.

    Args:
        page_facts: One fact object per page, in page order.
        fields: The contract fields to aggregate.

    Returns:
        The document object, including the built-in page_count.
    """
    document = {
        f.name: _aggregate(f, [_entry(page, f.name) for page in page_facts])
        for f in fields
    }
    document["page_count"] = _page_count(page_facts)
    return _canonicalize_tags(document, fields)


def build_evidence(page_facts: list[dict], fields: list[Field]) -> dict:
    """Collect the verbatim source spans behind each field's value.

    This is the grounding the report uses to show where a value came from; it
    never reaches the CEL.

    Args:
        page_facts: One fact object per page, in page order.
        fields: The contract fields to collect quotes for.

    Returns:
        Every non-empty quote each field earned, in page order, as {quote, page}
        entries keyed by field name.
    """
    evidence: dict = {}
    for f in fields:
        spans = []
        for page in page_facts:
            entry = page.get(f.name)
            if not isinstance(entry, dict):
                continue
            quote = entry.get("quote")
            if quote:
                spans.append({"quote": quote, "page": entry.get("page")})
        evidence[f.name] = spans
    return evidence
