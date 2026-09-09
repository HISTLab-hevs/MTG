# Published Docker image

MTG has two installation paths: clone the source and run `./setup.sh`, or
pull the published image and start the same stack with Docker Compose.
Both require a site-specific `.env` and reachable model endpoints. The image
contains the application and dependencies, not models, accounts, uploaded
materials, an existing database, secrets, or certificates.

The application image is `ghcr.io/histlab-hevs/mtg`. Each release's deployment
ZIP selects its exact image digest; you do not need to edit the Compose file.

One image runs `web`, `worker-documents`, `worker-reports`, and `worker-rules`;
Compose supplies their different commands. Postgres/pgvector, Redis, and Caddy
use their own public images. A plain `docker run` is not a complete installation:
the web migration command, networking, workers, and volumes live in Compose.

## 1. Download and configure

Install Docker with the Compose plugin, using Linux containers. On Windows,
start Docker Desktop in Linux-container mode. No host Python,
Git, Postgres, or Redis installation is needed. Download the deployment ZIP
from the [release page](https://github.com/HISTLab-hevs/MTG/releases), not GitHub's
automatically generated source-code ZIP. Always use files from the same release
as the image. Set the version once below, then paste the rest unchanged; use
the version shown on the release page:

PowerShell:

```powershell
$Version = "1.0.0"
Invoke-WebRequest -Uri "https://github.com/HISTLab-hevs/MTG/releases/download/v$Version/mtg-deploy-$Version.zip" -OutFile mtg-deploy.zip
Expand-Archive -LiteralPath mtg-deploy.zip -DestinationPath mtg
Set-Location mtg
Copy-Item .env.example .env
```

Linux/macOS:

```bash
VERSION=1.0.0
curl -fL -o mtg-deploy.zip "https://github.com/HISTLab-hevs/MTG/releases/download/v$VERSION/mtg-deploy-$VERSION.zip"
mkdir mtg
unzip mtg-deploy.zip -d mtg
cd mtg
cp .env.example .env
```

For a private release, sign in to GitHub in your browser with an authorized
account and download the ZIP from the release page; the unauthenticated download
commands above apply to public assets. Before pulling a private image, run:

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

At the password prompt, use a personal access token classic with `read:packages`.
Your account must have access to the package. Public images need no login.
See [GHCR authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

The example download URLs become available when that release is published.
Optionally compare the downloaded ZIP's SHA-256 with the release's SHA256SUMS
using `Get-FileHash mtg-deploy.zip -Algorithm SHA256` or `sha256sum mtg-deploy.zip`.

Keep `docker-compose.yml`, `Caddyfile`, and `.env` together. Leave the packaged
`MTG_IMAGE` digest unchanged. To generate the session secret without installing
Python locally, pull the image and run its Python without starting the stack:

```bash
docker compose pull
docker compose run --rm --no-deps web python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the result into `APP_SECRET_KEY` in `.env`. This temporary container does
not run migrations or create an admin. Then configure:

- `EXTRACTION_*`: the page-extraction vision endpoint and model; choose
  `EXTRACTION_MODE=nuextract-markdown` for NuExtract3, `prompt` for a generic VLM.
- `GENERATION_*`: an instruction-following model supporting image input,
  `guided_json`, and `chat_template_kwargs`. Generic OpenAI compatibility
  alone is insufficient for this branch's request format.
- `EMBEDDING_*`: an embedding endpoint emitting **768-dimensional** vectors.
- `GENERATION_MAX_MODEL_LEN`: the generation model's actual context limit.
- `HTTP_PORT` / `HTTPS_PORT`: free host ports, normally 80/443.

All model base URLs must include `/v1`. Use the real API keys, or `EMPTY` for
servers configured without authentication. Models must be reachable from the
containers: `localhost` would address the calling container. With Docker
Desktop, `host.docker.internal` addresses the host; for a remote model server,
use its reachable hostname/IP. See [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/).

Keep `HTTPS_ONLY=true` with the supplied proxy. Compose overrides the template's
host-development database, Redis, and storage addresses with internal ones.
Never distribute your completed `.env`. See [Installation](installation.md)
for the full model requirements and variable reference.

## 2. Start, create an administrator, and verify

```bash
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
docker compose logs --tail=100 web
```

The earlier `pull` downloads all required images. `--no-build` prevents an
accidental source build. Wait for Alembic to finish and Uvicorn to start, then
create the first administrator (replace the email and password):

```bash
docker compose exec web python -m app.cli create-admin --email admin@example.org --password "REPLACE-WITH-A-STRONG-PASSWORD" --first-name Admin --last-name User
```

Open `https://localhost` on the Docker host, or `https://SERVER:HTTPS_PORT`.
Configure certificate trust as explained in
[Installation: transport security](installation.md#5-transport-security).
Caddy creates this installation's own CA; no maintainer certificates are shipped.

A fresh installation has empty data volumes and no users until the admin
command runs. Built-in rule definitions are application resources, not personal
data. Reusing an existing Compose project reuses its existing volumes.

Verify the complete pipeline: sign in, create an educator and course, upload
a PDF and wait for `ready`, then run an analysis, try the materials chat, and
compile a custom rule. Running containers alone do not prove model compatibility.

```bash
docker compose logs -f web worker-documents worker-reports worker-rules
```

Verify your own model endpoints with the checks above; a successful startup
does not guarantee that an external model supports the required request format.

## 3. Updates and source installations

For an image update, back up the database and uploaded storage, read the new
release notes, and download its bundle. Keep your existing `.env` and local
Caddyfile changes; merge any configuration changes and copy the new template's
`MTG_IMAGE` value into your `.env`. Then:

```bash
docker compose pull
docker compose up -d --no-build
```

A pinned digest does not advance just because `pull` is repeated. Keep the
same installation directory/project name so Compose reuses the same volumes.
Do not use `down -v` to update: it deletes the database, uploaded files, and
Caddy volumes. Database migrations run on web startup; reverting an image does
not undo a migration. Redis currently has no persistent volume: finish queued
work before recreating it.

For a source installation, clone the `main` branch and run `./setup.sh` as
explained in [Installation](installation.md#2-installation). The script creates
`.env` if missing, generates the secret, records `MTG_IMAGE=mtg:local`, builds
the checked-out source once for all four application services, and starts it.
Configure the same model endpoints and certificate trust. Do not run `setup.sh`
inside an image-only installation; it deliberately selects source-build mode.
