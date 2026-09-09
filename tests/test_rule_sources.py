"""Provenance of the default rules: every rule cites something, and the
reference numbers the UI shows resolve to a real entry."""

import pathlib

import yaml

from app.rule_sources import NUMBERS, ORDER, RULE_SOURCES, SOURCES, numbered_sources, sources_for
from tests.conftest import login

RULES_YAML = pathlib.Path("app/rules/default_rules.yaml")


def _default_codes() -> set[str]:
    return {r["code"] for r in yaml.safe_load(RULES_YAML.read_text(encoding="utf-8"))}


# `all1` (spelling and grammar) rests on plain professional practice with no
# paper behind it, so it deliberately cites nothing and shows no reference mark.
UNCITED = {"all1"}


def test_every_default_rule_cites_a_source_or_is_a_known_exception():
    """A default rule should say where it came from, unless it has no source."""
    missing = _default_codes() - set(RULE_SOURCES) - UNCITED
    assert not missing, f"default rules with no recorded source: {sorted(missing)}"


def test_no_source_entry_points_at_a_rule_that_does_not_exist():
    """A stale entry would render a reference to nothing."""
    stale = set(RULE_SOURCES) - _default_codes()
    assert not stale, f"sources for rules no longer in the YAML: {sorted(stale)}"


def test_every_cited_key_resolves_to_an_entry():
    for code, keys in RULE_SOURCES.items():
        for key in keys:
            assert key in SOURCES, f"{code} cites unknown source {key!r}"
            assert key in ORDER, f"{key!r} has no reference number"


def test_reference_numbers_are_stable_and_contiguous():
    """The numbers are what a rule's mark links to, so they must be 1..n."""
    numbers = sorted(NUMBERS.values())
    assert numbers == list(range(1, len(ORDER) + 1))
    assert len(numbered_sources()) == len(ORDER)


def test_a_custom_rule_cites_nothing():
    """The professor wrote it, so there is nothing to cite."""
    assert sources_for(None) == []
    assert sources_for("sy999") == []


def test_sources_page_lists_every_entry(client, member):
    login(client, "user@example.com")
    page = client.get("/evidence")
    assert page.status_code == 200
    for number, source in numbered_sources():
        assert f'id="ref-{number}"' in page.text
        assert source.title in page.text
        # Titles and citations only; the page cites rather than argues.
        # Jinja escapes the "&" in author lists, so compare escaped text.
        head = source.citation.split(",")[0].replace("&", "&amp;")
        assert head in page.text, source.key
        if source.url:
            assert source.url in page.text, source.key


def test_rules_page_marks_link_to_the_right_entry(client, member, course):
    """The mark beside a rule points at that rule's own entry."""
    login(client, "user@example.com")
    page = client.get(f"/courses/{course.id}/rules")
    assert page.status_code == 200
    for source in sources_for("sy1"):
        assert f"#ref-{NUMBERS[source.key]}" in page.text
    # Reference marks appear only on default rules; a course starts with those.
    assert 'class="ref-mark"' in page.text


def test_a_link_is_always_a_doi_never_a_homepage():
    """The shape of a citation's link, not which paper it is.

    A homepage is not a citation: an entry once shipped pointing at a project's
    website instead of the paper. Checking the SHAPE catches that without
    freezing which works are cited, which is free to change as the rules do.
    """
    for source in SOURCES.values():
        if source.url:
            assert source.url.startswith("https://doi.org/"), source.key
            assert " " not in source.url, source.key


def test_every_entry_is_complete():
    """Each entry must actually cite something a reader can follow up."""
    for key, source in SOURCES.items():
        assert source.title.strip(), key
        assert source.citation.strip(), key
        # A citation names a work: an author or organisation and a year, or an
        # explicit statement that it is our own unpublished material.
        assert len(source.citation) > 20, key
