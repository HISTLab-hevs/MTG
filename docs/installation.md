# Installation

This document describes how to install and run MTG (Mind The Gap) on a single
host. It covers the prerequisites, the one-command setup, the model endpoints
the system requires, and the checks that confirm a working installation.

MTG is distributed as a Docker Compose stack. It ships no language model of its
own: the three model roles it depends on are supplied by external
OpenAI-compatible endpoints that the operator configures.

## 1. Prerequisites

### Host

| Requirement | Notes |
| --- | --- |
| Linux shell environment | `setup.sh` needs Bash 4+, GNU utilities, Git and curl. On Windows, use WSL with Docker integration; it is not a PowerShell script. |
| Docker with the Compose plugin | `docker compose version` must succeed. The whole stack runs in containers. |
| Two free TCP ports | `setup.sh` prefers 80 and 443 and falls back to 8089-8091 and 8444-8446 when they are taken. |
| Disk space | Uploaded PDFs and one rendered PNG per page are stored on a Docker volume. Budget roughly 1-2 MB per page at the default rendering resolution. |
| Network access to the model endpoints | The containers must be able to reach the three endpoints below. |

No Python, Postgres or Redis installation is needed on the host: the stack
brings its own. Python 3.12 and [uv](https://docs.astral.sh/uv/) are needed only
to run the test suite or build the API reference, both of which run outside
Docker.

### Model endpoints

MTG separates model use into three roles, each configured independently. All
three are required for a complete installation.

| Role | Purpose | Recommended service |
| --- | --- | --- |
| Extraction | Transcribes each rendered page image into Markdown. | A vision-language model, typically vLLM serving `numind/NuExtract3`. |
| Generation | Compiles custom rules into their formal condition, extracts the facts a rule declares, writes report prose, and answers both conversational surfaces. | An instruction-following vision-language model: fact extraction sends page images as well as text. |
| Embedding | Indexes page text so the materials agent can retrieve it. | Any embedding model that emits 768-dimensional vectors. |

Each endpoint must expose the OpenAI-compatible API, and each configured URL
must end in `/v1`.

The three roles may point at the same physical machine, and roles may share a
model if it supports all required inputs and operations. Fact extraction
deliberately uses the generation endpoint: a template-driven document model
such as NuExtract3 is used here in document-to-Markdown mode, while the
generation model must interpret the per-field instructions in a rule contract.

The embedding model must emit vectors of the dimension declared as
`EMBEDDING_DIM` in `app/models` (768). A model of a different width fails at
the point of insertion, and never degrades silently.

### Consequences of an unconfigured endpoint

Each missing endpoint fails at a distinct and visible stage:

- without extraction, uploads are stored but page transcription fails and the
  document version never becomes ready;
- without embedding, a version fails at the indexing step, and never becomes
  ready with an index missing;
- without generation, analysis cannot run.

## 2. Installation

Choose either a source checkout with `setup.sh` (below), or the
[published image and release bundle](docker_image.md#1-download-and-configure).
Both use the same Compose, environment template, and Caddyfile, and both need
your model endpoints and a site-specific session secret. Image installations
need no source checkout: configure `.env`, run `docker compose pull`, then
`docker compose up -d --no-build`.

```bash
git clone --branch main https://github.com/HISTLab-hevs/MTG.git
cd MTG
./setup.sh
```

`setup.sh` performs the entire first-run sequence: it creates `.env` from
`.env.example` with a freshly generated random session secret, selects free host
ports and records them, builds the images, starts the stack, waits for the
application to answer, and offers to create the first administrator account.
Database migrations run automatically when the web container starts.

On the first run the script reports that the model endpoints are not yet set.
Fill them in and run the script again:

```env
GENERATION_BASE_URL=http://<llm-host>:8001/v1
GENERATION_API_KEY=EMPTY
GENERATION_MODEL=<vision-language-model-as-served>

EXTRACTION_BASE_URL=http://<vlm-host>:8000/v1
EXTRACTION_API_KEY=EMPTY
EXTRACTION_MODEL=numind/NuExtract3

EMBEDDING_BASE_URL=http://<llm-host>:8002/v1
EMBEDDING_API_KEY=EMPTY
EMBEDDING_MODEL=<embedding-model-as-served>
```

`GENERATION_API_KEY` is sent as the bearer token. Local vLLM deployments that
require no authentication accept the placeholder `EMPTY`; a hosted endpoint
needs its real key.

After editing `.env`, run `docker compose up -d` to apply the changes while
keeping the configured ports. `setup.sh` selects free ports on every run; when
the stack is already running, its occupied ports can cause the script to choose
different ones. Use `docker compose up -d --build` when the source also
changed.

### Non-interactive installation

For unattended provisioning, the administrator account can be passed as
arguments:

```bash
./setup.sh --admin-email admin@example.org --admin-password 'replace-with-a-strong-password'
```

## 3. Serving the models

The endpoints must support the inputs and extra request fields MTG sends. In
particular, generation requires image input, `guided_json` and
`chat_template_kwargs`; OpenAI API compatibility alone is insufficient. The
client does not automatically remove unsupported fields. The reference
deployment uses vLLM, and its version must accept that request format.

```bash
vllm serve numind/NuExtract3 --host 0.0.0.0 --port 8000 \
  --trust-remote-code --chat-template-content-format openai \
  --generation-config vllm
```

Check the [NuExtract3 model
card](https://huggingface.co/numind/NuExtract3#vllm-deployment) for deployment
requirements and tune context length to the available GPU memory. The command
above starts only extraction; generation and embedding must also be served and
configured. Container URLs must address the model host, not `localhost`, which
refers to the calling container.

`EXTRACTION_MODE` selects how the extraction endpoint is addressed:

- `nuextract-markdown` uses NuExtract3's native document-to-Markdown mode, which
  is selected through vLLM's `chat_template_kwargs` and takes no text prompt.
  This is the value shipped in `.env.example`.
- `prompt` drives a generic vision model with the template in
  `app/prompt/page_extraction.j2`. Use this value for any other vision model.

Set `GENERATION_MAX_MODEL_LEN` to the served model's actual `max_model_len`.
Prompts are budgeted against this value, so a figure larger than the truth
causes long documents to overflow the model's context at generation time.

## 4. Verifying the installation

`setup.sh` prints the application URL when it finishes, for example
`https://localhost`. The certificate is issued by Caddy's internal authority,
so browsers do not trust it by default. For normal institutional use,
distribute and trust the internal CA certificate on client devices or configure
a trusted certificate; accepting a warning does not establish permanent trust.

A complete verification runs one document through the pipeline:

1. Sign in with the administrator account and create an educator account from
   the administration page.
2. As that educator, create a course and upload one PDF, classified with at
   least one document type.
3. Watch the status of the document version advance through `uploaded`,
   `rendering`, `extracting`, `embedding`, and `ready`. Each transition
   confirms one part of the stack: the first two exercise storage and
   rendering, the third the extraction endpoint, the fourth the embedding
   endpoint.
4. Start an analysis from the Analysis page. A completed report confirms the
   generation endpoint and the rule engine.

If a version stops at `failed`, its recorded reason names the stage that failed.
Container logs give the underlying error:

```bash
docker compose logs -f web worker-documents worker-reports worker-rules
```

## 5. Transport security

Certificates belong to the operator's installation. No certificate, private
key, or CA from the maintainer is included in the image or release bundle.

The application is served over HTTPS only. Plain HTTP is redirected by the
reverse proxy and never reaches the application, and the session cookie is
marked `Secure`. Certificates are issued automatically for whatever hostname or
address the server is reached at, so no certificate configuration is required.

`HTTPS_ONLY=false` disables the application redirect and the `Secure` cookie
attribute. It does not disable the redirect or TLS in the supplied Caddyfile.
This value is intended for development against a plain HTTP listener. With no
HTTPS listener present, leaving it at `true` makes login appear to fail
silently, because the browser never returns a cookie it was told to send only
over TLS.

### Local machine or institutional network: trust the local CA

The supplied Caddyfile works without a public domain. On first use Caddy creates
a private CA in this installation's `caddy_data` volume and issues certificates
for the names used to access the app. After opening the HTTPS URL once, export
only its public root certificate:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./mtg-local-ca.crt
```

Verify that this file came from your own server, then install it in the trusted
root certificate store of each client that should trust this installation:

- Windows: run `certutil -user -addstore Root mtg-local-ca.crt` for the current
  user's trust store, or distribute it using institutional device management.
- macOS: import it into Keychain Access, open the certificate's Trust settings,
  and trust it for SSL.
- Debian/Ubuntu: copy it to
  `/usr/local/share/ca-certificates/mtg-local-ca.crt` and run
  `sudo update-ca-certificates`.

Some browsers use their own certificate store; import the same public root
there if required. Restart the browser and verify that HTTPS loads without a
certificate warning. An internal CA is trusted to issue certificates: distribute
it only to clients that should trust your server. Never distribute `root.key`
or the entire Caddy data volume. Preserve that volume across upgrades so clients
do not need to trust a newly generated CA. See [Caddy's Docker instructions](https://caddyserver.com/docs/running#docker-compose).

Prefer `localhost` for access on the server itself, or a hostname resolving to
the server for remote clients. With the supplied `default_sni localhost`, clients
connecting by IP without SNI receive a localhost certificate: trusting the CA
does not fix that hostname mismatch. Use a matching hostname instead.

### Public domain: let Caddy obtain a trusted certificate

For a public hostname such as `mtg.example.org`, point its DNS A/AAAA records
at the deployment server (publish AAAA only if IPv6 reaches it). Allow/forward
inbound TCP ports 80 and 443 to that server. Set `HTTP_PORT=80`,
`HTTPS_PORT=443`, and `HTTPS_ONLY=true` in `.env`.

Replace the contents of the installation's `Caddyfile` with the following,
substituting your email and real domain:

```caddyfile
{
    email admin@example.org
}

mtg.example.org {
    reverse_proxy web:8000
}
```

This intentionally replaces the original global `local_certs` and `tls internal`
settings. Caddy obtains and renews a publicly trusted certificate and redirects
HTTP to HTTPS. Apply both environment and file changes:

```bash
docker compose up -d --no-build --force-recreate caddy
docker compose logs --tail=100 caddy
```

Access the app using that exact hostname. No certificate files need to be sent
by the maintainer. For private domains, blocked validation ports, or certificates
issued by your institution, follow your institution's TLS setup and
[Caddy's automatic HTTPS documentation](https://caddyserver.com/docs/automatic-https).
DNS challenges require a Caddy build with the appropriate DNS provider module;
the stock image in this repository does not include those modules.

## 6. Configuration reference

All settings live in `.env`. The table lists deployment settings from the
application and Compose; `.env.example` carries the recommended starting
values. Tests also use `JOB_EAGER=true` to run jobs inline; leave it disabled
in deployment.

| Variable | Purpose |
| --- | --- |
| `MTG_IMAGE` | Application image; `mtg:local` for source setup, a release digest for image installs. |
| `APP_SECRET_KEY` | Signs session cookies. Generated randomly by `setup.sh`. |
| `SESSION_IDLE_MINUTES` | Idle period after which a user is logged out. |
| `HTTPS_ONLY` | Controls the application HTTPS redirect and `Secure` cookie; Caddy is configured separately. |
| `HTTP_PORT`, `HTTPS_PORT` | Host ports for the reverse proxy, set by `setup.sh`. |
| `DATABASE_URL`, `REDIS_URL` | Postgres and Redis locations. Compose overrides both per service. |
| `STORAGE_DIR` | Filesystem root for uploaded PDFs and rendered page images. |
| `GENERATION_BASE_URL`, `GENERATION_API_KEY`, `GENERATION_MODEL` | Vision-language model: rule compilation, fact extraction, report prose, conversation. |
| `EXTRACTION_BASE_URL`, `EXTRACTION_API_KEY`, `EXTRACTION_MODEL` | Vision model endpoint for page transcription. |
| `EXTRACTION_MODE` | `nuextract-markdown` or `prompt`. |
| `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL` | Embedding endpoint for page indexing and retrieval. |
| `GENERATION_MAX_MODEL_LEN` | The generation model's real context window; prompts are budgeted against it. |
| `REPORT_OUTPUT_TOKEN_BUDGET`, `CHAT_OUTPUT_TOKEN_BUDGET` | Output space reserved inside that window. |
| `LLM_REQUEST_TIMEOUT`, `LLM_MAX_RETRIES` | Per-attempt timeout and retry count for one model call. |
| `EMBEDDING_BATCH_SIZE` | Pages sent per embedding request. |
| `MATERIALS_RETRIEVAL_K` | Pages the materials agent retrieves per question. |
| `PAGE_EXTRACTION_CONCURRENCY` | Pages transcribed in parallel per document. |
| `EVIDENCE_EXTRACTION_CONCURRENCY` | Pages whose rule facts are extracted in parallel within one document. |
| `REPORT_GENERATION_CONCURRENCY` | Documents whose report prose is generated in parallel. |
| `REPORT_WORKER_REPLICAS` | Analysis workers; keep at 1 unless the model server has spare capacity. |
| `MAX_UPLOAD_MB` | Maximum accepted PDF size. |
| `PDF_RENDER_DPI` | Resolution used when rendering pages to images. |

The three concurrency settings and the worker replica count all draw on the same
resource, namely the model server's concurrency budget. Raising any of them
above what the server can absorb slows every request, and the run finishes no
sooner.

## 7. Operating the stack

```bash
docker compose up -d --build   # apply code changes; migrations run on start
docker compose down            # stop the stack
docker compose down -v         # stop and delete the database and stored files
```

Commands that touch the database run inside the web container, because Postgres
is not published on the host:

```bash
docker compose exec web python -m app.cli create-admin \
  --email admin@example.org --password 'replace-with-a-strong-password' \
  --first-name Admin --last-name User
```

## 8. Data residency

Processing runs in the Compose stack and at the model endpoints the operator
selects. Uploaded PDFs and rendered pages are written to a local Docker volume,
and the database holds the extracted text, rule definitions, evaluations and
reports. During document analysis and conversation, course content is sent to
the configured model endpoints. Image builds and other deployment operations
may also access package and container registries. Pointing those at services on
the same institutional network keeps all course material within it.

## Related documents

- [Usage guide](usage.md), how educators work with an installed system.
- [API and interface reference](api.md), the HTTP surface and command-line tools.
- [Hybrid prompt-rule framework](prompt_rule_framework.md), the rule
  representation and prompt library.
- [Published Docker image](docker_image.md), pulling a pre-built image instead
  of building from source.

---

Copyright 2026 HES-SO Valais-Wallis. This document is licensed under
[CC BY 4.0](../LICENSE-docs).
