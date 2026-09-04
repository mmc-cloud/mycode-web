from pathlib import Path
import asyncio
import secrets

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import WebContext, current_context, current_user, services
from app.db.database import WebSession, WebUser
from app.models.api import (
    ConsoleEventResponse,
    ConsoleSnapshotResponse,
    MessageRequest,
    PermissionDecisionRequest,
    ProfileUpdate,
    SessionListResponse,
    SessionRename,
    SessionResponse,
    UserResponse,
)
from app.services.events import encode_sse
from app.services.relay import RelayAuthenticationError, RelayConfigurationError
from app.services.runtime import (
    RuntimeCapacityError,
    RuntimeConflictError,
    RuntimeUnavailableError,
)
from app.services.workspace import WorkspaceError, WorkspaceLimitError
from app.services.terminal import TerminalUnavailableError
from app.paths import API_BASE_PATH


router = APIRouter(prefix=API_BASE_PATH)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _session_response(request: Request, session: WebSession) -> SessionResponse:
    runtime = services(request).runtime
    return SessionResponse(
        id=session.id,
        name=session.name,
        created_at=session.created_at,
        last_active_at=session.last_active_at,
        runtime_status=runtime.status(session.id),
        active_turn_id=runtime.active_turn_id(session.id),
        pending_permission=runtime.pending_permission(session.id),
    )


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    request: Request,
    user: WebUser = Depends(current_user),
) -> SessionListResponse:
    return SessionListResponse(
        display_name=user.display_name,
        sessions=[
            _session_response(request, session)
            for session in services(request).database.list_sessions(user.id)
        ],
    )


@router.post("/sessions", response_model=SessionResponse, status_code=201)
def create_session(
    request: Request,
    user: WebUser = Depends(current_user),
) -> SessionResponse:
    app_services = services(request)
    session = app_services.database.create_session(user.id)
    app_services.workspace.ensure_session_directories(session.id, user_id=user.id)
    return _session_response(request, session)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    request: Request,
    context: WebContext = Depends(current_context),
) -> SessionResponse:
    app_services = services(request)
    app_services.database.touch_session(context.session.id)
    session = app_services.database.get_session(context.session.id, context.user.id)
    assert session is not None
    return _session_response(request, session)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    request: Request,
    context: WebContext = Depends(current_context),
) -> None:
    deleted = await services(request).lifecycle.delete_session(
        context.session.id, user_id=context.user.id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found.")


@router.patch("/sessions/{session_id}", response_model=SessionResponse)
def rename_session(
    payload: SessionRename,
    request: Request,
    context: WebContext = Depends(current_context),
) -> SessionResponse:
    try:
        session = services(request).database.update_session_name(
            context.session.id, context.user.id, payload.name
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail="Session not found.") from error
    return _session_response(request, session)


@router.post("/sessions/{session_id}/activate", status_code=202)
async def activate_session(
    request: Request,
    context: WebContext = Depends(current_context),
) -> dict[str, str]:
    app_services = services(request)
    try:
        status = await app_services.runtime.activate(context.session.id)
    except RuntimeCapacityError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except RuntimeUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"status": status}


@router.post("/profile", response_model=UserResponse)
def update_profile(
    payload: ProfileUpdate,
    request: Request,
    user: WebUser = Depends(current_user),
) -> UserResponse:
    user = services(request).database.update_display_name(
        user.id, payload.display_name
    )
    return UserResponse(display_name=user.display_name)


@router.post("/sessions/{session_id}/message", status_code=202)
async def send_message(
    payload: MessageRequest,
    request: Request,
    context: WebContext = Depends(current_context),
) -> dict[str, str]:
    turn_id = secrets.token_urlsafe(16)
    try:
        admission = await services(request).runtime.send_message(
            context.session.id, payload.content, turn_id=turn_id
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeCapacityError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except RuntimeUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"status": admission, "turn_id": turn_id}


@router.websocket("/sessions/{session_id}/terminal")
async def terminal_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    app_services = services(websocket)  # type: ignore[arg-type]
    user_id = websocket.cookies.get(app_services.settings.cookie_name)
    user = app_services.database.get_user(user_id)
    if user is None or app_services.database.get_session(session_id, user.id) is None:
        await websocket.close(code=1008)
        return

    connection = None
    sender: asyncio.Task[None] | None = None
    try:
        connection = await app_services.terminal.attach(session_id)
        sender = asyncio.create_task(_send_terminal_messages(websocket, connection))
        while True:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                continue
            message_type = payload.get("type")
            if message_type == "input" and isinstance(payload.get("data"), str):
                await app_services.terminal.input(connection, payload["data"])
            elif message_type == "resize":
                await app_services.terminal.resize(
                    connection, payload.get("cols"), payload.get("rows")
                )
    except WebSocketDisconnect:
        pass
    except TerminalUnavailableError as error:
        if not websocket.client_state.name == "DISCONNECTED":
            await websocket.send_json(
                {"type": "status", "status": "error", "message": str(error)}
            )
            await websocket.close(code=1011)
    finally:
        if sender is not None:
            if not sender.done():
                sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        if connection is not None:
            await app_services.terminal.detach(connection)


async def _send_terminal_messages(websocket: WebSocket, connection) -> None:
    while True:
        message = await connection.messages.get()
        await websocket.send_json(message)
        if message.get("type") == "status" and message.get("status") in {
            "closed", "error"
        }:
            await websocket.close(
                code=1011 if message.get("status") == "error" else 1000
            )
            return


@router.get("/sessions/{session_id}/events")
async def events(
    request: Request,
    context: WebContext = Depends(current_context),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    try:
        cursor = (
            int(last_event_id)
            if last_event_id is not None
            else after
            if after is not None
            else services(request).events.latest_id(context.session.id)
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID.") from error

    async def body():
        async for event in services(request).events.stream(
            context.session.id, cursor
        ):
            if await request.is_disconnected():
                return
            yield encode_sse(event)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/sessions/{session_id}/console",
    response_model=ConsoleSnapshotResponse,
)
def console_history(
    request: Request,
    context: WebContext = Depends(current_context),
) -> ConsoleSnapshotResponse:
    app_services = services(request)
    event_cursor = app_services.events.latest_id(context.session.id)
    events = list(
        app_services.database.console_history(
            context.session.id, context.user.id
        )
    )
    return ConsoleSnapshotResponse(events=events, event_cursor=event_cursor)


@router.post("/sessions/{session_id}/permission")
async def permission(
    payload: PermissionDecisionRequest,
    request: Request,
    context: WebContext = Depends(current_context),
) -> dict[str, str]:
    try:
        await services(request).runtime.resolve_permission(
            context.session.id, payload.allow
        )
    except RuntimeConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"status": "resolved"}


@router.post("/sessions/{session_id}/files/upload", status_code=201)
async def upload_file(
    request: Request,
    upload: UploadFile = File(...),
    archive: bool = Form(False),
    relative_path: str | None = Form(None),
    context: WebContext = Depends(current_context),
) -> dict[str, str]:
    filename = upload.filename or "upload"
    try:
        await run_in_threadpool(
            services(request).workspace.save_upload,
            context.session.id,
            filename,
            upload.file,
            archive=archive,
            relative_path=relative_path,
            user_id=context.user.id,
        )
    except WorkspaceLimitError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        await upload.close()
    await services(request).events.publish(
        context.session.id, "workspace_changed", changes=[]
    )
    return {"status": "uploaded"}


@router.get("/sessions/{session_id}/files/tree")
async def file_tree(
    request: Request,
    context: WebContext = Depends(current_context),
) -> dict[str, object]:
    try:
        tree = await run_in_threadpool(
            services(request).workspace.tree,
            context.session.id,
            user_id=context.user.id,
        )
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"entries": tree}


@router.get("/sessions/{session_id}/files/content")
async def file_content(
    request: Request,
    path: str = Query(..., min_length=1),
    context: WebContext = Depends(current_context),
) -> dict[str, str]:
    try:
        content = await run_in_threadpool(
            services(request).workspace.read_text,
            context.session.id,
            path,
            user_id=context.user.id,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="File not found.") from error
    except WorkspaceLimitError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"path": path, "content": content}


@router.delete("/sessions/{session_id}/files")
async def delete_path(
    request: Request,
    path: str = Query(..., min_length=1),
    context: WebContext = Depends(current_context),
) -> dict[str, str]:
    try:
        await run_in_threadpool(
            services(request).workspace.delete_path,
            context.session.id,
            path,
            user_id=context.user.id,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="File not found.") from error
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await services(request).events.publish(
        context.session.id, "workspace_changed", changes=[]
    )
    return {"status": "deleted"}


@router.get("/sessions/{session_id}/files/download")
async def download_file(
    request: Request,
    path: str = Query(..., min_length=1),
    context: WebContext = Depends(current_context),
) -> FileResponse:
    try:
        target = services(request).workspace.resolve_file(
            context.session.id, path, user_id=context.user.id
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="File not found.") from error
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return FileResponse(target, filename=target.name)


@router.get("/sessions/{session_id}/workspace/download")
async def download_workspace(
    request: Request,
    context: WebContext = Depends(current_context),
) -> FileResponse:
    try:
        archive = await run_in_threadpool(
            services(request).workspace.build_workspace_zip,
            context.session.id,
            user_id=context.user.id,
        )
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return FileResponse(
        archive,
        filename="workspace.zip",
        media_type="application/zip",
        background=BackgroundTask(_remove_file, archive),
    )


@router.post("/relay/v1/{relay_path:path}", response_model=None)
async def relay(
    relay_path: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    app_services = services(request)
    try:
        app_services.relay.authenticate(authorization)
    except RelayAuthenticationError:
        raise HTTPException(status_code=401, detail="Relay authentication failed.")
    if relay_path != "chat/completions":
        raise HTTPException(status_code=404, detail="Unsupported relay path.")
    try:
        status, headers, chunks = await app_services.relay.forward(
            relay_path,
            await request.body(),
            request.headers.get("content-type"),
        )
    except RelayConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=f"Provider request failed: {type(error).__name__}",
        ) from error
    return StreamingResponse(chunks, status_code=status, headers=headers)


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)
