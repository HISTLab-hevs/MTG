"""Admin area: create, enable, disable and delete regular accounts.

Every endpoint is behind require_admin and answers with a full page or, for the
htmx posts, with the users panel alone.
"""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_admin
from app.models import Course, Role, User
from app.services.courses import delete_course
from app.security import hash_password, is_valid_email, normalize_email
from app.templating import templates

router = APIRouter(prefix="/admin")


def _users(db: Session) -> list[User]:
    return list(
        db.scalars(select(User).where(User.role != Role.ADMIN).order_by(User.created_at))
    )


@router.get("")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Render the admin dashboard with the account list."""
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {"user": user, "users": _users(db), "alert": None},
    )


@router.post("/users")
def create_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Create a regular account, re-rendering the panel with the outcome.

    A rejected form (bad email, short password, address already taken) comes
    back as an error alert rather than an HTTP error, since the panel is
    swapped in place by htmx.
    """
    email = normalize_email(email)
    alert = None
    if not is_valid_email(email):
        alert = ("error", "Please provide a valid email address.")
    elif len(password) < 8:
        alert = ("error", "The password must be at least 8 characters long.")
    elif db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        alert = ("error", f"A user with email {email} already exists.")
    else:
        db.add(
            User(
                email=email,
                password_hash=hash_password(password),
                first_name=first_name.strip(),
                last_name=last_name.strip(),
                role=Role.USER,
            )
        )
        db.commit()
        alert = ("success", f"User {email} created.")
    return templates.TemplateResponse(
        request,
        "admin/_users_panel.html",
        {"user": user, "users": _users(db), "alert": alert},
    )


@router.post("/users/{user_id}/toggle")
def toggle_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Flip a regular account's active flag."""
    target = db.get(User, user_id)
    alert = None
    if target is None:
        alert = ("error", "User not found.")
    elif target.is_admin:
        alert = ("error", "Administrator accounts cannot be disabled.")
    else:
        target.is_active = not target.is_active
        db.commit()
        state = "enabled" if target.is_active else "disabled"
        alert = ("success", f"User {target.email} {state}.")
    return templates.TemplateResponse(
        request,
        "admin/_users_panel.html",
        {"user": user, "users": _users(db), "alert": alert},
    )


@router.post("/users/{user_id}/delete")
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    target = db.get(User, user_id)
    if target is None:
        alert = ("error", "User not found.")
    elif target.is_admin:
        alert = ("error", "Administrator accounts cannot be deleted.")
    else:
        email = target.email
        for course in list(db.scalars(select(Course).where(Course.owner_id == target.id))):
            delete_course(db, course)
        db.delete(target)
        db.commit()
        alert = ("success", f"User {email} deleted.")
    return templates.TemplateResponse(
        request,
        "admin/_users_panel.html",
        {"user": user, "users": _users(db), "alert": alert},
    )
