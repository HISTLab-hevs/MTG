"""FastAPI dependencies for authentication and ownership.

An unmet requirement raises a 303 rather than a 401: the app is server-rendered,
so the caller is a browser that should land on a page it can act on. main.py
turns that into an HX-Redirect for an htmx request.
"""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Course, User
from app.security import SESSION_COOKIE, read_session_token


def _redirect(url: str) -> HTTPException:
    """A 303 to another page, raised as an exception from a dependency."""
    return HTTPException(status_code=303, headers={"Location": url})


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The signed-in account, or None.

    Returns None for every failure mode alike, an absent, malformed or expired
    cookie and a disabled or deleted account, so no caller has to tell them
    apart.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = read_session_token(token)
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_login(user: User | None = Depends(get_current_user)) -> User:
    """The signed-in account, redirecting to the login page when there is none."""
    if user is None:
        raise _redirect("/login")
    return user


def require_admin(user: User = Depends(require_login)) -> User:
    """Admins only: a normal user is sent back to their courses."""
    if not user.is_admin:
        raise _redirect("/courses")
    return user


def require_member(user: User = Depends(require_login)) -> User:
    """Normal users only: admins are kept inside the admin area."""
    if user.is_admin:
        raise _redirect("/admin")
    return user


def get_owned_course(course_id: int, user: User, db: Session) -> Course:
    """Resolve a course the user owns.

    A course owned by someone else answers 404, not 403: whether it exists is
    itself not this user's business.

    Args:
        course_id: Course to load.
        user: Signed-in professor.
        db: Session to query.

    Returns:
        The course.

    Raises:
        HTTPException: 404 when it does not exist or is not theirs.
    """
    course = db.get(Course, course_id)
    if course is None or course.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Course not found")
    return course
