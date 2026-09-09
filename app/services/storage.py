"""Local filesystem storage.

The database stores paths relative to STORAGE_DIR so the storage root can be
moved, between a local checkout and a Docker volume for instance, without
rewriting rows.
"""

from pathlib import Path

from app.config import get_settings


def storage_root() -> Path:
    """The configured storage root, created on first use."""
    root = Path(get_settings().storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def abs_path(rel_path: str) -> Path:
    """Resolve a stored relative path against the storage root."""
    return storage_root() / rel_path


def _version_dir(course_id: int, document_id: int, version_number: int) -> str:
    """Relative directory holding one version's files."""
    return f"courses/{course_id}/documents/{document_id}/v{version_number}"


def save_original_pdf(
    course_id: int, document_id: int, version_number: int, data: bytes
) -> str:
    """Store an uploaded PDF.

    Returns:
        Its path relative to the storage root.
    """
    rel = f"{_version_dir(course_id, document_id, version_number)}/original.pdf"
    path = abs_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def save_page_image(
    course_id: int,
    document_id: int,
    version_number: int,
    page_number: int,
    data: bytes,
) -> str:
    """Store one rendered page image.

    Returns:
        Its path relative to the storage root.
    """
    rel = (
        f"{_version_dir(course_id, document_id, version_number)}"
        f"/pages/page_{page_number:04d}.png"
    )
    path = abs_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel
