"""The FastAPI application: middleware, static files and the routers.

Everything is server-rendered HTML with htmx; there is no JSON API, so the
error handling below is about getting a browser to the right page rather than
returning a status a client would parse.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.routers import admin, auth, courses, documents, rules
from app.security import SESSION_COOKIE, read_session_token, set_session_cookie

app = FastAPI(title="MTG - Mind The Gap")


@app.exception_handler(StarletteHTTPException)
async def auth_redirect_handler(request: Request, exc: StarletteHTTPException):
    """Send an auth redirect where the caller can actually follow it.

    A browser navigation gets the plain 303. An htmx request must not have the
    login page swapped into a fragment, which would render the whole login form
    inside the chat, so it gets an HX-Redirect header instead and htmx navigates
    the whole page.
    """
    location = exc.headers.get("Location") if exc.headers else None
    if exc.status_code == 303 and location:
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=204, headers={"HX-Redirect": location})
        return RedirectResponse(location, status_code=303)
    from fastapi.exception_handlers import http_exception_handler

    return await http_exception_handler(request, exc)


@app.middleware("http")
async def enforce_https(request: Request, call_next):
    """Redirect a plain-HTTP request to HTTPS when https_only is set.

    Caddy already redirects at the edge, so this only fires if a proxy forwards
    HTTP straight through. The scheme comes from X-Forwarded-Proto, which
    uvicorn honours only because it runs with --proxy-headers.
    """
    if get_settings().https_only and request.url.scheme == "http":
        return RedirectResponse(
            request.url.replace(scheme="https"), status_code=307
        )
    return await call_next(request)


@app.middleware("http")
async def sliding_session(request: Request, call_next):
    """Re-issue the session cookie on activity, so the idle timeout slides.

    Skipped when the endpoint already set or cleared the cookie itself, which is
    what keeps a logout from being undone here.
    """
    response = await call_next(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token and "set-cookie" not in response.headers:
        user_id = read_session_token(token)
        if user_id is not None:
            set_session_cookie(response, user_id)
    return response


app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(documents.router)
app.include_router(rules.router)
app.include_router(courses.router)
