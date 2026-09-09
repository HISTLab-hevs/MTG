# Usage guide

This document describes how an educator uses MTG to review a course. It follows
the order of the work itself: create the course, upload its materials, settle
the rules that apply, run the analysis, and read the resulting report.

The guide assumes an installed and configured system. See the
[installation guide](installation.md) for setting one up.

## 1. Roles and accounts

MTG has two roles with disjoint responsibilities.

**Administrators** create, list, enable, disable and delete educator accounts.
Deletion requires confirmation and permanently removes the account and all its
courses, documents and reports. Disable an account instead to preserve its data.
Administrator accounts are not shown in the list. Administrators cannot enter
course workspaces or view their content.

**Educators** own course workspaces. They upload and classify materials,
configure rules, run reviews and work with the reports.

There is no self-registration. The first administrator is created during
installation. Educators are created from the administration page; additional
administrators are created with the management CLI.

## 2. Creating a course

A course is the unit of review, because a course's documents only make sense
together: the syllabus states the intent that the slides teach and the
assessments examine. A course carries a title and a short description, both of
which the report generator uses as context.

Each course opens on four pages.

| Page | Purpose |
| --- | --- |
| Overview | Course details, document readiness, and the state of the most recent review. |
| Material | Uploads, versions, processing status, and the materials agent. |
| Rules | The active rule set, plus the creation and editing of custom rules. |
| Analysis | Starting a review, reading its report, its history, and comparisons. |

## 3. Uploading materials

Materials are PDF files. Each upload is classified with one or more of four
document types.

| Type | Typical artifact |
| --- | --- |
| Syllabus | Course outline, learning objectives, grading scheme, policies. |
| Slides | Lecture decks and other presentation material. |
| Assessment | Examinations, assignments, project briefs, graded tasks. |
| Rubric | Marking criteria and performance-level descriptors. |

The classification is not cosmetic: it decides which rules apply to the
document. An examination that also contains its own marking criteria carries
both the assessment and the rubric type, and is then checked against both
families of rules. Choosing types accurately is the single most consequential
input an educator gives the system.

### Processing

An upload advances through a fixed sequence of states, visible in the interface
as it happens: `uploaded`, `rendering`, `extracting`, `embedding`, `ready`. A
failure records `failed` together with the stage that failed and its reason.

Each state corresponds to real work. Rendering converts every page into an
image, at the resolution set by `PDF_RENDER_DPI`. Extraction transcribes each
page image into Markdown through the vision model, preserving headings, lists
and tables. Embedding indexes the resulting text so the materials agent can
retrieve it. A version is not usable for analysis until it reaches `ready`.

### Versions and reuse

Re-uploading a file under the same name creates a new version, and the previous
one is preserved, so earlier reviews remain interpretable against the material
they actually examined.

Two forms of reuse keep the cost of iteration low. A re-uploaded file whose
bytes are identical to any stored version in the same course is recognised and
skipped, even under a different filename. Within a changed file, each page
image is hashed, and a page whose image is unchanged reuses its existing
transcription, with no second call to the model. Revising three slides in a
hundred-page deck therefore sends three pages for transcription, not a hundred.

## 4. Working with rules

A rule states one expectation about course material. Every course starts from
the same default rule set, and educators extend it with rules of their own.

### The default set

The system ships 22 default rules, seeded into every new course: 6 for the
syllabus, 4 for slides, 6 for assessments, 3 for rubrics, 2 applying to both
syllabus and assessments, and 1 applying to all four types. They span five
improvement areas: learning outcomes, grading & feedback, AI-readiness,
content, and clarity.

Each rule is presented through four elements: the requirement in plain language,
the rationale explaining why it matters, the document types it applies to, and
whether it is active. Reading and using the rule set requires no knowledge of
the formal language underneath it.

Every rule also carries a short stable code, such as `sy1`, `s3` or `a2`, which
identifies it across runs and reports. The code is what allows two reviews to be
compared: it survives edits to the rule's wording.

Rules can be deactivated individually. A deactivated rule is not evaluated and
does not affect any score.

### Adding a custom rule

An educator writes a custom rule as a sentence in plain language, for example:

> Every assignment brief must state how many hours of work it is expected to
> take.

A background job compiles the request into two coupled parts: a formal condition
that decides the verdict, and an extraction contract declaring the typed facts
that condition needs, each with an instruction for the model that reads the
pages. The compiled candidate is validated automatically, and a rejected
candidate is repaired and retried a limited number of times.

The result is a **proposal**, not an active rule. The educator sees the
requirement, its rationale, its scope and severity, and decides whether it
belongs in the course. Accepting it admits it to the active set; rejecting it
discards it. This decision is deliberately not automated, because automated
validation checks syntax and contract consistency, not that it is pedagogically
appropriate for a given course.

When compilation cannot produce a valid rule within its attempt budget, the
candidate is left in a visible failed state and the educator is asked to
rephrase the request. Requests that ask for a judgement about a single page tend
to compile more reliably than requests that combine several unrelated
conditions.

Accepted custom rules stay editable afterwards, including their formal
condition. When a hand-edited condition reads a fact the contract does not
declare, the system rejects the save and offers to draft the missing
declarations for review. Validation catches common mistakes; it does not prove
that every expression will evaluate successfully on every document.

For the full description of the rule language, its aggregation operators and the
prompt library behind compilation, see the
[hybrid prompt-rule framework](prompt_rule_framework.md).

## 5. Running a review

Open **Analysis** and choose **Run analysis**. The review runs in the
background. The action requires at least one ready document and is disabled
while a review is running or when the analysed inputs have not changed since
the last report.

For each ready document, the engine selects the active rules whose scope
overlaps the document's assigned types, merges the facts those rules declare
into one request per page, and reads the pages once for all of them. The
per-page observations are then combined into a single document-level value per
fact, using each fact's declared aggregation operator, and each rule's
condition is evaluated over the result. The model supplies the facts; the
engine computes the verdict from them. Incorrect extraction can therefore
affect the result.

Verdicts are stored against the immutable document version. Re-running a course
therefore re-analyses only what has changed: documents that are new, documents
that were re-uploaded, and documents whose applicable rules were edited.
Everything else is reused.

### Reading a verdict

A check ends in one of four states.

| Outcome | Meaning |
| --- | --- |
| Passed | The document satisfies the requirement. |
| Failed | It does not. The finding carries the evidence behind it. |
| Not applicable | The situation the rule checks for never arises in this document. |
| Failed to evaluate | A technical failure prevented a verdict from being computed. |

The third state deserves attention. A rule such as "if the syllabus discusses
using AI, it must also teach students to judge what AI produces" is conditional.
When a syllabus never mentions AI, the condition is technically satisfied, but
reporting that as a pass would credit the course for something it does not do.
Rules declare which of their facts are preconditions, and a rule whose
preconditions never occur is reported as not applicable and excluded from the
score.

The fourth state is kept separate from the verdicts on purpose: a technical
failure is not a pedagogical finding, and is never counted as one.

## 6. Reading the report

The report presents the results of the review, and its parts do not all have the
same status.

**Computed and fixed.** A health score, expressed as the severity-weighted
share of passing checks; an AI-readiness score computed the same way over the
AI-readiness rules alone; a grid of documents against improvement areas; a
ranked list of what to address first; and a comparison between two selected
reviews. Because these are computed from the verdicts, the same verdicts always
give the same numbers.

**Written and fixed.** For each failing check, an explanation of the finding.
The model is instructed to explain findings the engine has already established.
The response structure and fallback text ensure coverage of stored failures;
they do not guarantee that every explanation is accurate.

**Written and editable.** Redesign suggestions, at document level and course
level. These are the only parts of a report an educator changes, by hand or
through the report agent, with one-step undo.

One judgement stands apart. Constructive alignment, meaning whether the stated
objectives are actually taught by the material and exercised by the graded work,
is a direct model judgement over the syllabus and the document summaries, not a
rule verdict. The report presents it as such.

Findings show extracted values and available page-level quotations so they can
be checked against the document. A missing requirement may have no passage to
quote. Quotations are model-produced and should be verified against the PDF.

### History and comparison

Every completed review is kept until explicitly deleted. Two runs of the same
course can be compared, which sorts the checks into resolved, newly failing,
and still failing. The comparison pairs checks by their rule code and guards
against a code whose definition changed between the runs, so a rule that was
rewritten is not silently compared against its earlier self.

Reports are downloadable as PDF for circulation outside the system.

## 7. The two agents

MTG has two conversational surfaces, each limited to a different part of the
system's state.

The **materials agent**, on the Material page, answers questions about the
uploaded pages. It retrieves the pages most similar to the question, pulls in
their neighbours so that content split across a page boundary is not lost, and
answers with citations to filename and page. It is instructed to answer from
the course's own pages and acknowledge missing information. The system checks
citation references, but cannot guarantee that a cited passage supports every
claim.

The **report agent**, within a report opened from Analysis, works from a
completed review. It explains why a check failed or what a suggestion means,
and it revises suggestions on request. Mentioning a document with `@filename`
pulls that document's text into the conversation, truncated if necessary to fit
the context budget. Its authority is bounded: it can edit suggestion blocks and
nothing else. No conversation changes a verdict, a score or a finding.

Both keep their transcript in browser session storage, scoped to user and
course, and summarise it when it grows long. Summaries can omit details.
History is cleared on logout or when the tab closes, and expires after two
hours without an update. **New chat** clears it immediately.

## 8. A typical revision cycle

1. Upload the course materials and classify each one.
2. Review the default rules, deactivate any that do not fit the course, and add
   custom rules for expectations specific to the course or institution.
3. Run the review and read the report, starting from the ranked priorities.
4. Inspect the evidence behind the findings that matter. Findings cannot be
   dismissed in place: if a rule does not fit the course, deactivate it or
   revise the custom rule, then run a new analysis.
5. Revise the course materials, using the suggestions as a starting point rather
   than as instructions.
6. Re-upload the revised documents and run the review again.
7. Compare the two runs to see what was resolved and what remains.

The comparison in step 7 is what makes the cycle worth repeating: it records
the effect of a revision against recorded rule definitions. Changed definitions
are identified separately; new extraction can still affect the observed facts.

## Related documents

- [Installation guide](installation.md), setting up and configuring the system.
- [API and interface reference](api.md), the HTTP surface and command-line tools.
- [Hybrid prompt-rule framework](prompt_rule_framework.md), how rules are
  represented, compiled and evaluated.

---

Copyright 2026 HES-SO Valais-Wallis. This document is licensed under
[CC BY 4.0](../LICENSE-docs).
