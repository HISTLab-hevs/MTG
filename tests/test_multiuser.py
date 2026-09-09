"""Two professors working at the same time must stay fully isolated: each sees
only their own courses, documents, and reports, and interleaved uploads/analyses
do not bleed across users or sessions.

These use two independent authenticated clients with interleaved requests (the
realistic shape of "two people using it at once"); the eager job runner makes each
upload/analysis complete inline so the assertions are deterministic.
"""

from app.models import Course, Role, User
from app.security import hash_password
from tests.conftest import login, make_client, make_pdf, upload_pdf


def _user(db, email):
    u = User(
        email=email, password_hash=hash_password("password123"),
        first_name="P", last_name="Q", role=Role.USER,
    )
    db.add(u)
    db.commit()
    return u


def _client_for(email):
    c = make_client()
    login(c, email)
    return c


def test_two_users_uploads_stay_isolated(db):
    alice = _user(db, "alice@example.com")
    bob = _user(db, "bob@example.com")
    a_course = Course(owner_id=alice.id, title="Alice OS")
    b_course = Course(owner_id=bob.id, title="Bob Networks")
    db.add_all([a_course, b_course])
    db.commit()
    a_id, b_id = a_course.id, b_course.id

    ca = _client_for("alice@example.com")
    cb = _client_for("bob@example.com")

    # Interleave the two professors' uploads.
    upload_pdf(ca, a_id, "alice1.pdf", make_pdf(["Alice page"]), "slide")
    upload_pdf(cb, b_id, "bob1.pdf", make_pdf(["Bob page"]), "slide")
    upload_pdf(ca, a_id, "alice2.pdf", make_pdf(["Alice two"]), "slide")

    a_view = ca.get(f"/courses/{a_id}/materials").text
    assert "alice1.pdf" in a_view and "alice2.pdf" in a_view
    assert "bob1.pdf" not in a_view

    b_view = cb.get(f"/courses/{b_id}/materials").text
    assert "bob1.pdf" in b_view
    assert "alice1.pdf" not in b_view


def test_user_cannot_reach_another_users_course(db):
    alice = _user(db, "alice@example.com")
    _user(db, "bob@example.com")
    a_course = Course(owner_id=alice.id, title="Alice OS")
    db.add(a_course)
    db.commit()

    cb = _client_for("bob@example.com")
    assert cb.get(f"/courses/{a_course.id}/materials").status_code == 404
    resp = upload_pdf(cb, a_course.id, "x.pdf", make_pdf(["x"]), "slide")
    assert resp.status_code == 404


def test_concurrent_analysis_runs_stay_per_course(db):
    """Two users run analysis on their own courses at the same time; each run is
    scoped to its course and neither errors, with verdicts kept per course."""
    from app.models import CourseRule, ReportRun, RuleEvaluation
    from app.services.rules import ensure_default_rules

    alice = _user(db, "alice@example.com")
    bob = _user(db, "bob@example.com")
    a_course = Course(owner_id=alice.id, title="Alice OS")
    b_course = Course(owner_id=bob.id, title="Bob Networks")
    db.add_all([a_course, b_course])
    db.commit()
    a_id, b_id = a_course.id, b_course.id
    # Both courses get rules so analysis produces verdicts.
    ensure_default_rules(db, a_course)
    ensure_default_rules(db, b_course)
    db.commit()

    ca = _client_for("alice@example.com")
    cb = _client_for("bob@example.com")
    upload_pdf(ca, a_id, "alice.pdf", make_pdf(["Alice content"]), "slide")
    upload_pdf(cb, b_id, "bob.pdf", make_pdf(["Bob content"]), "slide")

    # Run both analyses, interleaved; neither should error.
    assert ca.post(f"/courses/{a_id}/report/analyze",
                   follow_redirects=False).status_code in (200, 303)
    assert cb.post(f"/courses/{b_id}/report/analyze",
                   follow_redirects=False).status_code in (200, 303)

    # Every evaluation belongs to a rule of the course it was produced for: no
    # verdict from Alice's run is attached to Bob's course, or vice versa.
    a_rule_ids = {r.id for r in db.query(CourseRule).filter_by(course_id=a_id)}
    b_rule_ids = {r.id for r in db.query(CourseRule).filter_by(course_id=b_id)}
    assert a_rule_ids and b_rule_ids and not (a_rule_ids & b_rule_ids)

    a_runs = db.query(ReportRun).filter_by(course_id=a_id).all()
    b_runs = db.query(ReportRun).filter_by(course_id=b_id).all()
    assert a_runs and b_runs

    evals = db.query(RuleEvaluation).all()
    assert evals  # analysis actually produced verdicts
    for e in evals:
        # Each evaluation's rule belongs to exactly one of the two courses.
        assert (e.rule_id in a_rule_ids) ^ (e.rule_id in b_rule_ids)


def test_analyze_does_not_duplicate_an_active_run(db):
    """A second analyze POST while a run is still active (a double-submit or a
    stale tab) must not create a second run and steal a worker slot."""
    from app.models import ReportRun

    alice = _user(db, "dup@example.com")
    course = Course(owner_id=alice.id, title="Dup Course")
    db.add(course)
    db.commit()
    cid = course.id
    # Simulate a run that a worker has already picked up (so eager-mode does not
    # complete it inline before the second POST is evaluated).
    db.add(ReportRun(course_id=cid, status="running"))
    db.commit()

    client = _client_for("dup@example.com")
    resp = client.post(f"/courses/{cid}/report/analyze", follow_redirects=False)
    assert resp.status_code in (200, 303)

    runs = db.query(ReportRun).filter_by(course_id=cid).all()
    assert len(runs) == 1  # no duplicate enqueued


def test_queued_run_reports_how_many_are_ahead(db):
    """A still-pending run behind older active runs shows an honest queue depth."""
    from app.models import ReportRun
    from app.routers.courses import _queue_position

    owner = _user(db, "queue@example.com")
    course = Course(owner_id=owner.id, title="Queued Course")
    ahead = [
        Course(owner_id=owner.id, title="Ahead One"),
        Course(owner_id=owner.id, title="Ahead Two"),
    ]
    db.add_all([course, *ahead])
    db.commit()
    # Two older active runs (other courses) sit ahead of ours.
    older = [ReportRun(course_id=c.id, status="running") for c in ahead]
    db.add_all(older)
    db.commit()
    mine = ReportRun(course_id=course.id, status="pending")
    db.add(mine)
    db.commit()

    assert _queue_position(db, mine) == 2
    # A completed run is never "in line".
    mine.status = "completed"
    db.commit()
    assert _queue_position(db, mine) == 0
