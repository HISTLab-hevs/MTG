"""Per-page fact aggregation, especially the `agg: all` "relevant pages" rule."""

from app.services.aggregation import build_document
from app.services.contracts import Field


def _bool_field(name, agg):
    return Field(name=name, type="boolean", agg=agg, instruction="x")


def _page(name, value, quote="", applicable=None):
    entry = {"value": value, "quote": quote, "page": 1}
    if applicable is not None:
        entry["applicable"] = applicable
    return {name: entry}


def test_all_uses_explicit_applicable_flag():
    """The model's explicit `applicable=false` excludes a page even if value=false."""
    field = _bool_field("has_weights", "all")
    pages = [
        _page("has_weights", False, "", applicable=False),  # page says: N/A here
        _page("has_weights", True, "Midterm 30%, Final 70%", applicable=True),
    ]
    assert build_document(pages, [field])["has_weights"] is True


def test_all_ignores_pages_silent_on_the_field():
    """A feature stated on one page isn't voted down by pages that omit it.

    Regression: `grading_components_have_weights` (agg: all) on a 2-page syllabus
    returned False because page 1 (no grading content) was False. Pages with no
    quote and a falsy value are silent and must not count as a failure.
    """
    field = _bool_field("has_weights", "all")
    pages = [
        _page("has_weights", False, quote=""),  # silent: no grading on this page
        _page("has_weights", True, quote="Midterm 30%, Final 70%"),  # states it
    ]
    assert build_document(pages, [field])["has_weights"] is True


def test_all_is_false_when_a_relevant_page_violates_it():
    """A page that DOES bear on the field but fails it still makes `all` false."""
    field = _bool_field("has_weights", "all")
    pages = [
        _page("has_weights", True, quote="Midterm 30%, Final 70%"),
        _page("has_weights", False, quote="Project: pass/fail, no weight"),  # relevant fail
    ]
    assert build_document(pages, [field])["has_weights"] is False


def test_all_is_false_when_absent_everywhere():
    """Genuinely missing on every page stays an honest False, not vacuous True."""
    field = _bool_field("has_weights", "all")
    pages = [_page("has_weights", False, ""), _page("has_weights", False, "")]
    assert build_document(pages, [field])["has_weights"] is False


def test_any_unaffected():
    """`agg: any` still fires on a single positive page."""
    field = _bool_field("has_policy", "any")
    pages = [_page("has_policy", False, ""), _page("has_policy", True, "Accommodations…")]
    assert build_document(pages, [field])["has_policy"] is True


def _numbered_page(name, value, page, quote="", applicable=None):
    entry = {"value": value, "quote": quote, "page": page}
    if applicable is not None:
        entry["applicable"] = applicable
    return {name: entry}


def test_pages_agg_yields_sorted_page_numbers():
    """`agg: pages` keeps the 1-based pages where the boolean is true, deduped
    and sorted — the positional data a cross-page rule reasons over."""
    field = _bool_field("formula", "pages")
    pages = [
        _numbered_page("formula", False, 1),
        _numbered_page("formula", True, 3, quote="E=mc^2"),
        _numbered_page("formula", False, 4),
        _numbered_page("formula", True, 7, quote="F=ma"),
    ]
    assert build_document(pages, [field])["formula"] == [3, 7]


def test_pages_agg_on_a_number_field_keeps_pages_with_a_nonzero_count():
    """`pages` is offered for count fields too (the edit form's way to turn a
    counting rule into a positional one): it keeps the pages whose count is
    non-zero, testing truthiness rather than the boolean type."""
    field = Field(name="formula_count", type="number", agg="pages", instruction="x")
    pages = [
        _numbered_page("formula_count", 0, 1),
        _numbered_page("formula_count", 2, 3, quote="E=mc^2; F=ma"),
        _numbered_page("formula_count", 1, 7, quote="PV=nRT"),
    ]
    assert build_document(pages, [field])["formula_count"] == [3, 7]


def test_pages_agg_is_empty_when_never_true():
    """No page bearing the field -> empty list (a vacuously-satisfiable rule)."""
    field = _bool_field("formula", "pages")
    pages = [_numbered_page("formula", False, 1), _numbered_page("formula", False, 2)]
    assert build_document(pages, [field])["formula"] == []


def test_pages_agg_excludes_pages_marked_not_applicable():
    """A page the extractor marks not-applicable is not counted even if value is
    truthy, mirroring the evidence rule used elsewhere."""
    field = _bool_field("formula", "pages")
    pages = [
        _numbered_page("formula", True, 2, quote="stated", applicable=True),
        _numbered_page("formula", True, 5, applicable=False),  # not a real formula page
    ]
    assert build_document(pages, [field])["formula"] == [2]


def test_tagged_pages_agg_yields_normalized_item_page_pairs():
    """`tagged_pages` keeps {item, page} pairs from per-page tag lists, deduped,
    page-ordered, lowercased — the identity a paired rule matches on."""
    field = Field(name="formula_items", type="string_list",
                  agg="tagged_pages", instruction="x")
    pages = [
        _numbered_page("formula_items", [], 1),
        _numbered_page("formula_items", ["Kinetic  Energy"], 2, quote="E=1/2mv^2"),
        _numbered_page("formula_items", ["kinetic energy", "Newton's law"], 4,
                       quote="F=ma"),
        _numbered_page("formula_items", ["ignored"], 5, applicable=False),
    ]
    assert build_document(pages, [field])["formula_items"] == [
        {"item": "kinetic energy", "page": 2},
        {"item": "kinetic energy", "page": 4},
        {"item": "newton's law", "page": 4},
    ]


def test_tagged_rule_catches_the_interleaved_case_proximity_misses():
    """The whole point of tagged_pages: formula A on p.2, formula B on p.4, and
    an example OF A on p.5 — plain proximity would pass B (something example-ish
    sits within its window), identity matching correctly fails it."""
    from app.models import CourseRule
    from app.services.rule_engine import evaluate

    formulas = Field(name="formula_items", type="string_list",
                     agg="tagged_pages", instruction="x")
    examples = Field(name="example_items", type="string_list",
                     agg="tagged_pages", instruction="x")
    cel = ("doc.formula_items.all(f, doc.example_items.exists(e, "
           "e.item == f.item && e.page > f.page && e.page <= f.page + 3))")
    rule = CourseRule(cel=cel)

    def doc(formula_by_page, example_by_page):
        pages = [
            {**_numbered_page("formula_items", formula_by_page.get(p, []), p),
             **_numbered_page("example_items", example_by_page.get(p, []), p)}
            for p in range(1, 7)
        ]
        return build_document(pages, [formulas, examples])

    interleaved = doc({2: ["kinetic energy"], 4: ["newton's law"]},
                      {5: ["kinetic energy"]})
    assert evaluate(rule, interleaved) is False  # B's own example never comes
    both = doc({2: ["kinetic energy"], 4: ["newton's law"]},
               {5: ["kinetic energy"], 6: ["newton's law"]})
    assert evaluate(rule, both) is True
    assert evaluate(rule, doc({}, {})) is True  # no formulas: vacuously fine


# Calibration set for the tag matcher: drift pairs OBSERVED in real-model QA
# (plus plausible neighbours) that must merge, and same-family pairs that must
# never merge. TAG_MATCH_THRESHOLD sits in the gap between the two.
MUST_MATCH = [
    ("newton's 2nd law", "second law of newton"),   # observed drift
    ("kinetic energy", "kinetic energy formula"),   # observed drift
    ("law of demand", "the law of demand"),
    ("ohm's law", "ohms law"),
    ("pythagorean theorem", "pythagoras theorem"),
    ("supply and demand", "demand and supply"),
    ("photosynthesis", "photosynthesis process"),
    # other domains: history, law, biology, computing
    ("treaty of versailles", "the versailles treaty"),
    ("pearl harbor attack", "attack on pearl harbor"),
    ("habeas corpus", "writ of habeas corpus"),
    ("dna replication", "replication of dna"),
    ("binary search", "binary search algorithm"),
]
MUST_NOT_MATCH = [
    ("kinetic energy", "potential energy"),
    ("first law of thermodynamics", "second law of thermodynamics"),
    ("newton's first law", "newton's second law"),
    ("law of demand", "law of supply"),
    ("bayes theorem", "binomial theorem"),
    ("supply curve", "demand curve"),
    ("marginal cost", "marginal revenue"),
    # other domains, including near-twin sibling names
    ("world war one", "world war two"),
    ("french revolution", "russian revolution"),
    ("treaty of versailles", "treaty of vienna"),
    ("mitosis", "meiosis"),
    ("binary search", "linear search"),
    # acronym-grade: one letter IS the identity
    ("dna replication", "rna replication"),
    # abbreviation vs full word: kept apart BY DESIGN (conservative side —
    # the extractor is instructed to use standard names, never abbreviations)
    ("std deviation", "standard deviation"),
    # synonyms: string similarity cannot know these are the same, and a
    # matcher loose enough to catch them would merge distinct items too.
    ("normal distribution", "gaussian distribution"),
    # the un-named formula drift: one shared generic word is exactly what
    # distinct concepts share too, so the matcher must NOT reach for it.
    ("decay relation", "exponential decay"),
]


def test_tag_similarity_separates_drift_from_distinct_items():
    from app.services.aggregation import TAG_MATCH_THRESHOLD, tag_similarity

    for a, b in MUST_MATCH:
        s = tag_similarity(a, b)
        assert s >= TAG_MATCH_THRESHOLD, f"should merge: {a!r} ~ {b!r} ({s:.2f})"
    for a, b in MUST_NOT_MATCH:
        s = tag_similarity(a, b)
        assert s < TAG_MATCH_THRESHOLD, f"must stay apart: {a!r} vs {b!r} ({s:.2f})"


def test_tagged_pages_canonicalization_repairs_cross_page_drift():
    """The observed variant-name drift, end to end: the two spellings collapse
    to one canonical tag, so the identity CEL pairs them again — while the
    distinct second formula stays unmatched."""
    from app.models import CourseRule
    from app.services.rule_engine import evaluate

    formulas = Field(name="formula_items", type="string_list",
                     agg="tagged_pages", instruction="x")
    examples = Field(name="example_items", type="string_list",
                     agg="tagged_pages", instruction="x")
    pages = [
        {**_numbered_page("formula_items", ["Newton's 2nd Law"], 2),
         **_numbered_page("example_items", [], 2)},
        {**_numbered_page("formula_items", [], 3),
         **_numbered_page("example_items", ["second law of Newton"], 3)},
        {**_numbered_page("formula_items", ["first law of thermodynamics"], 4),
         **_numbered_page("example_items", [], 4)},
    ]
    document = build_document(pages, [formulas, examples])
    # Both spellings of Newton's law collapsed to one tag...
    (newton,) = [p["item"] for p in document["example_items"]]
    assert newton in {p["item"] for p in document["formula_items"]}
    # ...while thermodynamics stayed its own item, so the rule rightly fails.
    cel = ("doc.formula_items.all(f, doc.example_items.exists(e, "
           "e.item == f.item && e.page >= f.page && e.page <= f.page + 3))")
    assert evaluate(CourseRule(cel=cel), document) is False


def test_cross_page_rule_evaluates_adjacency_end_to_end():
    """The whole point: build the `pages` lists and let the CEL judge that every
    formula page has an example on the same or the next page."""
    from app.models import CourseRule
    from app.services.rule_engine import evaluate

    formula = _bool_field("formula_pages", "pages")
    example = _bool_field("example_pages", "pages")
    cel = "doc.formula_pages.all(p, (p in doc.example_pages) || ((p + 1) in doc.example_pages))"
    rule = CourseRule(cel=cel)

    def doc(formula_pages, example_pages):
        return build_document(
            [_numbered_page("formula_pages", p in formula_pages, p) for p in range(1, 9)]
            + [_numbered_page("example_pages", p in example_pages, p) for p in range(1, 9)],
            [formula, example],
        )

    # formula@3 -> example@4 (next page) and formula@7 -> example@7 (same page): pass
    assert evaluate(rule, doc({3, 7}, {4, 7})) is True
    # formula@3 with its example far away (@8): fail
    assert evaluate(rule, doc({3}, {8})) is False
    # no formula anywhere: vacuously satisfied
    assert evaluate(rule, doc(set(), {5})) is True
