"""Sign-in, sign-out, and the three static information pages.

The information pages take an optional course_id: when it names a course the
user owns, the course's nav tabs stay in the sidebar so the professor does not
lose their place.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import project_info, rule_sources
from app.db import get_db
from app.deps import get_current_user, require_member
from app.models import User
from app.security import SESSION_COOKIE, set_session_cookie, verify_password
from app.templating import templates

router = APIRouter()


def _home_url(user: User) -> str:
    """Landing page for a signed-in account, by role."""
    return "/admin" if user.is_admin else "/courses"


@router.get("/")
def root(user: User | None = Depends(get_current_user)):
    """Send a visitor to their home page, or to the login form."""
    return RedirectResponse(_home_url(user) if user else "/login", status_code=303)


@router.get("/login")
def login_page(request: Request, user: User | None = Depends(get_current_user)):
    """Show the login form, or skip it when already signed in."""
    if user:
        return RedirectResponse(_home_url(user), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Verify the credentials and open a session.

    An unknown address and a wrong password give the same message, so the form
    does not reveal which addresses are registered.
    """
    user = db.execute(
        select(User).where(User.email == email.strip().lower())
    ).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password."}
        )
    if not user.is_active:
        return templates.TemplateResponse(
            request, "login.html", {"error": "This account has been disabled."}
        )

    response = RedirectResponse(_home_url(user), status_code=303)
    set_session_cookie(response, user.id)
    return response


@router.get("/how-it-works")
def how_it_works(
    request: Request,
    course_id: int | None = None,
    user: User = Depends(require_member),
    db: Session = Depends(get_db),
):
    """Guide for professors: what the tool does and how to read each screen.

    Admins are redirected to their own area by require_member.

    Args:
        course_id: Course to keep in the sidebar; ignored unless the user owns
            it.
    """
    from app.models import Course

    course = None
    if course_id is not None:
        candidate = db.get(Course, course_id)
        if candidate is not None and candidate.owner_id == user.id:
            course = candidate
    return templates.TemplateResponse(
        request,
        "how_it_works.html",
        {"user": user, "course": course, "active_tab": "help"},
    )


@router.get("/about")
def about(
    request: Request,
    course_id: int | None = None,
    user: User = Depends(require_member),
    db: Session = Depends(get_db),
):
    """Who is behind the project and where to read more.

    Args:
        course_id: Course to keep in the sidebar; ignored unless the user owns
            it.
    """
    from app.models import Course

    course = None
    if course_id is not None:
        candidate = db.get(Course, course_id)
        if candidate is not None and candidate.owner_id == user.id:
            course = candidate
    return templates.TemplateResponse(
        request,
        "about.html",
        {
            "user": user,
            "course": course,
            "teams": project_info.TEAMS,
            "organizations": project_info.ORGANIZATIONS,
            "contacts": project_info.CONTACTS,
            "abstract": project_info.ABSTRACT,
            "site_url": project_info.SITE_URL,
            "active_tab": "about",
        },
    )


@router.get("/evidence")
def evidence(
    request: Request,
    course_id: int | None = None,
    user: User = Depends(require_member),
    db: Session = Depends(get_db),
):
    """What the default rules are based on: the focus groups and the literature.

    Reached from the reference marks beside each default rule, which link to the
    entry they cite (/evidence#ref-3), so a professor can go from "why is this
    rule here?" straight to the evidence and back.

    Args:
        course_id: Course to keep in the sidebar; ignored unless the user owns
            it.
    """
    from app.models import Course

    course = None
    if course_id is not None:
        candidate = db.get(Course, course_id)
        if candidate is not None and candidate.owner_id == user.id:
            course = candidate
    return templates.TemplateResponse(
        request,
        "evidence.html",
        {
            "user": user,
            "course": course,
            "sources": rule_sources.numbered_sources(),
            "active_tab": "evidence",
        },
    )


@router.post("/logout")
def logout():
    """Clear the session cookie and return to the login form."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
