from dataclasses import dataclass

from fastapi import Request, Response

from app.db.database import WebSession, WebUser
from app.services.container import AppServices


@dataclass(frozen=True)
class WebContext:
    user: WebUser
    session: WebSession


def services(request: Request) -> AppServices:
    return request.app.state.services


def current_context(request: Request, response: Response) -> WebContext:
    app_services = services(request)
    candidate = request.cookies.get(app_services.settings.cookie_name)
    user, _created = app_services.database.get_or_create_user(candidate)
    session = app_services.database.ensure_session(user.id)
    app_services.workspace.ensure_session_directories(session.id)
    response.set_cookie(
        key=app_services.settings.cookie_name,
        value=user.id,
        max_age=app_services.settings.cookie_max_age_seconds,
        expires=app_services.settings.cookie_max_age_seconds,
        httponly=True,
        secure=app_services.settings.cookie_secure,
        samesite="lax",
        path="/mycode",
    )
    return WebContext(user=user, session=session)
