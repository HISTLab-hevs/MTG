"""Sphinx configuration for the MTG API reference.

Documents the app package only: everything under app/ is the system itself,
while the repository's other top-level directories are research material,
deployment glue and one-off scripts that no reader of this reference needs.

The pages are generated from the docstrings in the source, so the source stays
the single place a behaviour is described.
"""

import os
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# app.db opens an engine and app.templating builds a Jinja environment at import
# time, and autodoc has to import both. SQLite needs no server, so pointing the
# settings at a throwaway file lets every module import on a machine with no
# Postgres and no Redis running.
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("STORAGE_DIR", str(REPO_ROOT / ".docs-build-storage"))

project = "MTG · Mind The Gap"
author = "Valerio Crocetti"
copyright = "2026, HES-SO Valais-Wallis"
release = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

# Google-style docstrings, the format the codebase is written in. NumPy parsing
# is off so a stray section header can never be read as the other dialect.
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True
napoleon_use_admonition_for_notes = True
# Render an Attributes: section as field-list entries rather than as separate
# object descriptions. Autodoc already emits one description per attribute, so
# the default would document, and index, each of them twice.
napoleon_use_ivar = True

autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
}
# Signatures carry the annotations; repeating them in the parameter list would
# duplicate what the Args section already describes in words.
autodoc_typehints = "signature"
autodoc_preserve_defaults = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "sqlalchemy": ("https://docs.sqlalchemy.org/en/20/", None),
    "fastapi": ("https://fastapi.tiangolo.com/", None),
}
# A missing objects.inv must not fail the build on a machine with no network.
intersphinx_disabled_reftypes = ["*"]

templates_path = ["_templates"]
exclude_patterns = []

html_theme = "furo"
html_title = f"{project} {release}"
html_static_path = []

# A warning here is a docstring the reader will not see rendered correctly, so
# the build treats them as work to do rather than as noise.
nitpicky = False


def setup(app):
    """Import the whole application before autodoc starts.

    Two reasons, both about import order. Documenting Base's members makes
    SQLAlchemy initialise its registry, after which a model module imported for
    the first time fails to map; and FastAPI's own Pydantic models must be built
    before autodoc imports a router. Importing everything once, here, leaves
    both settled by the time the first page is generated.

    Doing it in this hook rather than at module level matters: Sphinx has
    finished loading its extensions by now, so nothing is left half-imported.
    """
    import app.main  # noqa: F401
    import app.models  # noqa: F401
    import app.templating  # noqa: F401
