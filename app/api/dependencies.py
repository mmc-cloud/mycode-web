from dataclasses import dataclass

from fastapi import HTTPException, Request, Response

from app.db.database import WebSession, WebUser
from app.services.container import AppServices


@dataclass(frozen=True)
class WebContext:
    user: WebUser
    session: WebSession


def services(request: Request) -> AppServices:
    return request.app.state.services


def current_user(request: Request, response: Response) -> WebUser:
    app_services = services(request)
    candidate = request.cookies.get(app_services.settings.cookie_name)
    user, _created = app_services.database.get_or_create_user(candidate)
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
    return user


def current_context(
    session_id: str,
    request: Request,
    response: Response,
) -> WebContext:
    user = current_user(request, response)
    session = services(request).database.get_session(session_id, user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return WebContext(user=user, session=session)
