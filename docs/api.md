# API and interface reference

This document describes the programmatic surfaces of MTG: the HTTP endpoints the
application exposes, the command-line tool, the internal Python API, and the
external services the system calls. It is a reference, consulted while working; the
[usage guide](usage.md) is the place to start for what the operations mean.

## 1. Interface style

MTG is a server-rendered hypermedia application. Endpoints return HTML, either
a full page or a fragment that htmx swaps into an existing one, and accept
`application/x-www-form-urlencoded` bodies, except PDF uploads, which use
`multipart/form-data`. There is no supported public, machine-facing API; a few
internal endpoints return JSON.

The omission is deliberate. Every state change the system exposes is a step in
a review that an educator performs and inspects. Publishing the same operations
as a headless interface is outside the supported educator workflow. This is an
interface design choice, not a technical barrier to automating authenticated
HTTP requests.

Consequently, the endpoint tables below document the application's own
interface. They are the correct reference for reading the source, for
understanding the request flow, and for operating the system. They are not a
contract for external clients, and endpoint paths and fragment shapes may change
between versions.

The main non-HTML responses are:

- the original PDF of a document version, returned as a file;
- a rendered page image, returned as an image;
- a report exported as PDF;
- chat compaction results, returned as JSON.

FastAPI also exposes `/openapi.json`, `/docs` and `/redoc` with its default
configuration. Validation and HTTP errors can return JSON.

### Authentication

Access is by signed session cookie, established at `POST /login`. The cookie is
marked `Secure` when `HTTPS_ONLY` is enabled, and the session expires after
`SESSION_IDLE_MINUTES` of inactivity. There are no API keys and no token-based
authentication.

Two dependencies enforce the role split. Educator endpoints require a signed-in
member and additionally verify that the requested course belongs to that
account, so a course identifier belonging to another educator is not
distinguishable from one that does not exist. Administration endpoints require
the administrator role.

### Status codes

| Code | Meaning in this application |
| --- | --- |
| 200 | The page or fragment is returned. |
| 204 | The request was a deliberate no-op, such as an empty chat message. htmx swaps nothing. |
| 303 | Redirect after a successful state change, or to the login form. |
| 307, 308 | HTTP-to-HTTPS redirect from the application or reverse proxy. |
| 400 | The requested operation is invalid for the resource state. |
| 403 | The account is signed in, and lacks permission for the operation. |
| 404 | The resource does not exist, or does not belong to the signed-in account. |
| 409 | A version cannot be deleted while it is processing. |
| 422 | Required form data or query parameters are missing or invalid. |

For an expired htmx session, 204 may carry `HX-Redirect: /login`; the browser
navigates to sign-in. Some form errors instead return a 200 HTML fragment with
an inline error, so 200 alone does not establish that a change was saved.

## 2. Endpoints

### Session and information pages

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Redirects to the account's home page, or to the login form. |
| GET | `/login` | The sign-in form. |
| POST | `/login` | Verifies credentials and opens a session. An unknown address and a wrong password give the same message. |
| POST | `/logout` | Ends the session. |
| GET | `/how-it-works` | How the system works, for educators. |
| GET | `/about` | Project background. |
| GET | `/evidence` | The evidence base behind the default rules. |

The three information pages accept an optional course identifier, which keeps
the course's navigation in the sidebar so an educator consulting them does not
lose their place.

### Administration

Requires the administrator role. Administrator accounts are excluded from the
list and cannot be disabled or deleted through these endpoints.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/admin` | Lists educator accounts. |
| POST | `/admin/users` | Creates an educator account. Administrators are created through the CLI. |
| POST | `/admin/users/{user_id}/toggle` | Enables or disables an account. |
| POST | `/admin/users/{user_id}/delete` | Permanently deletes an educator account and all its courses, documents and reports. |

### Courses

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/courses` | The signed-in educator's courses. |
| POST | `/courses` | Creates a course. |
| GET | `/courses/{course_id}` | Opens the course workspace. |
| GET, POST | `/courses/{course_id}/edit` | Reads and updates the course details. |
| POST | `/courses/{course_id}/delete` | Deletes the course and everything it owns. |
| GET | `/courses/{course_id}/overview` | The Overview page. |
| GET | `/courses/{course_id}/materials` | The Material page. |
| GET | `/courses/{course_id}/report` | The Analysis page. |

### Documents

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/courses/{course_id}/materials/upload` | Uploads a PDF with its document types. Creates a new version of an existing filename; a file byte-identical to any stored version in the course is skipped. |
| GET | `/courses/{course_id}/materials/list` | The material list fragment, polled while processing runs. |
| GET | `/courses/{course_id}/documents/{document_id}` | One document with its versions. |
| GET | `/courses/{course_id}/documents/{document_id}/panel` | The document status fragment. |
| GET | `/courses/{course_id}/documents/{document_id}/file` | The original PDF. |
| GET | `/courses/{course_id}/documents/{document_id}/pages/{page_id}/image` | One rendered page image. |
| POST | `/courses/{course_id}/documents/{document_id}/delete` | Deletes the document and all its versions. |
| POST | `/courses/{course_id}/documents/{document_id}/versions/{version_id}/delete` | Deletes one version. |

Uploads are limited to PDF files no larger than `MAX_UPLOAD_MB`. The detail,
panel and file GET routes accept `version_id`; omitting it selects the latest
version.

### Rules

All paths are relative to `/courses/{course_id}/rules`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `` | The Rules page. |
| GET | `/panel` | The rule list fragment, polled while a compilation runs. |
| POST | `/chat` | Submits a natural-language rule request and enqueues its compilation. |
| POST | `/{rule_id}/accept` | Admits a validated proposal to the active set. |
| POST | `/{rule_id}/reject` | Discards a proposal. |
| GET, POST | `/{rule_id}/edit` | Reads and updates a custom rule, including its formal condition. |
| GET | `/{rule_id}/edit/draft` | The current editing draft. |
| POST | `/{rule_id}/edit/cancel` | Abandons the draft. |
| POST | `/{rule_id}/reconcile` | Declares contract fields that a hand-edited condition reads but the contract lacks. |
| POST | `/{rule_id}/toggle` | Activates or deactivates a rule. |
| POST | `/{rule_id}/delete` | Deletes a custom rule. |

### Reviews and reports

All paths are relative to `/courses/{course_id}/report`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/analyze` | Starts a review of the course's ready documents. |
| GET | `/panel` | The run status fragment, polled while a review runs. |
| GET | `/view` | The most recent report. |
| GET | `/{report_id}/view` | One report from the history. |
| GET | `/history` | Redirects to the Analysis page, which contains report history. |
| GET | `/compare` | The comparison between two reviews. |
| GET | `/view/pdf` | The most recent report, exported as PDF. |
| GET | `/{report_id}/view/pdf` | One historical report, exported as PDF. |
| POST | `/{report_id}/delete` | Deletes one report. |
| POST | `/{report_id}/solutions` | Saves an edited course-level suggestion. |
| POST | `/section/{section_id}/solutions` | Saves an edited document-level suggestion. |
| POST | `/{report_id}/solutions/undo` | Restores the previous version of a course-level suggestion. |
| POST | `/section/{section_id}/solutions/undo` | Restores the previous version of a document-level suggestion. |

Comparison takes two repeated `ids` parameters, for example
`/courses/1/report/compare?ids=4&ids=7`; `reverse=true` reverses the default
chronological direction. Both reports must belong to the course.

Only suggestion blocks are writable. There is no endpoint that changes a
verdict, a score, a finding or a computed metric, at any privilege level.

### Conversation

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/courses/{course_id}/materials/chat` | One materials-agent turn. Read-only. |
| POST | `/courses/{course_id}/materials/chat/compact` | Compacts the materials transcript. |
| POST | `/courses/{course_id}/report/{report_id}/chat` | One report-agent turn, which may return an edit to a suggestion block. |
| POST | `/courses/{course_id}/report/{report_id}/chat/compact` | Compacts the report transcript. |

Both agents keep their transcript in the browser and send it with each turn
as a JSON array in the `history` field. The server holds no conversation state.
An empty message is answered with 204 and changes nothing.

## 3. Command-line interface

The management CLI covers the operations that have no place in the web
interface. Because Postgres is not published on the host, the CLI runs inside
the web container.

```bash
docker compose exec web python -m app.cli create-admin \
  --email admin@example.org --password 'replace-with-a-strong-password' \
  --first-name Admin --last-name User
```

`create-admin` is the only command. There is no sign-up route, so it is how the
first administrator account comes into existence. Use it for additional
administrators too; educator accounts are created through the administration
page. It exits non-zero, with a message, when the address is malformed or
already registered.

## 4. Internal Python API

The `app` package is documented as a Sphinx reference generated from the
docstrings in the source, so behaviour is described in one place only. It builds
on the host and needs no database, Redis or model server:

```bash
uv sync --group docs
cd docs && make html
```

`make open` builds and opens the result, `make strict` fails on any warning,
and `make clean` removes the output. Serving the directory over HTTP avoids
browser restrictions that can affect search when opening the files directly:

```bash
cd docs/build && python3 -m http.server 8765 --bind 127.0.0.1
```

The reference covers the application package only.

Docstrings are Google-style, rendered by `sphinx.ext.napoleon`.

### Package layout

| Package | Responsibility |
| --- | --- |
| `app/routers/` | HTTP endpoints. Validate input, read and write rows, enqueue background work, render templates. Long processing jobs are queued; chat and transcript compaction call models during the request. |
| `app/services/` | Domain logic: uploads and processing, rule compilation and evaluation, analysis, reporting, conversation. |
| `app/models/` | The SQLAlchemy schema. |
| `app/llm/` | The shared model client and prompt rendering. Every model call goes through it. |
| `app/prompt/` | Jinja2 prompt templates, one per task. |
| `app/rules/` | `default_rules.yaml`, the default rule set with text, formal conditions and contracts. |
| `app/jobs/` | Enqueueing onto Redis. |
| `app/templates/`, `app/static/` | Server-rendered pages, htmx fragments, stylesheets and scripts. |

The `services/` grouping is thematic; the directory itself is flat.

### Background queues

Work that outlives its request runs on one of three Redis queues, each with its
own worker, so that no kind of work blocks another.

| Queue | Work |
| --- | --- |
| `documents` | Page rendering, page transcription, embedding. |
| `reports` | Fact extraction, rule evaluation, report prose. |
| `rules` | Compiling a natural-language rule request into its formal condition and contract. |

Workers write a heartbeat. A review whose worker dies is detected through a
stale heartbeat and marked failed, so a course is never left waiting behind a
run that no longer exists.

## 5. External service interfaces

MTG consumes three OpenAI-compatible endpoints. Each URL must include `/v1`.

| Role | Endpoint used | Called by |
| --- | --- | --- |
| Extraction | `/v1/chat/completions` with a page image | Page transcription. |
| Generation | `/v1/chat/completions`, including page images for fact extraction | Rule compilation, fact extraction, report prose, both agents. |
| Embedding | `/v1/embeddings` | Page indexing and retrieval. |

Two properties of these calls affect which servers are suitable.

**Constrained decoding.** Calls whose answer must be machine-readable request a
JSON schema through the server's guided-decoding parameter, so the answer's
shape is constrained when the server supports it. The current client sends
`guided_json` and `chat_template_kwargs`; the endpoint must accept these
fields. There is no automatic retry without unsupported parameters. Parsing,
validation and repair still handle imperfect responses, but cannot fix an HTTP
rejection of the request format.

**Extended reasoning disabled.** Most structured generation calls pass a
chat-template argument turning off extended reasoning where the served template
supports it. The reason is correctness as well as latency: on complex rule
compilations, leaving it enabled has been observed to consume the entire
request timeout without producing an answer.

Requests are retried, with a per-attempt timeout of `LLM_REQUEST_TIMEOUT` and
`LLM_MAX_RETRIES` additional attempts after the initial call. Report and chat
text use estimated token budgets based on `GENERATION_MAX_MODEL_LEN` minus
reserved output space. This reduces context overflows but does not guarantee
that every request fits, especially when page images are involved.

## Related documents

- [Installation guide](installation.md), prerequisites and configuration.
- [Usage guide](usage.md), the educator-facing workflow.
- [Hybrid prompt-rule framework](prompt_rule_framework.md), the rule language,
  its validation, and the prompt library.

---

Copyright 2026 HES-SO Valais-Wallis. This document is licensed under
[CC BY 4.0](../LICENSE-docs).
