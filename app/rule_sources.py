"""Where the default rules come from.

Every default rule is grounded either in the project's own faculty focus groups
or in published work on teaching and assessment. That provenance already lived
in the comments of app/rules/default_rules.yaml; this module makes it data, so
the app can show a professor why a rule exists and link it to the work behind it.

A rule is inspired by its source, not a mechanical implementation of it. This
module records only where a rule comes from and what kind of work that is; the
case for a rule is argued in the comments of default_rules.yaml, which stay the
place for it.

The numbering comes from ORDER and is what the superscript on a rule links to,
so it must stay stable.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Source:
    """One entry in the evidence list behind the default rules."""

    key: str
    title: str
    citation: str
    url: str = ""
    rules: tuple[str, ...] = field(default_factory=tuple)


ORDER = [
    "bloom",
    "transparent",
    "retrieval",
    "signaling",
    "evaljudge",
    "aias",
    "redesign",
    "ai-resist",
    "wicked",
    "ai-limits",
    "benchmarks",
    "udl",
    "focus-groups",
]

SOURCES: dict[str, Source] = {
    "focus-groups": Source(
        key="focus-groups",
        title="MTG faculty focus groups",
        citation="Mind the Gap project, focus groups with teaching faculty "
    ),
    "bloom": Source(
        key="bloom",
        title="Constructive alignment and the revised Bloom taxonomy",
        citation="Biggs (1996), Enhancing teaching through constructive "
                 "alignment, Higher Education 32:347-364; Anderson & Krathwohl "
                 "(2001), A Taxonomy for Learning, Teaching and Assessing",
        url="https://doi.org/10.1007/BF00138871",
    ),
    "transparent": Source(
        key="transparent",
        title="Transparency in assignments and criteria",
        citation="Winkelmes, Bernacki, Butler, Zochowski, Golanics & Harriss "
                 "Weavil (2016), A teaching intervention that increases "
                 "underserved college students' success, Peer Review 18(1/2); "
                 "Tapp, Felten, Winkelmes, Boye & Finley, eds. (2023), "
                 "Transparent Design in Higher Education Teaching and "
                 "Leadership, Routledge; Brookhart (2013), How to Create and "
                 "Use Rubrics, ASCD",
        url="https://doi.org/10.4324/9781003448396",
    ),
    "retrieval": Source(
        key="retrieval",
        title="Retrieval practice (the testing effect)",
        citation="Roediger & Karpicke (2006), Test-enhanced learning, "
                 "Psychological Science 17(3):249-255; Agarwal, Nunes & Blunt "
                 "(2021), Retrieval practice consistently benefits student "
                 "learning, Educational Psychology Review 33:1409-1453",
        url="https://doi.org/10.1007/s10648-021-09595-9",
    ),
    "signaling": Source(
        key="signaling",
        title="Signaling and advance organizers in teaching material",
        citation="Mayer (2009/2021), Multimedia Learning, signaling principle; "
                 "Schneider, Beege, Nebel & Rey (2018), A meta-analysis of how "
                 "signaling affects learning with media, Educational Research "
                 "Review 23:1-24; Ausubel (1960), advance organizers",
        url="https://doi.org/10.1016/j.edurev.2017.11.001",
    ),
    "evaljudge": Source(
        key="evaljudge",
        title="Evaluative judgement in the age of generative AI",
        citation="Bearman, Tai, Dawson, Boud & Ajjawi (2024), Developing "
                 "evaluative judgement for a time of generative artificial "
                 "intelligence, Assessment & Evaluation in Higher Education "
                 "49(6):893-905",
        url="https://doi.org/10.1080/02602938.2024.2335321",
    ),
    "aias": Source(
        key="aias",
        title="The AI Assessment Scale (AIAS)",
        citation="Perkins, Furze, Roe & MacVaugh (2024), The Artificial "
                 "Intelligence Assessment Scale (AIAS): a framework for ethical "
                 "integration of generative AI in educational assessment, "
                 "Journal of University Teaching and Learning Practice 21(6)",
        url="https://doi.org/10.53761/q3azde36",
    ),
    "redesign": Source(
        key="redesign",
        title="Redesigning assessment for the generative-AI era",
        citation="Khlaif, Alkouk, Salama & Abu Eideh (2025), Redesigning "
                 "Assessments for AI-Enhanced Learning, Education Sciences "
                 "15(2):174",
        url="https://doi.org/10.3390/educsci15020174",
    ),
    "ai-resist": Source(
        key="ai-resist",
        title="AI-resistant assessment design",
        citation="Awadallah Alkouk & Khlaif (2024), AI-resistant assessments in "
                 "higher education: practical insights from faculty training "
                 "workshops, Frontiers in Education 9:1499495",
        url="https://doi.org/10.3389/feduc.2024.1499495",
    ),
    "wicked": Source(
        key="wicked",
        title="The wicked problem of AI and assessment",
        citation="Corbin, Bearman, Boud & Dawson (2025), The wicked problem of "
                 "AI and assessment, Assessment & Evaluation in Higher "
                 "Education 51(4):736-752",
        url="https://doi.org/10.1080/02602938.2025.2553340",
    ),
    "ai-limits": Source(
        key="ai-limits",
        title="Model performance across Bloom levels",
        citation="Herrmann-Werner, Festl-Wietek, Holderried, Herschbach, "
                 "Griewatz & Masters (2024), Assessing ChatGPT's mastery of "
                 "Bloom's taxonomy using psychosomatic medicine exam questions, "
                 "Journal of Medical Internet Research 26:e52113",
        url="https://doi.org/10.2196/52113",
    ),
    "benchmarks": Source(
        key="benchmarks",
        title="Bloom levels of questions in current examinations",
        citation="Crowe, Dirks & Wenderoth (2008), Biology in Bloom: "
                 "implementing Bloom's taxonomy to enhance student learning in "
                 "biology, CBE Life Sciences Education 7(4):368-381, which "
                 "ranked ~600 questions from college science exams; alongside "
                 "the higher-order floors set in medical-education assessment "
                 "guidelines",
        url="https://doi.org/10.1187/cbe.08-05-0024",
    ),
    "udl": Source(
        key="udl",
        title="Accessibility and accommodation statements",
        citation="CAST, Universal Design for Learning Guidelines 3.0 (2024); "
                 "Boysen (2024), A critical analysis of the research evidence "
                 "behind CAST's universal design for learning guidelines, "
                 "Policy Futures in Education 22(7):1219-1238",
        url="https://doi.org/10.1177/14782103241255428",
    ),
}


RULE_SOURCES: dict[str, tuple[str, ...]] = {
    "sy1": ("bloom",),
    "sy2": ("transparent",),
    "sy3": ("focus-groups", "evaljudge"),
    "sy4": ("udl",),
    "sy5": ("focus-groups",),
    "sy6": ("focus-groups",),
    "s1": ("focus-groups", "evaljudge"),
    "s2": ("signaling",),
    "s3": ("retrieval",),
    "s4": ("focus-groups", "evaljudge"),
    "a1": ("ai-resist", "redesign", "bloom", "benchmarks", "ai-limits", "wicked"),
    "a2": ("redesign", "aias", "wicked"),
    "a3": ("focus-groups",),
    "a4": ("focus-groups",),
    "a5": ("focus-groups",),
    "a6": ("focus-groups",),
    "sya1": ("focus-groups",),
    "sya2": ("focus-groups",),
    "r1": ("transparent",),
    "r2": ("ai-resist", "redesign", "wicked", "evaljudge"),
    "r3": ("bloom",),
}


# Reference number per source, from ORDER. Cited on the rules list and linked
# to the matching entry on the evidence page.
NUMBERS: dict[str, int] = {key: i for i, key in enumerate(ORDER, start=1)}


def sources_for(code: str | None) -> list[Source]:
    """The sources behind a default rule.

    Args:
        code: The rule's stable code.

    Returns:
        Its sources in reference-number order; empty for a custom rule, which
        the professor wrote, or for a code with no recorded provenance.
    """
    keys = RULE_SOURCES.get(code or "", ())
    return sorted(
        (SOURCES[k] for k in keys if k in SOURCES),
        key=lambda s: NUMBERS[s.key],
    )


def numbered_sources() -> list[tuple[int, Source]]:
    """Every source with its reference number, for the evidence page.

    Returns:
        The (number, source) pairs in ORDER, each source carrying the codes of
        the rules that cite it.
    """
    out = []
    for key in ORDER:
        source = SOURCES[key]
        rules = tuple(
            code for code, keys in RULE_SOURCES.items() if key in keys
        )
        out.append((NUMBERS[key], Source(**{**source.__dict__, "rules": rules})))
    return out
