"""Report history and the diff between two past runs (always the same course).

Each CourseReport stores a self-contained snapshot of what its run saw
(build_snapshot): per document, its version number and every rule's verdict,
plus the frozen definition of every rule that ran. Verdict rows are overwritten
by the next analysis and rules can be edited or deleted afterwards, so the
snapshot is the only thing that keeps an old run comparable.

The diff (diff_reports) is computed purely from two snapshots, deterministic and
with no model call. It answers what a professor asks when comparing runs: which
documents were added, removed, edited or left unchanged; which checks were newly
resolved, newly broken, or stayed failing; which rules were added, removed or
changed; and how the headline scores moved.

Rule identity across runs is the rule code (see app/services/rules.
next_rule_code), stable under text edits unlike the text itself. A code alone is
not proof of identity, though, since deleting a rule frees its number for a
later, different one. Two verdicts therefore count as the same check only when
their code's frozen text and CEL match on both sides; a code whose definition
differs is reported as a changed rule, and its verdicts never masquerade as
resolved or broken transitions.
"""

import json
from dataclasses import dataclass, field

from app.services.report_metrics import parse_metrics
from app.services.report_view import ReportView


def build_snapshot(view: ReportView, fingerprint: str | None = None) -> str:
    """Freeze a run's verdicts into the JSON stored on its CourseReport.

    Besides the per-document verdicts, the snapshot records the frozen
    definition of every rule that ran, the exact rule set active when the job
    was launched, keyed by the rule's stable code. Later edits or deletions of
    the live rules cannot touch it, which is what makes two runs honestly
    comparable and rule changes between them detectable.

    Args:
        view: Report view holding the run's verdicts.
        fingerprint: The run's course-input fingerprint (see
            analysis.course_input_fingerprint), stored so a later request can
            tell whether anything analysable has changed since, without a schema
            change. Snapshots written before this existed carry none, and the
            "has anything changed?" check treats that as "cannot tell, allow the
            run".

    Returns:
        The snapshot JSON.
    """
    docs = []
    rules: dict[str, dict] = {}
    for doc in view.documents:
        if not doc.verdicts:
            continue
        for v in doc.verdicts:
            key = v.rule.code or v.rule.text
            rules.setdefault(
                key,
                {
                    "code": v.rule.code,
                    "text": v.rule.text,
                    "rationale": v.rule.rationale,
                    "cel": v.rule.cel,
                    "extraction_contract": v.rule.extraction_contract,
                    "severity": v.rule.severity,
                    "category": v.rule.category,
                    "document_type": v.rule.document_type,
                },
            )
        docs.append(
            {
                "document_id": getattr(doc, "document_id", None),
                "document_version_id": getattr(doc, "document_version_id", None),
                "version_number": getattr(doc, "version_number", None),
                "filename": doc.filename,
                "document_type": doc.document_type,
                "verdicts": [
                    {
                        "code": v.rule.code,
                        "rule": v.rule.text,
                        "category": v.rule.category,
                        "severity": v.severity,
                        "outcome": v.outcome,
                        "facts": [
                            {
                                "field": getattr(f, "field", None),
                                "value": getattr(f, "value", None),
                                "gate": bool(getattr(f, "gate", False)),
                                "quotes": list(getattr(f, "quotes", []) or []),
                            }
                            for f in v.facts
                        ],
                    }
                    for v in doc.verdicts
                    if v.outcome in ("pass", "fail")
                ],
            }
        )
    payload = {
        "documents": docs,
        "rules": sorted(rules.values(), key=lambda r: r["code"] or ""),
    }
    if fingerprint:
        payload["input_fingerprint"] = fingerprint
    return json.dumps(payload)


def snapshot_fingerprint(snapshot_json: str | None) -> str | None:
    """The course-input fingerprint frozen on a report, when it carries one."""
    data = _load(snapshot_json)
    value = data.get("input_fingerprint")
    return value if isinstance(value, str) and value else None


def _load(snapshot_json: str | None) -> dict:
    """Decode a stored snapshot, falling back to an empty one when unreadable."""
    if not snapshot_json:
        return {"documents": []}
    try:
        data = json.loads(snapshot_json)
    except (ValueError, TypeError):
        return {"documents": []}
    return data if isinstance(data, dict) else {"documents": []}


@dataclass
class VerdictChange:
    """How one check on one document moved between the two runs.

    Attributes:
        change: One of "resolved", "broken" or "still_failing".
    """

    filename: str
    rule: str
    severity: str
    change: str
    code: str | None = None


@dataclass
class DocChange:
    """How one document differs between the two runs.

    Attributes:
        change: One of "added", "removed", "edited" for a new version, or
            "unchanged".
    """

    filename: str
    document_type: str
    change: str


@dataclass
class RuleChange:
    """A rule whose presence or definition differs between the two runs.

    Attributes:
        change: One of "added", "removed" or "changed".
        text: The newer run's text, or the older one's when the rule was
            removed.
        text_before: The older text, set only on a change that moved it.
    """

    code: str
    change: str
    text: str
    text_before: str | None = None


@dataclass
class ReportDiff:
    """Everything the compare view needs, from both reports' snapshots.

    A score can be absent on one side and present on the other: AI-readiness is
    None until the run has an AI rule to score, so the first run that gains one
    goes from no score to a number. That is the movement the professor wants to
    see, so it counts as the full value gained or lost rather than as no change.
    _delta_from treats a missing side as zero, and only two missing sides mean
    there is nothing to say.

    Attributes:
        rules: Rule-set movement between the runs: rules that exist on one side
            only, and rules whose frozen definition changed, including a deleted
            rule replaced by a different one that reused its code.
    """

    resolved: list[VerdictChange] = field(default_factory=list)
    broken: list[VerdictChange] = field(default_factory=list)
    still_failing: list[VerdictChange] = field(default_factory=list)
    docs: list[DocChange] = field(default_factory=list)
    rules: list[RuleChange] = field(default_factory=list)
    score_before: int | None = None
    score_after: int | None = None
    ai_before: int | None = None
    ai_after: int | None = None

    @staticmethod
    def _delta_from(before: int | None, after: int | None) -> int | None:
        """Movement between two scores, counting a missing side as zero."""
        if before is None and after is None:
            return None
        return (after or 0) - (before or 0)

    @property
    def score_delta(self) -> int | None:
        """How the health score moved, or None when neither run has one."""
        return self._delta_from(self.score_before, self.score_after)

    @property
    def ai_delta(self) -> int | None:
        """How AI-readiness moved, or None when neither run scored it."""
        return self._delta_from(self.ai_before, self.ai_after)

    @property
    def score_is_new(self) -> bool:
        """Whether only the newer run has a health score, so nothing compares."""
        return self.score_before is None and self.score_after is not None

    @property
    def ai_is_new(self) -> bool:
        """Whether AI-readiness was unscored before this run.

        The delta is then a gain from nothing, not an improvement on an earlier
        number.
        """
        return self.ai_before is None and self.ai_after is not None


def _verdict_map(snapshot: dict, by_code: bool) -> dict[tuple[str, str], dict]:
    """Index a snapshot's verdicts for pairing against the other run's.

    Args:
        snapshot: Decoded snapshot.
        by_code: Key rules by their stable code; snapshots from before codes
            existed are keyed by rule text instead.

    Returns:
        The verdicts keyed by (filename, rule key).
    """
    out: dict[tuple[str, str], dict] = {}
    for doc in snapshot.get("documents", []):
        for v in doc.get("verdicts", []):
            key = (v.get("code") if by_code else None) or v["rule"]
            out[(doc["filename"], key)] = {**v, "filename": doc["filename"]}
    return out


def _has_codes(snapshot: dict) -> bool:
    """Whether a snapshot's verdicts carry rule codes at all."""
    return any(
        v.get("code")
        for doc in snapshot.get("documents", [])
        for v in doc.get("verdicts", [])
    )


def _rule_map(snapshot: dict) -> dict[str, dict]:
    """The snapshot's frozen rule definitions, keyed by code."""
    return {
        r["code"]: r
        for r in snapshot.get("rules", [])
        if isinstance(r, dict) and r.get("code")
    }


def _diff_rules(before: dict, after: dict, diff: "ReportDiff") -> set[str]:
    """Record how the rule set moved between the runs.

    A code present on both sides with a different frozen text or CEL is not the
    same check: it is either an edit or a deleted rule whose number a new rule
    reused, so its verdicts must not be read as transitions.

    Args:
        before: The older run's decoded snapshot.
        after: The newer run's.
        diff: The diff being built; its rules list is filled in place.

    Returns:
        The codes whose definition changed, to be excluded from the verdict
        transitions.
    """
    rb, ra = _rule_map(before), _rule_map(after)
    changed: set[str] = set()
    for code in sorted(rb.keys() | ra.keys()):
        old_r, new_r = rb.get(code), ra.get(code)
        if old_r is None:
            diff.rules.append(RuleChange(code, "added", new_r["text"]))
        elif new_r is None:
            diff.rules.append(RuleChange(code, "removed", old_r["text"]))
        elif (old_r["text"], old_r.get("cel")) != (new_r["text"], new_r.get("cel")):
            changed.add(code)
            diff.rules.append(
                RuleChange(
                    code,
                    "changed",
                    new_r["text"],
                    text_before=old_r["text"] if old_r["text"] != new_r["text"] else None,
                )
            )
    return changed


def diff_reports(older, newer) -> ReportDiff:
    """Diff two of a course's reports, from their stored snapshots.

    Only the reports' snapshot_json and metrics_json are read, so the comparison
    stays stable even after later runs.

    Args:
        older: The report to treat as the "before".
        newer: The report to treat as the "after".

    Returns:
        The diff, ready for the compare view.
    """
    before = _load(older.snapshot_json)
    after = _load(newer.snapshot_json)
    diff = ReportDiff()
    # Verdicts pair by code only when both snapshots have codes; a comparison
    # against a pre-code report falls back to pairing by rule text.
    by_code = _has_codes(before) and _has_codes(after)
    changed_codes = _diff_rules(before, after, diff) if by_code else set()

    # Score movement, from the persisted metrics.
    mb, ma = parse_metrics(older.metrics_json), parse_metrics(newer.metrics_json)
    if mb:
        diff.score_before = mb.get("health_score")
        diff.ai_before = mb.get("ai_readiness_score")
    if ma:
        diff.score_after = ma.get("health_score")
        diff.ai_after = ma.get("ai_readiness_score")

    # Document set changes.
    before_docs = {d["filename"]: d for d in before.get("documents", [])}
    after_docs = {d["filename"]: d for d in after.get("documents", [])}
    for name in sorted(before_docs.keys() | after_docs.keys()):
        if name in before_docs and name not in after_docs:
            change = "removed"
            dtype = before_docs[name]["document_type"]
        elif name in after_docs and name not in before_docs:
            change = "added"
            dtype = after_docs[name]["document_type"]
        else:
            old_doc = before_docs[name]
            new_doc = after_docs[name]
            old_version = old_doc.get("document_version_id")
            new_version = new_doc.get("document_version_id")
            if (
                old_version is not None
                and new_version is not None
                and old_version != new_version
            ):
                change = "edited"
            else:
                change = "unchanged"
            dtype = new_doc["document_type"]
        diff.docs.append(DocChange(filename=name, document_type=dtype, change=change))

    # Per-(document, rule) verdict transitions.
    vb, va = _verdict_map(before, by_code), _verdict_map(after, by_code)
    for key in sorted(vb.keys() | va.keys()):
        old_v, new_v = vb.get(key), va.get(key)
        if old_v is None or new_v is None:
            continue  # rule/document only on one side: covered by doc/rule changes
        if key[1] in changed_codes:
            # Same code, different frozen definition: not the same check, so a
            # flipped outcome proves nothing. Reported once in diff.rules.
            continue
        code = new_v.get("code")
        text = new_v["rule"]
        if old_v["outcome"] == new_v["outcome"]:
            if new_v["outcome"] == "fail":
                diff.still_failing.append(
                    VerdictChange(key[0], text, new_v["severity"], "still_failing", code)
                )
            continue
        if old_v["outcome"] == "fail" and new_v["outcome"] == "pass":
            diff.resolved.append(
                VerdictChange(key[0], text, new_v["severity"], "resolved", code)
            )
        elif old_v["outcome"] == "pass" and new_v["outcome"] == "fail":
            diff.broken.append(
                VerdictChange(key[0], text, new_v["severity"], "broken", code)
            )
    return diff
