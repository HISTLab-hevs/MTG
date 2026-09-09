"""The report assistant: a professor refines the editable parts by chatting.

The generation model is given the report's editable blocks, the course-level
suggestions and each document's suggestions, and answers with a single
constrained-JSON object: a reply for the professor and a list of edits, each
replacing one block's Markdown. Only editable blocks are exposed; the issues and
the deterministic summary are never passed as targets, so the model cannot touch
the grounded verdicts.

A turn is one constrained call on the same guided-JSON path the rest of the app
uses, rather than the native tool calling the vLLM endpoint rejects. The model
first commits to an action, edit or answer, then writes the edits, whose targets
are pinned by enum to the real block ids, and only then the reply, so the reply
can only describe edits that actually exist. If the model declares an edit turn
but produces no usable edit, one corrective retry is made; if that also lands
nothing, the professor gets an honest "nothing was changed" instead of a reply
claiming an update.

The service applies the edits and returns the set of changed targets, so the
router can re-render exactly those blocks.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.client import NO_THINKING, get_report_client
from app.llm.prompts import (
    CHAT_MESSAGE_MAX_CHARS,
    CHARS_PER_TOKEN,
    MENTION_RE,
    TRUNCATION_MARKER,
    UPDATED_MARK_RE,
    char_budget,
    clip_markdown_to_budget,
    loads_model_json,
    render_prompt,
)
from app.models import Course, CourseReport
from app.services.report_view import build_report_view, document_markdown

logger = logging.getLogger(__name__)

# target id of the course-level suggestions block.
_COURSE_TARGET = "course"

# An @-mention of a document in the professor's message. Three accepted forms:
#   @"two words.pdf"   quoted, the only way to reference a filename with spaces;
#   @syllabus.pdf      a filename token with an extension;
#   @syllabus          a bare token, resolved by prefix in _resolve_document so
#                      "@07_rubric" still finds "07_rubric_essay.pdf".
# The bare/extension forms carry a (?<!\w) guard so an email address like
# "foo@bar.com" is not read as a mention; the quoted form needs none and so
# works even attached to a word (dell'@"a b.pdf").
_MENTION = MENTION_RE

_BARE_QUOTED = re.compile(r'"([^"]+)"')

_PROSE_MATCH_MIN = 0.62

_PROSE_MATCH_MARGIN = 0.06

_UPDATED_MARK = UPDATED_MARK_RE

_NO_EDIT_FALLBACK = (
    "I couldn't turn that into an edit, nothing was changed. Tell me which "
    "block to rewrite (or @-mention the document) and I'll apply it."
)


_ECHO_FALLBACK = (
    "The rewrite came out identical to the block's current text, so I didn't "
    "save it, the suggestion seems to already cover this. If you want me to go "
    "further, ask for the next level of detail explicitly (for example: “write "
    "out the actual example for each formula, don't just say to add one”)."
)

def _chat_schema(targets: list[str]) -> dict:
    """The constrained shape for one turn.

    Built per request, so each edit's target is pinned to the report's real
    block ids and the model cannot invent one.

    Property order is deliberate and enforced by the guided-JSON backend: the
    action comes first, so the model must commit to editing or answering before
    writing anything else, and the edits come before the reply, so the reply is
    written once the edits exist and can only describe changes actually
    produced.

    Args:
        targets: The real block ids an edit may name.

    Returns:
        The schema, passed to the model as guided_json.
    """
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["edit", "answer"]},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "enum": targets},
                        "markdown": {"type": "string", "minLength": 1},
                    },
                    "required": ["target", "markdown"],
                    "additionalProperties": False,
                },
            },
            "reply": {"type": "string"},
        },
        "required": ["action", "edits", "reply"],
        "additionalProperties": False,
    }


@dataclass
class ChatResult:
    """One turn's outcome, for the router to render.

    Attributes:
        changed: The blocks actually rewritten, so exactly those are re-rendered.
        context_filename: The document pulled into context, when one was.
        context_truncated: Whether its text had to be clipped to fit.
        context_from_prose: Whether it was only recognised from prose, and so
            scoped but not read.
        unresolved_mentions: Names that matched no document of the course.
        ignored_mentions: Further mentions beyond the one document read.
    """

    reply: str
    changed: set[str]
    context_filename: str | None = None
    context_truncated: bool = False
    context_from_prose: bool = False
    unresolved_mentions: list[str] = field(default_factory=list)
    ignored_mentions: list[str] = field(default_factory=list)


def _editable_blocks(report: CourseReport) -> list[dict]:
    """The blocks the assistant may edit, in display order, with their targets."""
    blocks = [
        {
            "target": _COURSE_TARGET,
            "label": "Course-level suggestions",
            "markdown": report.solutions_md or "",
            "issues": report.summary_md or "",
        }
    ]
    for s in report.sections:
        blocks.append(
            {
                "target": f"section:{s.id}",
                "label": f"Suggestions for {s.filename}",
                "markdown": s.solutions_md or "",
                "issues": s.issues_md or "",
            }
        )
    return blocks


def _findings_context(db: Session, report: CourseReport) -> list[dict]:
    """The complete per-document check matrix behind the report.

    Lets the assistant explain any verdict: what failed, with the rule, why it
    matters and the source quote, what passed, and what never applied. Without
    the passing and not-applicable checks the model can only guess at questions
    about what passed. Quotes and rationale ride only on failures, to keep the
    context bounded. Read-only: the chat never re-judges these verdicts.

    Args:
        db: Session used to rebuild the report view.
        report: Report whose run to read.

    Returns:
        One entry per document, each carrying its checks.
    """
    run = report.report_run
    course = db.get(Course, run.course_id) if run else None
    if course is None:
        return []
    view = build_report_view(db, course)
    out: list[dict] = []
    for doc in view.documents:
        checks = []
        for v in doc.verdicts:
            if v.outcome not in ("pass", "fail", "na"):
                continue
            entry: dict = {
                # The code lets the professor ask about "s3", the same id the
                # report bullets and the Rules page show.
                "rule": (f"[{v.rule.code}] " if v.rule.code else "") + v.rule.text,
                "outcome": {"na": "not applicable"}.get(v.outcome, v.outcome),
            }
            if v.outcome == "fail":
                entry["why_it_matters"] = v.rule.rationale
                entry["severity"] = v.severity
                entry["quotes"] = [
                    f'"{q["quote"]}" (p.{q.get("page")})'
                    for fct in v.facts
                    for q in fct.quotes
                    if q.get("quote")
                ]
            checks.append(entry)
        if checks:
            out.append({"filename": doc.filename, "checks": checks})
    return out


def _scope_blocks(blocks: list[dict], mention: "MentionContext") -> list[dict]:
    """Every editable block, always.

    Naming a document, with an @ or in prose, says which document to read and
    which block the request most likely means. It does not say the professor may
    not touch anything else in the same breath: asking to fix one document and
    update the syllabus too is an ordinary request, and so is tagging a document
    and then asking for something broader a turn later.

    Hiding the other blocks made both impossible and produced the worst failure
    this chat had, the assistant telling the professor that a document of their
    own course was not in the report. Which blocks an edit belongs to is a
    judgement the model makes from the request, guided by the prompt's hint, not
    something the block list should decide for it.

    Kept as a function rather than inlined because it is the documented place
    where that decision lives, and where a future size-based trim would go.
    """
    return blocks


def _scope_findings(findings: list[dict], mention: "MentionContext") -> list[dict]:
    """Give the mentioned document's checks in full and condense the others.

    The named document keeps its full check list, passes and not-applicables
    included, so questions about what passed stay answerable. The others are
    never dropped, only condensed to their failed rules and each failure's
    severity: that is what keeps a request naming one document and the syllabus
    too answerable, and what stops the assistant from claiming a document is
    absent from the report. Full detail for every document of a large course is
    what this digest exists to avoid.

    Args:
        findings: The full check matrix.
        mention: The resolved document context, if any.

    Returns:
        The scoped findings, or all of them when no document was named.
    """
    if not mention.filename:
        return findings
    scoped = []
    for d in findings:
        if d.get("filename") == mention.filename:
            scoped.append(d)
            continue
        fails = [c for c in d.get("checks", []) if c.get("outcome") == "fail"]
        if fails:
            scoped.append({
                "filename": d.get("filename"),
                "checks": [
                    {
                        "rule": c["rule"],
                        "outcome": "fail",
                        "severity": c.get("severity"),
                    }
                    for c in fails
                ],
                "summarised": True,
            })
    return scoped or findings


def _changed(old: str | None, new: str) -> bool:
    """Whether the new text really differs from the old, ignoring surrounding
    whitespace, so a model echoing the existing text back is not an edit."""
    return (old or "").strip() != (new or "").strip()


def _apply_edit(report: CourseReport, target: str, markdown: str) -> bool:
    """Write new Markdown to one editable block, when it really differs.

    The model sometimes returns an edit repeating the block verbatim. Writing it
    and reporting an update would tell the professor something changed when
    nothing did, so a no-op counts as no edit at all.

    Args:
        report: Report holding the block.
        target: The block to write to.
        markdown: Its new text.

    Returns:
        True only when a real change was written.
    """
    if target == _COURSE_TARGET:
        if not _changed(report.solutions_md, markdown):
            return False
        report.solutions_md_prev = report.solutions_md  # one-step undo
        report.solutions_md = markdown
        return True
    if target.startswith("section:"):
        section_id = target.split(":", 1)[1]
        for s in report.sections:
            if str(s.id) == section_id:
                if not _changed(s.solutions_md, markdown):
                    return False
                s.solutions_md_prev = s.solutions_md  # one-step undo
                s.solutions_md = markdown
                return True
    return False


def _parse(answer: str) -> dict:
    """Decode the constrained-JSON reply.

    Tolerates a code fence and the almost-valid JSON, unescaped inner quotes or
    lone backslashes, that the guided decoder lets through (see
    loads_model_json). A long rewrite quoting the source document is where those
    slips happen, and discarding one would surface a successful edit to the
    professor as "nothing was changed".
    """
    try:
        data = loads_model_json(answer)
        return data if isinstance(data, dict) else {}
    except ValueError:
        logger.warning("Report chat returned unrepairable non-JSON")
        return {}


def _looks_like_edit_request(message: str) -> bool:
    """Guess whether the message asks for an edit, to pick the schema.

    The prompt still explains the distinction, but long rewrite requests are
    more reliable when guided decoding is pinned to an edit shape immediately.
    Deliberately conservative: a question such as "why was this flagged?" must
    remain an answer turn.
    """
    m = message.casefold()
    if any(q in m for q in ("why ", "perché", "perche", "what passed", "cosa passa")):
        return False
    edit_cues = (
        "add", "aggiungi", "aggiungere", "rewrite", "rewrit", "riscrivi",
        "write out", "scrivi", "modifica", "cambia", "correggi",
        "expand", "espandi", "improve", "migliora", "make ", "rendi",
        "more concrete", "più concreto", "piu concreto", "specific",
        "example for each", "esempio per ogni", "per ogni singola",
        "actual example", "edit suggestion", "suggestion",
    )
    return any(cue in m for cue in edit_cues)


def _force_edit_schema(schema: dict) -> dict:
    forced = json.loads(json.dumps(schema))
    forced["properties"]["action"]["enum"] = ["edit"]
    forced["properties"]["edits"]["minItems"] = 1
    return forced


def _mentioned_names(message: str, course: Course | None = None) -> list[str]:
    """The filenames the professor mentioned, in order and de-duplicated.

    Also picks up a plain double-quoted phrase with no @ if, and only if, it is
    an exact match for one of the course's real filenames, case-insensitively
    and with the extension optional: a professor who types the filename without
    remembering the @ syntax still gets it read, while a quoted phrase that is
    not a real filename is never guessed into one. This runs only when no
    @-mention matched, so an explicit one always takes precedence.

    Args:
        message: The professor's message.
        course: Course whose filenames a bare quoted phrase is matched against.

    Returns:
        The mentioned names.
    """
    names: list[str] = []
    for quoted, bare in _MENTION.findall(message):
        name = (quoted or bare).strip()
        if name and name not in names:
            names.append(name)
    if names or course is None:
        return names
    for quoted in _BARE_QUOTED.findall(message):
        name = quoted.strip()
        if name and name not in names and _resolve_document(course, name) is not None:
            names.append(name)
    return names


def _prose_document(course: Course | None, message: str):
    """The one course document the professor named in prose, without an @.

    Writing "the suggestions for assessment 1" names a document as plainly as
    @-mentioning its full filename does, but carries no mention syntax, so
    without this the turn silently keeps the scope of whatever was mentioned
    earlier and the professor is told their document is not in the report.

    Matching is fuzzy rather than rule-based, since professors abbreviate,
    inflect and mistype. Every window of consecutive words in the message is
    compared with each filename stem by similarity ratio, and the best pair wins
    only if it clears _PROSE_MATCH_MIN and beats the runner-up document by
    _PROSE_MATCH_MARGIN.

    Args:
        course: Course whose documents to match against.
        message: The professor's message.

    Returns:
        The named document, or None. A wrong hit would edit the wrong block, so
        an ambiguous message, "the assessment" where there are two, is left to
        the existing scope rather than guessed.
    """
    if course is None:
        return None
    docs = list(course.documents)
    if not docs:
        return None
    words = re.findall(r"\w+", message.casefold())
    if not words:
        return None

    # Numbers the professor typed. Course files come in numbered series, and a
    # series number is an exact identifier, not a shade of similarity: "1" and
    # "2" differ by one character, so a ratio alone would happily match
    # "assessment 1" to "Assessment 2 - Group Presentation". Any number in the
    # message therefore VETOES every document carrying a different one.
    said_numbers = {w for w in words if w.isdigit()}

    best: dict[int, float] = {}   # document id -> its best window score
    by_id = {d.id: d for d in docs}
    for d in docs:
        tokens = _stem_tokens(d.filename)
        stem = " ".join(tokens)
        if not stem:
            continue
        doc_numbers = {t for t in tokens if t.isdigit()}
        if said_numbers and doc_numbers and not (said_numbers & doc_numbers):
            continue  # "slides 3" can never mean "Slides 5 - …"
        # Compare against the stem and, separately, against its non-numeric
        # words: "short essay" should score against "assessment short essay"
        # without the series number dragging the ratio down.
        variants = {stem, " ".join(t for t in tokens if not t.isdigit())}
        # Windows up to the stem's length (plus one for a stray article), so a
        # long filename can still be named by a short phrase.
        span = min(len(words), max(2, len(tokens) + 1))
        for size in range(1, span + 1):
            for i in range(len(words) - size + 1):
                phrase = " ".join(words[i:i + size])
                if len(phrase) < 3:
                    continue
                for variant in variants:
                    if not variant:
                        continue
                    score = SequenceMatcher(None, phrase, variant).ratio()
                    # An exact prefix ("assessment 1", "syllabus") is a
                    # deliberate abbreviation, not a partial coincidence.
                    if variant.startswith(phrase):
                        score = max(score, 0.90)
                    if score > best.get(d.id, 0.0):
                        best[d.id] = score

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked or ranked[0][1] < _PROSE_MATCH_MIN:
        return None
    # Too close to call: the message does not single out one document.
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < _PROSE_MATCH_MARGIN:
        return None
    return by_id[ranked[0][0]]


def _stem_tokens(filename: str) -> list[str]:
    """The meaningful lowercase words of a filename's stem, for prose matching.

    Separators and one or two-letter noise are dropped, so a match rests on
    words the professor would actually type.
    """
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename).casefold()
    return [t for t in re.split(r"[^\w]+", stem) if len(t) > 2 or t.isdigit()]


def _resolve_document(course: Course, name: str):
    """Find the course document a mention refers to.

    Matched case-insensitively, with or without the extension. An exact filename
    match wins, then a unique prefix match, so "@syllabus" finds "syllabus.pdf".

    Args:
        course: Course whose documents to search.
        name: The mentioned name.

    Returns:
        The document, or None when nothing matches unambiguously.
    """
    name = name.casefold()
    docs = list(course.documents)
    for d in docs:
        if d.filename.casefold() == name:
            return d
    stem_hits = [d for d in docs if d.filename.casefold().startswith(name)]
    if len(stem_hits) == 1:
        return stem_hits[0]
    return None


@dataclass
class MentionContext:
    """The document context of one turn.

    Carries the one document whose full text was injected plus any unresolved
    names, so the router can tell the professor what was and was not used.
    """

    filename: str | None = None
    markdown: str = ""
    truncated: bool = False
    unresolved: list[str] = field(default_factory=list)  # names that matched no document
    # Further documents the professor @-mentioned that resolved but were NOT read:
    # the assistant takes one document at a time, so only the first is used and
    # these are surfaced so the professor knows to ask about them separately.
    ignored: list[str] = field(default_factory=list)
    # The editable block target for the mentioned document (e.g. "section:3").
    # A HINT, rendered into the prompt as the block an unscoped edit most likely
    # means, not a restriction: every block stays editable, so the professor can
    # tag one document and still ask for a change to another in the same breath.
    target: str | None = None
    # True when the document was recognised from prose ("dell'assessment 1")
    # rather than an @-mention. It still points the turn at that document, but no
    # full source text was loaded, so the prompt says so and tells the professor
    # how to load it when a rewrite needs the document's real wording.
    from_prose: bool = False


def _edited_labels(history: list[dict]) -> list[str]:
    """The blocks this conversation has really edited, in order.

    Parsed from the deterministic update records the UI appends to an assistant
    turn. "What did you change?" must be answered from facts rather than the
    model's impression: without this list it either denies its own edit or
    claims the whole report's suggestions. Rendered into the prompt as the
    authoritative record, so everything absent from it was not touched in this
    conversation.
    """
    out: list[str] = []
    for turn in history or []:
        if turn.get("role") not in ("assistant", "summary"):
            continue
        for mark in _UPDATED_MARK.findall(str(turn.get("content", ""))):
            for label in mark.rstrip(".").split(", "):
                label = label.strip()
                if label and label not in out:
                    out.append(label)
    return out


def _document_from_history(course: Course, history: list[dict]):
    """The most recently mentioned document in the visible conversation.

    The full document text injected into an earlier prompt is not stored in the
    browser's chat history, so a bare follow-up must reload that document rather
    than rely on memory the model will not actually receive this turn.
    """
    for turn in reversed(history or []):
        if turn.get("role") not in ("user", "summary"):
            continue
        for name in _mentioned_names(str(turn.get("content", "")), course):
            doc = _resolve_document(course, name)
            if doc is not None:
                return doc
    return None


def _build_mention_context(
    db: Session, report: CourseReport, message: str, history: list[dict] | None = None
) -> MentionContext:
    """Resolve the turn's mentions to at most one document and load its text.

    One document at a time keeps the injected text within the model's context:
    the first resolvable mention is taken, its Markdown clipped to a budget
    derived from the real context window, leaving room for the rest of the
    prompt and the answer, and any unmatched name is reported so the professor
    is told rather than silently ignored.

    A message with no mention reloads the most recently mentioned document from
    the visible conversation, which is what lets a bare follow-up stay both
    scoped and grounded.

    Args:
        db: Session used to load the course and its documents.
        report: Report the turn is about.
        message: The professor's message.
        history: Prior turns, read for an earlier mention.

    Returns:
        The document context of this turn.
    """
    run = report.report_run
    course = db.get(Course, run.course_id) if run else None
    names = _mentioned_names(message, course)
    if not names:
        if course is None:
            return MentionContext(unresolved=[])
        # A document named in prose ("dell'assessment 1") wins over the one
        # carried from history: the professor has moved on, and pointing the turn
        # at the stale document is what made the assistant claim the newly named
        # one was not in the report.
        prose = _prose_document(course, message)
        chosen = prose or _document_from_history(course, history or [])
        if chosen is None:
            return MentionContext(unresolved=[])
        return _mention_context_for_document(
            report,
            chosen,
            unresolved=[],
            ignored=[],
            from_prose=prose is not None,
        )
    if course is None:
        return MentionContext(unresolved=names)

    chosen = None
    unresolved: list[str] = []
    ignored: list[str] = []
    for name in names:
        doc = _resolve_document(course, name)
        if doc is None:
            unresolved.append(name)
        elif chosen is None:
            chosen = doc
        else:
            # Resolved, but a document is already chosen: one at a time.
            ignored.append(doc.filename)

    if chosen is None:
        return MentionContext(unresolved=unresolved, ignored=ignored)

    return _mention_context_for_document(
        report, chosen, unresolved=unresolved, ignored=ignored
    )


def _mention_context_for_document(
    report: CourseReport,
    chosen,
    unresolved: list[str],
    ignored: list[str],
    from_prose: bool = False,
) -> MentionContext:
    """Load the chosen document's text and map it to its editable block.

    A document recognised from prose is scoped but not read: the professor did
    not @-mention it, the prompt does not render its source text, and loading it
    would spend the window, and light up the "read the full text" badge, for
    material the model never sees.
    """
    markdown = "" if from_prose else document_markdown(chosen)
    settings = get_settings()
    # Half the remaining window: a simple split that leaves room for the rest of
    # the prompt (findings + blocks + history) while still admitting a sizeable
    # document on a 64k-token model.
    char_budget = int(
        (settings.generation_max_model_len - settings.chat_output_token_budget)
        * CHARS_PER_TOKEN
        * 0.5
    )
    truncated = len(markdown) > char_budget
    if truncated:
        markdown = markdown[:char_budget].rstrip() + TRUNCATION_MARKER
    # The block an unscoped edit on this mention most likely means. A hint for
    # the prompt, not a lock: run_report_chat keeps every block writable.
    target = next(
        (f"section:{s.id}" for s in report.sections if s.document_id == chosen.id),
        None,
    )
    return MentionContext(
        filename=chosen.filename,
        markdown=markdown,
        truncated=truncated,
        unresolved=unresolved,
        ignored=ignored,
        target=target,
        from_prose=from_prose,
    )


def _prompt_char_budget() -> int:
    """Characters the whole chat prompt may use.

    Derived from the model's context window, leaving room for its own answer.
    Mirrors report generation's budgeting so the model is never overrun.
    """
    settings = get_settings()
    return char_budget(settings.generation_max_model_len, settings.chat_output_token_budget)


def _fit_chat_prompt(
    blocks: list[dict],
    findings: list[dict],
    message: str,
    history: list[dict],
    mention: "MentionContext",
) -> str:
    """Render the chat prompt within the model's context budget.

    The parts carrying the request and the grounding, the current message, the
    findings and the editable blocks, are kept intact. The flexible parts are
    shed in order: the oldest history turns first, since long-term memory is
    already handled by client-side compaction and recent turns matter most, and
    only if it still does not fit, the mentioned document's Markdown. In the
    common case nothing is shed and the full prompt is used.

    Args:
        blocks: The editable blocks.
        findings: The scoped check matrix.
        message: The professor's message.
        history: Prior turns, oldest first.
        mention: The turn's document context.

    Returns:
        The rendered prompt.
    """
    budget = _prompt_char_budget()

    def render(hist: list[dict], md: str) -> str:
        return render_prompt(
            "report_chat.j2",
            blocks=blocks,
            findings=findings,
            message=message,
            history=hist,
            # Derived from the FULL history on purpose: even when old turns are
            # shed to fit the window, the record of what was edited survives.
            edited_labels=_edited_labels(history),
            context_filename=mention.filename,
            context_markdown=md,
            context_target=mention.target,
            # Named in prose: it steers the scope, but its source text was not
            # loaded, so the prompt says so instead of promising a full read.
            context_from_prose=mention.from_prose,
        )

    hist = list(history)
    markdown = mention.markdown
    prompt = render(hist, markdown)
    if len(prompt) <= budget:
        return prompt

    # 1) Shed oldest history turns until it fits (or none remain).
    while hist and len(prompt) > budget:
        hist = hist[1:]
        prompt = render(hist, markdown)
    if len(prompt) <= budget:
        return prompt

    # 2) Still over budget: clip the injected document to whatever room is left,
    #    keeping the rest of the prompt (now history-free) intact.
    return clip_markdown_to_budget(lambda md: render(hist, md), markdown, budget)


def run_report_chat(
    db: Session, report: CourseReport, message: str, history: list[dict] | None = None
) -> ChatResult:
    """Run one assistant turn over the report's editable blocks.

    Args:
        db: Session the edits are written through.
        report: Report being refined.
        message: The professor's message.
        history: Prior turns as {role, content}, oldest first, so the
            conversation has memory. Rendered into the prompt rather than sent
            as chat messages, to keep the single guided-JSON call the endpoint
            supports.

    Returns:
        The reply, the set of blocks actually changed, and what document context
        was used.
    """
    if len(message) > CHAT_MESSAGE_MAX_CHARS:
        return ChatResult(
            "That message is too long. The maximum is 16,000 characters. "
            "Please shorten it or split the refinement into separate turns.",
            set(),
        )

    client = get_report_client()
    # A long rewrite runs to a few thousand output tokens, 1–2 minutes here, past
    # the default timeout tuned for short worker calls. One transport retry is
    # enough: the corrective retry below is the second chance that matters.
    client.request_timeout = max(client.request_timeout, 300.0)
    client.max_retries = min(client.max_retries, 1)
    if not client.is_configured:
        return ChatResult(
            "The assistant is unavailable: no generation model is configured.",
            set(),
        )
    all_blocks = _editable_blocks(report)
    all_findings = _findings_context(db, report)
    mention = _build_mention_context(db, report, message, history)
    blocks = _scope_blocks(all_blocks, mention)
    findings = _scope_findings(all_findings, mention)
    prompt = _fit_chat_prompt(blocks, findings, message, history or [], mention)
    # EVERY block stays writable. Naming a document says which one to read and
    # which block the request most likely means; it never says the professor may
    # not ask for something else in the same message ("fix @assessment1.pdf and
    # the syllabus too") or a turn later. Pinning the enum to the mentioned
    # document made those requests impossible to express, and the model, unable
    # to emit the right target, answered that the document was not in the report.
    #
    # Scope is therefore steered by the prompt (context_target names the likely
    # block, and rule 2 tells the model not to edit others "to be safe"), not
    # enforced by the grammar. A wrong-block edit is visible and undoable; a
    # request the grammar cannot express is neither.
    allowed_targets = [b["target"] for b in blocks]
    schema = _chat_schema(allowed_targets)
    edit_intent = _looks_like_edit_request(message)
    first_schema = _force_edit_schema(schema) if edit_intent else schema

    def call(p: str, s: dict) -> tuple[dict, str]:
        answer = client.generate(
            p,
            extra_body={
                **NO_THINKING,
                "guided_json": s,
                # Hard cap on the answer, matching the room the prompt budgeting
                # already reserves for it. Without it, a guided-decoding runaway
                # (no stop token) generates until the context window fills and
                # the wall-clock timeout kills the call, the professor then sees
                # "assistant unavailable" on a turn that would have worked.
                "max_tokens": get_settings().chat_output_token_budget,
            },
        )
        return _parse(answer), answer

    echoed: set[str] = set()  # targets the model rewrote to their identical text

    def apply(data: dict) -> set[str]:
        applied: set[str] = set()
        # `action` is the authoritative intent switch.  The JSON schema cannot
        # express the cross-field invariant on every guided-decoding backend, so
        # enforce it here: an answer turn can never mutate report content even
        # if the model also emitted an `edits` array.
        if data.get("action") != "edit":
            return applied
        # Guards against a target the model invented (or a stale id from an
        # earlier turn), not against a block the professor is "not allowed" to
        # edit: every real block is writable. Kept because the retry path and a
        # repaired JSON answer can both carry a target the grammar never vetted.
        valid_targets = set(allowed_targets)
        # One model turn is one undoable step per block.  Guided JSON permits a
        # target to appear twice; keep only its final proposed value so `_prev`
        # always remains the text from before this turn (Undo once, Redo once).
        edits_by_target: dict[str, dict] = {}
        for edit in (data.get("edits") or []):
            if isinstance(edit, dict) and isinstance(edit.get("target"), str):
                edits_by_target[edit["target"]] = edit
        for edit in edits_by_target.values():
            if not isinstance(edit, dict):
                continue
            target = edit.get("target")
            markdown = edit.get("markdown")
            if target not in valid_targets or not isinstance(markdown, str):
                if isinstance(target, str) and target not in valid_targets:
                    logger.info(
                        "Report chat dropped an edit on out-of-scope target %s "
                        "(allowed: %s)", target, sorted(valid_targets)
                    )
                continue
            if _apply_edit(report, target, markdown):
                applied.add(target)
            else:
                # The model returned this block verbatim (a no-op), typically
                # because the block already describes what was asked for at the
                # level the model understood it.
                echoed.add(target)
                logger.info("Report chat no-op edit on %s (echoed current text)", target)
        return applied

    try:
        data, answer = call(prompt, first_schema)
        changed = apply(data)
        # The model committed to an edit but none landed (empty list or a
        # verbatim echo). Retry showing it its own reply, where the new text
        # often ended up; the forced grammar pins `action` to "edit" and
        # requires a non-empty `edits`.
        if (data.get("action") == "edit" or (edit_intent and not data)) and not changed:
            logger.info(
                "Report chat declared an edit but produced none "
                "(edits=%d, reply=%.200r); retrying with a forced-edit grammar",
                len(data.get("edits") or []),
                data.get("reply", ""),
            )
            nudge = render_prompt("report_chat_retry.j2", reply=data.get("reply", ""))
            retry_schema = _force_edit_schema(schema)
            data, answer = call(prompt + nudge, retry_schema)
            changed = apply(data)
            if not changed:
                # Still nothing: relaying the model's reply could claim an update
                # that never happened. Log the answer so the failure stays
                # diagnosable from the container logs.
                logger.warning(
                    "Report chat edit turn produced no change after the forced "
                    "retry (echoed targets: %s); final answer: %.800s",
                    sorted(echoed) or "none",
                    answer,
                )
                data["reply"] = _ECHO_FALLBACK if echoed else _NO_EDIT_FALLBACK
    except Exception:
        # Endpoint unreachable or overloaded after the client's retries. This
        # runs inline in the web request, so raising here would be a 500 that
        # breaks the chat. Nothing is edited.
        logger.exception("Report chat model call failed")
        return ChatResult(
            "The assistant is temporarily unavailable (the model did not "
            "respond). Please try again in a moment.",
            set(),
            context_filename=mention.filename,
            context_truncated=mention.truncated,
            context_from_prose=mention.from_prose,
            unresolved_mentions=mention.unresolved or [],
            ignored_mentions=mention.ignored or [],
        )
    if changed:
        db.commit()
    reply = str(data.get("reply", "")).strip()
    # Safety net only: if the model returns a verbatim no-op on an "answer" turn,
    # we simply do not report an edit (changed stays empty, so no "Updated" line /
    # live swap) and keep the model's own reply.
    if not reply:
        reply = (
            f"Updated {len(changed)} block(s)." if changed else "No change made."
        )
    return ChatResult(
        reply,
        changed,
        context_filename=mention.filename,
        context_truncated=mention.truncated,
        context_from_prose=mention.from_prose,
        unresolved_mentions=mention.unresolved or [],
        ignored_mentions=mention.ignored or [],
    )
