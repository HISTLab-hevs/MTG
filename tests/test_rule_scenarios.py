"""End-to-end rule *reasoning* tests: a professor asks for X, we express it as
CEL + an extraction contract, and we assert the deterministic verdict is correct
on realistic — often adversarial — documents.

Unlike test_rule_generation (which stubs the LLM and checks the compile job) and
test_aggregation (which checks one agg in isolation), each test here takes a
plausible professor request through the *whole* deterministic chain that decides
a verdict: per-page facts -> build_document (aggregation) -> CEL evaluation. The
CEL/contract is the one a correct NL->CEL compilation would produce; every
expression is also run through the real validators so a scenario cannot silently
use CEL the generator would reject. The cases deliberately include the traps the
generation prompt warns about (coverage vs. presence, whose-quantity, positional
adjacency) and weird documents (empty, single page, everything on one page,
things present but in the wrong place).
"""

import pytest

from app.models import CourseRule
from app.services.aggregation import build_document
from app.services.contracts import Field
from app.services.rule_engine import evaluate
from app.services.rule_generation import (
    check_closure,
    check_macro_targets,
    validate_cel,
    validate_contract,
)


# --- tiny builders -----------------------------------------------------------

def field(name, type_, agg):
    return Field(name=name, type=type_, instruction="x", agg=agg)


def page(page_number, **field_values):
    """One page's fact object: {field: {value, quote, page, applicable}}.

    Pass field=value; a truthy value marks the page applicable and quoted so it
    counts, mirroring what a real extractor emits when it finds the thing.
    """
    facts = {}
    for name, value in field_values.items():
        applicable = value not in (None, False, 0, "", [], ())
        facts[name] = {
            "value": value,
            "quote": "grounded" if applicable else "",
            "page": page_number,
            "applicable": applicable,
        }
    return facts


def verdict(cel, fields, pages):
    """Build the doc from pages, evaluate the CEL — the full deterministic path."""
    doc = build_document(pages, fields)
    return evaluate(CourseRule(cel=cel), doc)


def assert_expressible(cel, fields):
    """A scenario's CEL/contract must be something the generator could produce."""
    contract = [
        {"name": f.name, "type": f.type, "agg": f.agg,
         "instruction": "x"}
        for f in fields
    ]
    assert validate_cel(cel) is None, cel
    assert validate_contract(contract) is None, cel
    assert check_closure(cel, {f.name for f in fields}) is None, cel
    assert check_macro_targets(cel, contract) is None, cel


# --- positional / cross-page (the new capability) ----------------------------

class TestPositional:
    """'X must come before/after Y' — only expressible with agg=pages."""

    FORMULA = field("formula_pages", "boolean", "pages")
    EXAMPLE = field("example_pages", "boolean", "pages")
    CEL = ("doc.formula_pages.all(p, (p in doc.example_pages) || "
           "((p + 1) in doc.example_pages))")

    def _pages(self, formulas, examples, n=8):
        return [
            page(p, formula_pages=p in formulas, example_pages=p in examples)
            for p in range(1, n + 1)
        ]

    def test_expressible(self):
        assert_expressible(self.CEL, [self.FORMULA, self.EXAMPLE])

    def test_example_on_next_page_passes(self):
        assert verdict(self.CEL, [self.FORMULA, self.EXAMPLE],
                       self._pages({3}, {4})) is True

    def test_example_on_same_page_passes(self):
        assert verdict(self.CEL, [self.FORMULA, self.EXAMPLE],
                       self._pages({3}, {3})) is True

    def test_example_two_pages_later_fails(self):
        # The whole reason the old design was wrong: present-but-not-adjacent.
        assert verdict(self.CEL, [self.FORMULA, self.EXAMPLE],
                       self._pages({3}, {5})) is False

    def test_example_before_the_formula_does_not_count(self):
        # An example on page 2 does not satisfy a formula introduced on page 3.
        assert verdict(self.CEL, [self.FORMULA, self.EXAMPLE],
                       self._pages({3}, {2})) is False

    def test_one_of_several_formulas_uncovered_fails(self):
        assert verdict(self.CEL, [self.FORMULA, self.EXAMPLE],
                       self._pages({3, 7}, {4})) is False

    def test_all_formulas_covered_passes(self):
        assert verdict(self.CEL, [self.FORMULA, self.EXAMPLE],
                       self._pages({3, 7}, {3, 8})) is True

    def test_no_formula_is_vacuously_satisfied(self):
        assert verdict(self.CEL, [self.FORMULA, self.EXAMPLE],
                       self._pages(set(), {5})) is True

    def test_formula_on_last_page_needs_example_on_it(self):
        # p+1 would be off the deck; only a same-page example can satisfy it.
        assert verdict(self.CEL, [self.FORMULA, self.EXAMPLE],
                       self._pages({8}, {8}, n=8)) is True
        assert verdict(self.CEL, [self.FORMULA, self.EXAMPLE],
                       self._pages({8}, set(), n=8)) is False


class TestAbsolutePosition:
    """'The LAST slide must be a summary' — position relative to the document's end.

    Needs the one thing a per-page extractor cannot know: how long the document
    is. The engine supplies it as `doc.page_count`, so the CEL does arithmetic
    instead of the model guessing (see TestPageTotalMustNotBeExtracted).
    """

    SUMMARY = field("summary_pages", "boolean", "pages")
    CEL = "doc.summary_pages.exists(p, p == doc.page_count)"

    def _pages(self, summaries, n=6):
        return [page(p, summary_pages=p in summaries) for p in range(1, n + 1)]

    def test_expressible(self):
        assert_expressible(self.CEL, [self.SUMMARY])

    def test_summary_on_last_page_passes(self):
        assert verdict(self.CEL, [self.SUMMARY], self._pages({6}, n=6)) is True

    def test_summary_in_the_middle_fails(self):
        assert verdict(self.CEL, [self.SUMMARY], self._pages({3}, n=6)) is False

    def test_second_to_last_is_not_the_end(self):
        assert verdict(self.CEL, [self.SUMMARY], self._pages({5}, n=6)) is False

    def test_no_summary_at_all_fails(self):
        # `exists` over an empty list is false — an absence must not pass.
        assert verdict(self.CEL, [self.SUMMARY], self._pages(set(), n=6)) is False

    def test_single_page_document_is_its_own_last_page(self):
        assert verdict(self.CEL, [self.SUMMARY], self._pages({1}, n=1)) is True

    def test_same_content_different_deck_length_flips_the_verdict(self):
        # The point of page_count: page 6 is the end of a 6-page deck and the
        # middle of a 12-page one, and the rule must tell those apart.
        assert verdict(self.CEL, [self.SUMMARY], self._pages({6}, n=6)) is True
        assert verdict(self.CEL, [self.SUMMARY], self._pages({6}, n=12)) is False


class TestAbsolutePositionFractions:
    """'The final third must hold a self-check' — a fraction of the document."""

    CHECK = field("self_check_pages", "boolean", "pages")
    # Multiplied out rather than divided, so integer division cannot round the
    # boundary the wrong way on short decks.
    CEL = "doc.self_check_pages.exists(p, p * 3 > doc.page_count * 2)"

    def _pages(self, checks, n=9):
        return [page(p, self_check_pages=p in checks) for p in range(1, n + 1)]

    def test_expressible(self):
        assert_expressible(self.CEL, [self.CHECK])

    def test_check_inside_final_third_passes(self):
        # 9 pages: the final third is 7, 8, 9.
        assert verdict(self.CEL, [self.CHECK], self._pages({7})) is True

    def test_check_just_before_the_boundary_fails(self):
        assert verdict(self.CEL, [self.CHECK], self._pages({6})) is False

    def test_boundary_holds_on_a_length_that_does_not_divide_evenly(self):
        # 10 pages: 2/3 is 6.67, so page 7 is inside the final third and 6 is not.
        assert verdict(self.CEL, [self.CHECK], self._pages({7}, n=10)) is True
        assert verdict(self.CEL, [self.CHECK], self._pages({6}, n=10)) is False

    def test_first_pages_never_satisfy_it(self):
        assert verdict(self.CEL, [self.CHECK], self._pages({1, 2})) is False


class TestAbsolutePositionOpening:
    """'An agenda within the first two slides' — already expressible, still guarded."""

    AGENDA = field("agenda_pages", "boolean", "pages")
    CEL = "doc.agenda_pages.exists(p, p <= 2)"

    def _pages(self, agendas, n=8):
        return [page(p, agenda_pages=p in agendas) for p in range(1, n + 1)]

    def test_expressible(self):
        assert_expressible(self.CEL, [self.AGENDA])

    def test_agenda_on_page_two_passes(self):
        assert verdict(self.CEL, [self.AGENDA], self._pages({2})) is True

    def test_agenda_on_page_three_fails(self):
        assert verdict(self.CEL, [self.AGENDA], self._pages({3})) is False


class TestAbsolutePositionOnlyAtEnd:
    """'References may appear ONLY at the end' — `all`, not `exists`."""

    REFS = field("reference_pages", "boolean", "pages")
    CEL = "doc.reference_pages.all(p, p * 4 > doc.page_count * 3)"

    def _pages(self, refs, n=8):
        return [page(p, reference_pages=p in refs) for p in range(1, n + 1)]

    def test_expressible(self):
        assert_expressible(self.CEL, [self.REFS])

    def test_all_references_in_the_last_quarter_pass(self):
        assert verdict(self.CEL, [self.REFS], self._pages({7, 8})) is True

    def test_one_stray_reference_early_fails(self):
        assert verdict(self.CEL, [self.REFS], self._pages({2, 8})) is False


class TestPageTotalMustNotBeExtracted:
    """The workaround a model reaches for without page_count, and why it fails.

    Probed against the real generation model before page_count existed: asked for
    "the last slide must be a summary" it invented a `last_page_number` number
    field, which passes every validator yet cannot work — the extractor reads one
    page with no idea how many follow, so the honest per-page answer is 0 (or its
    own page number, which only accidentally lands on the total). Both readings
    are asserted here so the built-in stays justified.
    """

    SUMMARY = field("summary_pages", "boolean", "pages")
    GUESSED = field("last_page_number", "number", "max")
    CEL = "doc.summary_pages.exists(p, p == doc.last_page_number)"

    def _pages(self, summary_on, guess, n=5):
        return [
            page(p, summary_pages=p == summary_on, last_page_number=guess(p))
            for p in range(1, n + 1)
        ]

    def test_extractor_cannot_know_the_total_so_a_correct_deck_fails(self):
        # Summary genuinely on the last page, but no page can state the total:
        # a false negative on a document that satisfies the professor's rule.
        assert verdict(self.CEL, [self.SUMMARY, self.GUESSED],
                       self._pages(5, lambda p: 0)) is False

    def test_the_builtin_gets_the_same_deck_right(self):
        assert verdict("doc.summary_pages.exists(p, p == doc.page_count)",
                       [self.SUMMARY], self._pages(5, lambda p: 0)) is True

    def test_declaring_the_page_total_as_a_field_is_rejected(self):
        contract = [{"name": "page_count", "type": "number", "agg": "max",
                     "instruction": "The number of pages in the document."}]
        assert validate_contract(contract) is not None

    def test_page_count_needs_no_declaration(self):
        # Closure accepts the built-in without a contract entry, and does not
        # then complain that a declared field went unused.
        assert check_closure(
            "doc.summary_pages.exists(p, p == doc.page_count)", {"summary_pages"}
        ) is None


class TestMacroOverScalarIsRejected:
    """A macro only works over a list — `pages`/`tagged_pages` and nothing else.

    Observed from the real generation model on "every slide must carry the
    course code in its footer": it answered
    `doc.has_course_code_in_footer.all(v, v == true)` over an agg="all" boolean.
    That is valid CEL and passes closure, but the aggregated value is a plain
    bool, so celpy raises "BoolType object is not iterable" and the rule yields
    no verdict at all. check_macro_targets rejects it at generation time.
    """

    CONTRACT = [{"name": "has_footer", "type": "boolean", "agg": "all",
                 "instruction": "True if THIS page carries the course code."}]

    def test_macro_over_a_scalar_boolean_is_rejected(self):
        error = check_macro_targets("doc.has_footer.all(v, v == true)", self.CONTRACT)
        assert error is not None and "collapses it" in error

    def test_the_scalar_macro_really_would_have_crashed(self):
        f = field("has_footer", "boolean", "all")
        with pytest.raises(Exception):
            verdict("doc.has_footer.all(v, v == true)", [f],
                    [page(1, has_footer=True), page(2, has_footer=True)])

    def test_reading_the_scalar_directly_is_accepted(self):
        assert check_macro_targets("doc.has_footer", self.CONTRACT) is None

    def test_macro_over_a_pages_field_is_accepted(self):
        contract = [{"name": "formula_pages", "type": "boolean", "agg": "pages",
                     "instruction": "True if THIS page states a formula."}]
        assert check_macro_targets(
            "doc.formula_pages.exists(p, p == doc.page_count)", contract
        ) is None

    def test_macro_over_a_tagged_pages_field_is_accepted(self):
        contract = [
            {"name": "formula_items", "type": "string_list", "agg": "tagged_pages",
             "instruction": "Tag each formula on THIS page."},
            {"name": "example_items", "type": "string_list", "agg": "tagged_pages",
             "instruction": "Tag each worked example on THIS page."},
        ]
        assert check_macro_targets(
            "doc.formula_items.all(f, doc.example_items.exists(e, e.item == f.item))",
            contract,
        ) is None

    def test_a_field_with_no_explicit_agg_uses_its_type_default(self):
        # No "agg" key: a string_list defaults to union, still a bad macro target
        # for page arithmetic even though union does yield a list.
        contract = [{"name": "topics", "type": "string_list",
                     "instruction": "List the topics on THIS page."}]
        assert check_macro_targets("doc.topics.all(t, t != '')", contract) is not None


class TestPositionalPrecedence:
    """'A definition must appear before it is used' — order the other way."""

    DEFN = field("definition_pages", "boolean", "pages")
    USE = field("use_pages", "boolean", "pages")
    # Every page that USES the term must have a definition on an earlier or equal page.
    CEL = "doc.use_pages.all(u, doc.definition_pages.exists(d, d <= u))"

    def _pages(self, defs, uses, n=6):
        return [
            page(p, definition_pages=p in defs, use_pages=p in uses)
            for p in range(1, n + 1)
        ]

    def test_expressible(self):
        assert_expressible(self.CEL, [self.DEFN, self.USE])

    def test_defined_before_use_passes(self):
        assert verdict(self.CEL, [self.DEFN, self.USE], self._pages({2}, {4})) is True

    def test_used_before_defined_fails(self):
        assert verdict(self.CEL, [self.DEFN, self.USE], self._pages({5}, {2})) is False

    def test_never_used_is_vacuous_pass(self):
        assert verdict(self.CEL, [self.DEFN, self.USE], self._pages({3}, set())) is True


class TestPositionalQuestionThenAnswer:
    """'Each in-class question must be answered on the following page.'"""

    Q = field("question_pages", "boolean", "pages")
    A = field("answer_pages", "boolean", "pages")
    CEL = "doc.question_pages.all(q, (q + 1) in doc.answer_pages)"

    def _pages(self, qs, ans, n=6):
        return [page(p, question_pages=p in qs, answer_pages=p in ans)
                for p in range(1, n + 1)]

    def test_the_rule_is_expressible(self):
        assert_expressible(self.CEL, [self.Q, self.A])

    def test_answer_strictly_next_page(self):
        assert verdict(self.CEL, [self.Q, self.A], self._pages({2}, {3})) is True

    def test_answer_same_page_fails_this_stricter_rule(self):
        # This rule demands the NEXT page specifically, not the same one.
        assert verdict(self.CEL, [self.Q, self.A], self._pages({2}, {2})) is False


# --- coverage vs. presence (the classic trap) --------------------------------

class TestCoverageTrap:
    """'The syllabus must state every course topic' is a COUNT rule, not a
    presence boolean; one page naming one topic must not satisfy it."""

    TOPICS = field("stated_topics", "string_list", "union")
    CEL = "size(doc.stated_topics) >= 5"

    def test_expressible(self):
        assert_expressible(self.CEL, [self.TOPICS])

    def test_one_topic_on_one_page_fails(self):
        pages = [page(1, stated_topics=["Intro"]), page(2, stated_topics=[])]
        assert verdict(self.CEL, [self.TOPICS], pages) is False

    def test_topics_spread_across_pages_are_unioned(self):
        pages = [
            page(1, stated_topics=["A", "B"]),
            page(2, stated_topics=["C", "D", "E"]),
        ]
        assert verdict(self.CEL, [self.TOPICS], pages) is True

    def test_duplicate_topics_do_not_inflate_the_count(self):
        pages = [
            page(1, stated_topics=["A", "B", "C"]),
            page(2, stated_topics=["A", "B", "C"]),  # repeats -> union stays 3
        ]
        assert verdict(self.CEL, [self.TOPICS], pages) is False


# --- proportion / ratio ------------------------------------------------------

class TestProportion:
    """'At least 60% of questions must require reasoning.'"""

    TOTAL = field("question_count", "number", "sum")
    GOOD = field("reasoning_count", "number", "sum")
    CEL = ("doc.question_count > 0 && "
           "doc.reasoning_count * 100 / doc.question_count >= 60")

    def test_expressible(self):
        assert_expressible(self.CEL, [self.TOTAL, self.GOOD])

    def test_majority_reasoning_passes(self):
        pages = [page(1, question_count=5, reasoning_count=3),
                 page(2, question_count=5, reasoning_count=3)]
        assert verdict(self.CEL, [self.TOTAL, self.GOOD], pages) is True  # 6/10

    def test_minority_reasoning_fails(self):
        pages = [page(1, question_count=10, reasoning_count=2)]
        assert verdict(self.CEL, [self.TOTAL, self.GOOD], pages) is False  # 2/10

    def test_empty_assessment_does_not_divide_by_zero_and_fails(self):
        pages = [page(1, question_count=0, reasoning_count=0)]
        assert verdict(self.CEL, [self.TOTAL, self.GOOD], pages) is False


# --- whose quantity (document size vs. prescribed value) ---------------------

class TestWhoseQuantity:
    """'A written assignment must require at least 1000 words' is about the
    length the task PRESCRIBES, not the length of the brief. The contract must
    read the required count; a 3-page brief that asks for 200 words must fail."""

    REQUIRED = field("required_word_count", "number", "max")
    CEL = "doc.required_word_count >= 1000"

    def test_expressible(self):
        assert_expressible(self.CEL, [self.REQUIRED])

    def test_prescribed_length_met(self):
        pages = [page(1, required_word_count=1500)]
        assert verdict(self.CEL, [self.REQUIRED], pages) is True

    def test_prescribed_length_too_small_fails(self):
        pages = [page(1, required_word_count=200), page(2, required_word_count=0)]
        assert verdict(self.CEL, [self.REQUIRED], pages) is False

    def test_no_stated_requirement_reads_zero_and_fails(self):
        pages = [page(1, required_word_count=0), page(2, required_word_count=0)]
        assert verdict(self.CEL, [self.REQUIRED], pages) is False


# --- violation ('no page may…') ----------------------------------------------

class TestViolation:
    """'No slide may go off topic' — a violation boolean (agg any), negated, so
    an empty deck cannot pass by accident."""

    OFF = field("has_off_topic", "boolean", "any")
    CEL = "!doc.has_off_topic"

    def test_expressible(self):
        assert_expressible(self.CEL, [self.OFF])

    def test_clean_deck_passes(self):
        pages = [page(1, has_off_topic=False), page(2, has_off_topic=False)]
        assert verdict(self.CEL, [self.OFF], pages) is True

    def test_single_off_topic_page_fails_the_whole_deck(self):
        pages = [page(1, has_off_topic=False), page(2, has_off_topic=True)]
        assert verdict(self.CEL, [self.OFF], pages) is False


# --- universal ('every page must…') ------------------------------------------

class TestUniversal:
    """'Every slide must carry the course/page footer' — agg all: a page silent
    on it is not a failure, but a page that clearly lacks it is."""

    FOOTER = field("has_footer", "boolean", "all")
    CEL = "doc.has_footer"

    def test_expressible(self):
        assert_expressible(self.CEL, [self.FOOTER])

    def test_footer_on_every_relevant_page_passes(self):
        pages = [page(1, has_footer=True), page(2, has_footer=True)]
        assert verdict(self.CEL, [self.FOOTER], pages) is True

    def test_one_page_missing_footer_fails(self):
        pages = [
            {"has_footer": {"value": True, "quote": "©", "page": 1, "applicable": True}},
            {"has_footer": {"value": False, "quote": "", "page": 2, "applicable": True}},
        ]
        assert verdict(self.CEL, [self.FOOTER], pages) is False


# --- combined / weird documents ----------------------------------------------

class TestWeirdDocuments:
    """Edge documents a professor's material really produces."""

    def test_empty_document_presence_rule_fails_not_crashes(self):
        f = field("has_policy", "boolean", "any")
        assert verdict("doc.has_policy", [f], []) is False

    def test_everything_on_a_single_page_positional_rule(self):
        # A one-page handout with a formula AND its example on that same page.
        fp = field("formula_pages", "boolean", "pages")
        ep = field("example_pages", "boolean", "pages")
        cel = "doc.formula_pages.all(p, (p in doc.example_pages) || ((p+1) in doc.example_pages))"
        assert_expressible(cel, [fp, ep])
        pages = [page(1, formula_pages=True, example_pages=True)]
        assert verdict(cel, [fp, ep], pages) is True

    def test_two_rules_share_a_field_without_interfering(self):
        # Same field name feeds two independent expressions; aggregation is per
        # field, so both read the same list correctly.
        fp = field("formula_pages", "boolean", "pages")
        pages = [page(1, formula_pages=True), page(2, formula_pages=False),
                 page(3, formula_pages=True)]
        doc = build_document(pages, [fp])
        assert evaluate(CourseRule(cel="size(doc.formula_pages) >= 2"), doc) is True
        assert evaluate(CourseRule(cel="size(doc.formula_pages) >= 3"), doc) is False


# --- the validators reject genuinely bad rules -------------------------------

class TestGeneratorGuards:
    """Things a professor's request must NOT be allowed to compile into."""

    def test_string_matching_on_extracted_field_is_banned(self):
        # "slides must mention 'GDPR'" must become a boolean field, not contains().
        assert check_closure('doc.body.contains("GDPR")', {"body"}) is not None

    def test_expression_referencing_undeclared_field_is_rejected(self):
        assert check_closure("doc.a && doc.b", {"a"}) is not None

    def test_declared_but_unused_field_is_rejected(self):
        assert check_closure("doc.a", {"a", "b"}) is not None

    def test_malformed_cel_is_rejected(self):
        assert validate_cel("doc.formula_pages.all(p, ") is not None

    # Every advertised agg is accepted — WITH a compatible type. The validator
    # enforces type/agg compatibility (a "union" over a boolean would crash the
    # aggregator at report time), so each agg is paired with a type from
    # AGG_CHOICES, and one incompatible pairing is asserted rejected.
    @pytest.mark.parametrize("agg,ftype", [
        ("any", "boolean"), ("all", "boolean"), ("sum", "number"),
        ("max", "number"), ("min", "number"), ("union", "string_list"),
        ("first", "string"), ("pages", "boolean"),
        ("tagged_pages", "string_list"),
    ])
    def test_every_advertised_agg_is_accepted_by_the_contract_validator(self, agg, ftype):
        contract = [{"name": "f", "type": ftype,
                     "agg": agg, "instruction": "x"}]
        assert validate_contract(contract) is None

    def test_type_incompatible_agg_is_rejected(self):
        contract = [{"name": "f", "type": "boolean",
                     "agg": "union", "instruction": "x"}]
        assert "incompatible" in validate_contract(contract)
