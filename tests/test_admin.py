import pytest
from sqlalchemy import select

from app.models import Course, Document, PageEmbedding, Role, User
from app.services import storage
from tests.conftest import _create_user, login, make_client, make_pdf, upload_pdf


def test_admin_list_excludes_administrators(client, db, admin, member):
    other = _create_user(db, 'second-admin@example.com', Role.ADMIN)
    login(client, admin.email)
    page = client.get('/admin')
    assert member.email in page.text
    assert other.email not in page.text
    assert f'/admin/users/{admin.id}/toggle' not in page.text
    assert f'/admin/users/{other.id}/delete' not in page.text
    assert 'Create user' in page.text


@pytest.mark.parametrize('action', ['toggle', 'delete'])
def test_admin_accounts_are_protected(client, db, admin, action):
    other = _create_user(db, 'second-admin@example.com', Role.ADMIN)
    login(client, admin.email)
    for target in (admin, other):
        response = client.post(f'/admin/users/{target.id}/{action}')
        assert 'Administrator accounts cannot be' in response.text
        db.refresh(target)
        assert target.is_active


@pytest.mark.parametrize('action', ['toggle', 'delete'])
def test_missing_user_returns_alert(client, admin, action):
    login(client, admin.email)
    response = client.post(f'/admin/users/99999/{action}')
    assert response.status_code == 200
    assert 'User not found.' in response.text


@pytest.mark.parametrize('authenticated', [False, True])
def test_only_admin_can_delete_users(client, db, member, authenticated):
    if authenticated:
        login(client, member.email)
    response = client.post(f'/admin/users/{member.id}/delete', follow_redirects=False)
    assert response.status_code == 303
    assert db.get(User, member.id) is not None


def test_delete_user_removes_courses_files_and_revokes_session(client, db, admin, member, course):
    member_id, course_id = member.id, course.id
    other = _create_user(db, 'other@example.com', Role.USER)
    surviving = Course(owner_id=other.id, title='Keep this course')
    db.add(surviving)
    db.commit()
    surviving_id = surviving.id
    account = make_client()
    login(account, member.email)
    upload_pdf(account, course_id, 'test.pdf', make_pdf(['Synthetic document']))
    files = storage.abs_path(f'courses/{course_id}')
    assert files.exists()
    login(client, admin.email)
    response = client.post(f'/admin/users/{member_id}/delete')
    assert response.status_code == 200
    assert 'User user@example.com deleted.' in response.text
    db.expire_all()
    assert db.get(User, member_id) is None
    assert db.get(Course, course_id) is None
    assert db.scalars(select(Document)).all() == []
    assert db.scalars(select(PageEmbedding)).all() == []
    assert not files.exists()
    assert db.get(Course, surviving_id) is not None
    assert account.get('/courses', follow_redirects=False).headers['location'] == '/login'
    assert login(account, 'user@example.com').status_code == 200


def test_enable_and_delete_disabled_user(client, db, admin, member):
    member_id = member.id
    login(client, admin.email)
    for active in (False, True, False):
        client.post(f'/admin/users/{member_id}/toggle')
        db.refresh(member)
        assert member.is_active is active
    response = client.post(f'/admin/users/{member_id}/delete')
    assert 'No users yet.' in response.text
    db.expire_all()
    assert db.get(User, member_id) is None
