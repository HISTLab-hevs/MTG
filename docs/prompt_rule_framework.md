# The hybrid prompt-rule framework

This document describes the representation MTG uses to state a pedagogical
expectation and decide whether a course document meets it. It covers the rule
language and its aggregation operators, the prompt library that compiles and
applies rules, the validation a compiled rule must pass, and the evidence base
behind the default rule set.

It is written as a reference implementation for researchers building
neurosymbolic educational tools, and as the specification an educator or
developer needs in order to read, audit or extend the rules the system ships
with.

## 1. The problem the framework addresses

Asking a language model directly whether a syllabus satisfies a pedagogical
requirement produces an answer without a decision procedure. The same document
can receive different verdicts on different days, the reasoning behind a verdict
is not inspectable, and there is no way to distinguish a claim the document
supports from one the model supplied.

Course documents nevertheless demand interpretation. They are visual, they vary
enormously in wording and structure, and a requirement such as "at least one
learning objective asks students to analyse, evaluate or create something"
cannot be decided by pattern matching.

The framework separates these two operations and assigns each to the mechanism
suited to it. A model interprets pages and reports what it observes. A
deterministic engine combines those observations and computes the verdict. The
line between the two is drawn explicitly, in a declaration attached to every
rule, where it can be read.

The resulting property is the one the system is built around: **verdicts are
deterministic, prose is generated.** The engine decides what passes and fails
from the extracted facts and their source quotations; the model explains those
findings and proposes fixes. For identical extracted facts and rule
definitions, verdicts and computed metrics are identical. Fresh extraction can
produce different observations and therefore different verdicts; deterministic
evaluation does not make the model interpretation reproducible or infallible.

## 2. Anatomy of a rule

A rule has four parts, plus provenance and status metadata.

| Part | Role |
| --- | --- |
| Requirement | The expectation in plain language, with the document types it applies to. This is what an educator reads and decides about. |
| Rationale | One sentence on why the requirement matters. Shown to the educator, and used by the report to justify a finding. |
| Extraction contract | The typed facts the rule needs from a document, each with an instruction for the model that reads the pages, and an operator saying how per-page observations combine. |
| Formal condition | A boolean expression over the combined facts. Its truth value is the verdict. |

The formal condition is written in the [Common Expression
Language](https://github.com/google/cel-spec) (CEL), a language in which a
program is a single expression. The host application declares the variables the
expression may read. MTG parses the expression and checks its contract and
selected operator usages before evaluation; it does not perform complete static
type checking. Evaluation happens through
[cel-python](https://github.com/cloud-custodian/cel-python) and calls no model.

The choice of CEL follows from what the evaluation actually requires. Each
verdict is a boolean function of a fixed set of typed facts about one document;
there is no search, no unification and no recursive inference to perform. CEL
provides a small, embeddable expression language for that evaluation. The
application further limits field references and string functions through its
validators; runtime errors remain possible.

### Example

Rule `a2`, from the default set, requires that a graded task states an explicit
AI-use policy.

```yaml
document_type: assessment
code: a2
severity: high
category: ai_readiness
text: >-
  The assessment must state an explicit, specific policy on what AI use is
  permitted, limited, or prohibited, and for which parts of the task, not a
  vague mention or a blanket statement.
rationale: >-
  A vague nod to AI leaves students guessing and is hard to enforce; a specific
  policy that says which tools and which parts of the task are covered is
  fairer, more defensible in an integrity case, and has been associated with
  fewer misconduct cases where it has been adopted.
cel: "!doc.has_assessment_task || doc.ai_policy_level >= 2"
contract:
  - name: has_assessment_task
    gate: true
    type: boolean
    agg: any
    instruction: >-
      True if THIS page sets work that is graded or handed in (a homework,
      assignment, problem to solve, exercise, or task with a deliverable);
      false for purely explanatory or illustrative content.
  - name: ai_policy_level
    type: number
    agg: max
    instruction: >-
      Rate how clearly THIS page states an AI-use policy for the work: 0 if AI
      use is not addressed at all; 1 if AI is mentioned only vaguely (e.g.
      "don't misuse AI") without saying what is allowed; 2 ONLY if it states
      explicitly and specifically what AI use is permitted, limited, or
      prohibited and for which parts of the task.
```

Three properties of this example generalise to the whole framework.

The division of labour is visible. Deciding whether a page states a specific
policy or only gestures at one is a judgement, and it lives in an instruction.
Deciding that "at least one page reaches level 2" satisfies the rule is
arithmetic, and it lives in the expression.

Judgement is graduated where the requirement is graduated. A boolean
"mentions AI" would collapse the distinction the rule exists to draw, so the
observation is a three-level scale and the threshold sits in the expression,
where it can be read and argued with.

The rule is conditional, and says so. `has_assessment_task` is marked as a
gate: it is the precondition, not the substance. On a lecture slide that sets no
graded work, the condition is satisfied vacuously, and the report says
*not applicable*, and the document is never credited with a policy it never
needed.

### Rule identity

Every rule carries a short stable code: `sy1`, `s3`, `a2`, `r1` for the four
document types, `sya1` for a rule spanning two, `all1` for one spanning all
four. The code is the rule's identity across time. It is frozen into each
report, so two reviews can be compared even after a rule's wording has been
edited, and an existing rule is never renumbered.

## 3. Facts and their aggregation

A fact is observed on a single page and then combined across pages. The contract
declares both halves.

A page-level observation is boolean, numeric, a string, or a list of strings.
The aggregation operator determines what the document-level value means, and for
the positional operators it also changes its type.

| Operator | Page type | Document type | Meaning |
| --- | --- | --- | --- |
| `any` | boolean | boolean | At least one page satisfies the fact. |
| `all` | boolean | boolean | Every relevant page satisfies it; false if no page is relevant. |
| `sum` | boolean or number | number | Count of true observations, or arithmetic total. |
| `max`, `min` | number | number | Highest or lowest observed value. |
| `union` | string list | string list | Deduplicated union across pages. |
| `first` | number or string | number or string | First non-default value encountered. |
| `pages` | boolean, number, string list | list of page numbers | Ordered pages where the observation is present, position preserved. |
| `tagged_pages` | string list | list of `{item, page}` | Item-page pairs, page-ordered, with variant spellings of the same item reconciled. |

The operator is a semantic commitment, not a formatting detail. "At least one
assignment names an AI policy" and "every assessment explains permitted use" are
different requirements, and they differ only in this choice. Getting it wrong
produces a rule that runs cleanly and checks the wrong thing, which is why the
operator is declared explicitly and validated against the fact's type.

The two positional operators exist because some requirements are about
arrangement, and presence alone does not settle them. A rule stating that a
formula must be followed by a worked example within the next page cannot be
decided from the fact that both appear somewhere: `pages` keeps the ordered
page numbers so the expression can compare them. `tagged_pages` goes further
and keeps which item appeared where, so a rule can require that each item
introduced is later exercised. The reconciliation of spellings is deterministic
and refuses to match two tags carrying different numbers, since "first law" and
"second law" name different things however similarly they read. Matching is
heuristic: similar spelling does not establish semantic identity.

### The engine-supplied fact

Positional operators locate an observation within the page sequence but cannot
relate it to the sequence's end, because a requirement about "the last slide" or
"the final third" presupposes the document's length, and a model shown one page
has no access to the total.

The combined object therefore carries one member no contract declares and no
model produces: `doc.page_count`, derived by the engine from the observation set
itself. Expressions read it without declaring it, and no contract may reuse the
name. Requirements about absolute position reduce to arithmetic over a list of
page numbers, and stay inside the same deterministic stage as everything else.

## 4. From natural language to a rule

Custom rules are written as a sentence and compiled by a model. The compilation
is a background job with a fixed validation loop.

### The prompt library

Prompts live in `app/prompt/` as Jinja2 templates, one per task, rendered
through a single entry point. Keeping them as files, in place of strings scattered
through the code, means each prompt can be read, reviewed and revised on its
own, next to the task it serves.

| Template | Task |
| --- | --- |
| `rule_generation.j2` | Compile a natural-language requirement into a formal condition and its extraction contract. |
| `rule_refinement.j2` | Repair a rejected candidate, given the candidate and the validator's message. |
| `rule_contract_completion.j2` | Declare the contract fields a hand-edited condition reads but the contract lacks, leaving the condition untouched. |
| `page_extraction.j2` | Transcribe one page image into Markdown. Used for generic vision models; NuExtract3's native mode takes no prompt. |
| `evidence_extraction.j2` | Observe the declared facts on one page, with a supporting quotation for each. |
| `document_summary.j2` | Condense one document into a factual summary for course-level reasoning. |
| `report_document.j2` | Explain the failures found in one document and propose improvements. |
| `report_course.j2` | Synthesise the per-document findings into a course-level review. |
| `report_priorities.j2` | Turn the deterministically ranked failures into an operational brief. |
| `report_readiness.j2` | Explain the AI-readiness score from its own verdicts. |
| `report_alignment.j2` | Judge constructive alignment across the syllabus and document summaries. |
| `report_chat.j2`, `report_chat_retry.j2` | Answer about a report, or revise a suggestion; the retry template recovers a turn that claimed an edit but produced none. |
| `materials_chat.j2` | Answer a question from retrieved pages, with citations. |
| `chat_compact.j2` | Compress a long transcript into a summary the next turn can continue from. |

Two conventions run through the library. Templates that decide something are
explicit about what the model may not do: the report templates state that the
engine's findings are authoritative and that no new problem may be introduced,
and the extraction template states that the page image is the ground truth and
the transcription only an aid. Templates whose answer must be machine-readable
request a JSON schema through the server's `guided_json` parameter. This
reduces malformed output when the endpoint supports it; parsing and validation
are still required, and malformed JSON can consume a repair attempt.

### The compilation loop

The generation prompt describes the engine to the model before asking for
output, because a usable rule depends on understanding where the boundary
between instruction and expression falls. The model returns the formal
condition, a one-sentence description, and the contract.

Compilation runs up to three attempts, at temperatures 0.0, 0.2 and 0.4. The
first attempt uses zero-temperature sampling, which does not guarantee
identical model outputs; a rejection is fed back through the refinement prompt
together with the validator's message, and the temperature rises so the retry
explores new ground. Exhausting the attempts leaves the candidate in a visible
failed state, and the educator is asked to rephrase.

### Validation

A candidate must pass every check below before it can be proposed.

1. **Syntax.** The expression compiles under cel-python.
2. **Document dependency.** The expression reads at least one fact. An
   expression over literals alone parses cleanly and gives every document the same verdict,
   which is what a half-finished edit leaves behind and what an educator has no
   way to diagnose from a report. This is a reference check, not a proof that the
   expression cannot be a tautology.
3. **Contract well-formedness.** Field names are snake_case and unique, no name
   collides with an engine-supplied one, each type is one of the four, each
   instruction is non-empty and at most 700 characters, and each gate flag is a
   boolean.
4. **Operator compatibility.** Each declared operator is valid for its field's
   type. A `union` over a boolean would run until the aggregation stage and then
   fail during a review.
5. **Closure, in both directions.** Every fact the expression reads is declared,
   and every declared fact is read. A missing declaration produces a rule that
   cannot be evaluated; an unread declaration adds extraction work on every page
   for a fact nothing consumes. Engine-supplied members are the sole exception.
6. **Cross-rule consistency.** A shared field name must have the same type and
   trimmed instruction as existing fields from rules with overlapping document
   types. Aggregation and gate flags are not compared by this validator.
7. **No string matching over extracted facts.** Functions such as `contains` and
   `matches` are rejected. Substring matching over model output is neither
   robust to variations in wording nor necessarily faithful to the requirement:
   the judgement belongs in the field instruction, where it can be reviewed.
8. **Macro targets.** Iteration macros over `doc` fields are accepted only for
   `pages` and `tagged_pages` aggregations in the current validator, even though
   `union` also produces a list.

The compiler also checks the returned severity and category. These checks catch
common structural errors; they do not prove a boolean result, safe arithmetic
for every input, or pedagogical validity. A validated candidate remains a
proposal for educator review. When an entire expression consists of one
quantifying macro, the compiler can infer a missing gate flag for its input.

## 5. Evaluation at runtime

For each ready document version, the engine selects the active rules whose scope
overlaps the document's assigned types, merges their contract fields into one
request per page, and reads each page once for all applicable rules. The model
returns, for each declared fact, an observed value and a supporting quotation.

The quotations travel on their own channel. Aggregation combines the values,
the expression is evaluated over the result, and the quotations accompany the
verdict into the report so a finding can be checked against the page it came
from. The expression does not read quotations directly. Aggregation uses the
page-level `applicable` flag to distinguish relevant pages from silent ones;
legacy entries without that flag fall back to the quotation or a positive
value. Quotations can therefore affect relevance in that fallback path. A
missing requirement may have no quotation, and returned quotations are not
automatically verified against the PDF.

Verdicts are stored against the immutable document version together with the
identity of the rules, conditions and contracts they were produced under.
Re-running a course therefore reuses everything unchanged and re-analyses only
documents that are new, re-uploaded, or whose applicable rules were edited. A
change to any part of a contract, including a field's instruction, invalidates
the reuse, because a differently worded instruction may produce a different
observation.

### Outcomes

| Outcome | Condition |
| --- | --- |
| Passed | The expression evaluates true and the not-applicable conditions below do not hold. |
| Failed | The expression evaluates false. |
| Not applicable | The expression evaluates true, the rule has at least one gate and one non-gate fact, and every gate has a falsy aggregated value. |
| Failed to evaluate | Extraction or evaluation raised an error. |

*Not applicable* is derived from the same single evaluation, with no second
model decision, and is excluded from every score. *Failed to evaluate* is kept
outside the verdict domain entirely: a technical failure is not a pedagogical
finding and is never counted as one.

## 6. Where generation is still authoritative

The framework constrains what a model decides, and the constraint is worth
stating precisely.

Verdicts, scores, rankings, comparisons and applicability are computed. Issue
explanations are written by a model but attached to findings the engine
established, under a response contract requiring one explanation per stored
finding, so fluent prose cannot silently omit a failure. Suggestions are written
by a model and are the only part of a report an educator edits.

One judgement is a direct model judgement and is presented as such:
constructive alignment across the course, meaning whether the stated objectives
are taught by the material and exercised by the graded work. It has no rule
behind it because it is a relation between documents, not a property of any one
of them, and the report does not present it as a verdict.

Structural coverage is what the response contract guarantees. It does not
establish that an explanation is pedagogically correct, that a suggestion is
good, or that the prose stays faithful to its evidence beyond the findings it
must cover.

## 7. The default rule set

Every course starts from the same 22 rules, defined in
`app/rules/default_rules.yaml`: 6 for the syllabus, 4 for slides, 6 for
assessments, 3 for rubrics, 2 spanning syllabus and assessment, and 1 spanning
all four types. They are distributed across five improvement areas, 13 of them
in AI-readiness, and across three severity levels which weight the health score
1, 2 and 3. For documents also classified as slides, assessment- or rubric-only
rules in grading & feedback or AI-readiness are reduced by one severity level
(high to medium, medium to low).

Each rule is justified in the file itself, and each justification is one of two
kinds: direct evidence from the project's educator focus groups, tagged with the
requirement it covers, or an established course-agnostic principle with its
primary source cited.

Two of those principles carry the rules the project proposal names. The revised
Bloom taxonomy supplies the level ordering: `sy1` requires at least one learning
objective asking students to analyse, evaluate or create rather than to know or
understand, which Biggs argues are not observable and so cannot be assessed, and
`a1` sets a floor on the share of assessment items demanding reasoning or
application rather than recall. Constructive alignment grounds `r3`, which asks
that a rubric's criteria trace back to the outcomes the task is meant to
demonstrate. All three cite the `[bloom]` source tag in the file. Alignment as a
relation across the whole course is not reduced to a rule at all: it is the
model judgement described in section 6, because it holds between documents
rather than within one.

The source notes distinguish pedagogical motivations, institutional
expectations and empirical findings. These have different strengths and should
not be read as validation of the automated checks themselves. Three examples
clarify the limits of the default set.

The accessibility rule draws on Universal Design for Learning and the
expectation that a syllabus explains available accommodations. [Boysen
(2024)](https://journals.sagepub.com/doi/10.1177/14782103241255428) found weak
support in studies cited for three CAST guidelines; this does not establish
that every accessibility practice is ineffective. The rule checks for a
statement, not legal compliance. Its suitability and high severity must be
assessed against the institution's own requirements.

Higher-order tasks should not be treated as proof of resistance to AI.
[Herrmann-Werner et al. (2024)](https://doi.org/10.2196/52113) found that GPT-4
answered 91–93% of 307 psychosomatic medicine questions correctly, depending on
the prompt, and analysed its errors using Bloom's taxonomy. That result is
specific to the model and assessment studied; it does not establish a general
relationship between Bloom level and model accuracy. The default set therefore
also includes process evidence and explicit AI-use policies; its AI-readiness
score measures these checks, not proven resistance to delegation.

The 50% threshold in `a1` is a design choice, not a universal research-derived
cutoff. A concrete institutional precedent is the [Baylor College of Medicine
assessment guidance
(2015)](https://www.bcm.edu/sites/default/files/2019/04/principles-and-guidelines-for-assessments-6.15.15.pdf),
which sets targets of at least 40% higher-order items for M1 exams and 50% for
M2. MTG counts reasoning or application rather than recall, which need not
match another framework's definition of higher-order thinking. The precedent
motivates the threshold but does not validate it across disciplines or as an
AI-resistance measure.

Default rules are activated and deactivated per course, and their definitions
stay fixed, so the baseline stays comparable across courses. Course- and
institution-specific expectations are expressed as custom rules.

## 8. Files

| Path | Content |
| --- | --- |
| `app/rules/default_rules.yaml` | The 22 default rules, with requirement, rationale, formal condition, contract, and the evidence and source notes for each. |
| `app/prompt/*.j2` | The prompt library, one template per task. |
| `app/services/rule_generation.py` | Natural-language compilation, the repair loop, and the validators. |
| `app/services/contracts.py` | The contract schema, field types, operators and the engine-supplied members. |
| `app/services/aggregation.py` | Combination of page observations into document facts, including the positional operators. |
| `app/services/rule_engine.py` | Deterministic evaluation of a condition over a document object. |
| `app/services/evidence.py` | Per-page fact extraction with quotations. |
| `app/services/analysis.py` | Rule selection, run orchestration and verdict reuse. |

## Related documents

- [Installation guide](installation.md), setting up the system.
- [Usage guide](usage.md), how educators work with rules and reports.
- [API and interface reference](api.md), the HTTP surface and internal packages.

---

Copyright 2026 HES-SO Valais-Wallis. This document is licensed under
[CC BY 4.0](../LICENSE-docs).
