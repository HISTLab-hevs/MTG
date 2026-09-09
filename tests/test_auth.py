"""Login, session guards, and the split between the admin and course areas.

Each role is redirected to its own home, and neither can reach the other's
pages; an htmx request that hits a guard gets a header, not a rendered form.
"""

from sqlalchemy import select

from app.models import User
from tests.conftest import login, make_client


def test_login_success_redirects_by_role(client, admin, member):
    response = login(client, "admin@example.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"

    response = login(client, "user@example.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/courses"


def test_login_wrong_password(client, member):
    response = login(client, "user@example.com", "wrong-password")
    assert response.status_code == 200
    assert "Invalid email or password" in response.text


def test_anonymous_is_redirected_to_login(client):
    response = client.get("/courses", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_htmx_request_when_logged_out_gets_hx_redirect_not_login_html(client, course):
    """An htmx request that hits an auth guard (e.g. an expired session) must get an
    HX-Redirect header, not the login page swapped into the fragment — otherwise the
    whole login form renders inside, e.g., the materials chat."""
    response = client.post(
        f"/courses/{course.id}/materials/chat",
        data={"message": "hi", "history": "[]"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/login"
    assert "Password" not in response.text and "Sign in" not in response.text


def test_browser_navigation_when_logged_out_still_redirects(client):
    """A normal (non-htmx) navigation keeps the ordinary 303 redirect to login."""
    response = client.get("/courses", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_how_it_works_requires_login(client):
    response = client.get("/how-it-works", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_how_it_works_not_for_admin(client, admin):
    login(client, "admin@example.com")
    response = client.get("/how-it-works", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_how_it_works_renders_for_member(client, member):
    login(client, "user@example.com")
    response = client.get("/how-it-works")
    assert response.status_code == 200
    assert "How it works" in response.text
    # The iterate-and-improve loop is the page's key message.
    assert "re-run" in response.text.lower()
    assert "score rise" in response.text.lower()


def test_how_it_works_keeps_course_nav_when_in_a_course(client, db, member):
    from app.models import Course

    login(client, "user@example.com")
    course = Course(owner_id=member.id, title="Concurrency 101")
    db.add(course)
    db.commit()
    response = client.get(f"/how-it-works?course_id={course.id}")
    assert response.status_code == 200
    assert f"/courses/{course.id}/materials" in response.text
    assert f"/courses/{course.id}/rules" in response.text
    assert "Concurrency 101" in response.text


def test_how_it_works_ignores_course_of_another_user(client, db, admin, member):
    from app.models import Course

    other = Course(owner_id=admin.id, title="Not yours")
    db.add(other)
    db.commit()
    login(client, "user@example.com")
    response = client.get(f"/how-it-works?course_id={other.id}")
    assert response.status_code == 200
    # No course tabs leak for a course the member does not own.
    assert f"/courses/{other.id}/materials" not in response.text


def test_member_cannot_access_admin_area(client, member):
    login(client, "user@example.com")
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/courses"


def test_admin_cannot_access_course_area(client, admin):
    login(client, "admin@example.com")
    response = client.get("/courses", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_admin_creates_user_who_can_login(client, db, admin):
    login(client, "admin@example.com")
    response = client.post(
        "/admin/users",
        data={
            "email": "new.prof@example.com",
            "password": "password123",
            "first_name": "New",
            "last_name": "Prof",
        },
    )
    assert response.status_code == 200
    created = db.execute(
        select(User).where(User.email == "new.prof@example.com")
    ).scalar_one()
    assert created.role == "user"

    other = make_client()
    assert login(other, "new.prof@example.com").headers["location"] == "/courses"


def test_disabled_user_cannot_login(client, db, admin, member):
    login(client, "admin@example.com")
    client.post(f"/admin/users/{member.id}/toggle")
    db.refresh(member)
    assert member.is_active is False

    other = make_client()
    response = login(other, "user@example.com")
    assert response.status_code == 200
    assert "disabled" in response.text
