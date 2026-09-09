# MTG — Mind the Gap

[![Release](https://img.shields.io/github/v/release/HISTLab-hevs/MTG?sort=semver)](https://github.com/HISTLab-hevs/MTG/releases/latest)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)

**Mind the Gap: Adapting Higher Education to the Age of AI**

HES-SO R&I project **MTG, 140012/RI-PSTRAT25-06**

[Project website](https://mtg.histlab.hevs.ch/) · [Releases](https://github.com/HISTLab-hevs/MTG/releases)

MTG helps educators review course materials and redesign teaching and assessment
for the age of generative AI. It combines language-model document understanding
with explicit pedagogical rules to produce traceable findings and improvement
suggestions. This repository contains the project's **software release**.

## What it does

- Organizes courses and PDF materials, including syllabi, slides, assessments,
  and rubrics, with document versioning.
- Extracts page-level text using an external vision model and indexes it for
  conversations grounded in the uploaded materials.
- Evaluates pedagogical rules deterministically using CEL expressions and
  source-linked evidence; language models generate explanations and suggestions.
- Produces review reports with scores, priorities, history, comparisons, and
  PDF export, and supports custom rules written in natural language.

See the [user guide](docs/usage.md) for the educator workflow and the
[rule framework](docs/prompt_rule_framework.md) for the reasoning approach.

## Requirements

- Docker with the Compose plugin. On Windows, use Docker Desktop with Linux
  containers; the source setup script runs in Git Bash or WSL.
- Reachable external endpoints for **page extraction**, **generation**, and
  **embedding**. No models are included in the image.
- Generation must support image input and the request parameters used by this
  release; embeddings must contain **768 dimensions**. Generic OpenAI API
  compatibility alone is not sufficient.

Model requirements, configuration variables, networking, and HTTPS certificates
are documented in [Installation](docs/installation.md).

## Installation

Choose either path. Both use the same Compose file and require your own `.env`,
model endpoints, and session secret. New installations contain no user accounts
or uploaded materials.

### From source

```bash
git clone --branch main https://github.com/HISTLab-hevs/MTG.git
cd MTG
./setup.sh
```

The script creates `.env` with a random secret, builds the application, starts
the stack, and offers to create the first administrator. Configure the three
model endpoints in `.env`, then apply configuration changes with
`docker compose up -d --no-build`.

### From the container image

Download the deployment ZIP from the [latest release](https://github.com/HISTLab-hevs/MTG/releases/latest),
extract it, and follow [Installing the Docker image](docs/docker_image.md).
Copy `.env.example` to `.env`, generate the secret, and configure the model
endpoints. Then:

```bash
docker compose pull
docker compose up -d --no-build
```

The ZIP includes the configuration files and pins the exact image digest from
`ghcr.io/histlab-hevs/mtg`. No source checkout is required. While access is
restricted, sign in to GitHub to download the release and authenticate to GHCR
as explained in the image guide.

Open the configured HTTPS URL and create the first administrator as described
in the installation guide. Caddy generates an installation-specific local CA;
client trust or a public-domain certificate must be configured by the operator.

## Documentation

| Guide | Contents |
| --- | --- |
| [Installation](docs/installation.md) | Setup, model requirements, configuration, HTTPS, verification, and operations |
| [Docker image](docs/docker_image.md) | Download, configure, run, and update a prebuilt image |
| [Usage](docs/usage.md) | Courses, materials, rules, analysis, reports, and conversations |
| [API](docs/api.md) | Application routes, management CLI, and external interfaces |
| [Rule framework](docs/prompt_rule_framework.md) | Rule contracts, deterministic evaluation, prompts, and supporting research |

## Development

The application uses FastAPI, PostgreSQL with pgvector, Redis/RQ workers, and
Caddy. Runtime code and resources live in `app/`; database migrations are in
`migrations/`, tests in `tests/`, and documentation in `docs/`.

With Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --frozen
uv run pytest
```

Tests use SQLite and simulated model responses; they do not require live model
servers. The release workflow runs tests only when a version tag is pushed,
not on ordinary commits or branch pushes. Update or remove obsolete tests
alongside the behavior they cover. Validate deployments with the real endpoints. Rebuild a
source installation after code changes with `./setup.sh`.

## Project and licensing

Developed within **HES-SO R&I Mind the Gap: Adapting Higher Education to the Age
of AI (MTG, 140012/RI-PSTRAT25-06)**. See the [project website](https://mtg.histlab.hevs.ch/)
for the consortium, publications, and contacts.

| Material | License |
| --- | --- |
| Source code | [MIT](LICENSE) |
| Documentation | [CC BY 4.0](LICENSE-docs) |
| Distributed container image | [Apache 2.0](LICENSE-docker) |

Copyright 2026 HES-SO Valais-Wallis.
