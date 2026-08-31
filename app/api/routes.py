from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import WebContext, current_context, services
from app.models.api import (
    MessageRequest,
    PermissionDecisionRequest,
    ProfileUpdate,
    SessionResponse,
    UserResponse,
)
from app.services.events import encode_sse
from app.services.relay import RelayAuthenticationError, RelayConfigurationError
from app.services.runtime import RuntimeConflictError, RuntimeUnavailableError
from app.services.workspace import WorkspaceError, WorkspaceLimitError


router = APIRouter(prefix="/mycode/api")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/session", response_model=SessionResponse)
@router.post("/session", response_model=SessionResponse)
def get_session(
    request: Request,
    context: WebContext = Depends(current_context),
) -> SessionResponse:
    runtime_status = services(request).runtime.status(context.session.id)
    return SessionResponse(
        display_name=context.user.display_name,
        created_at=context.session.created_at,
        last_active_at=context.session.last_active_at,
        runtime_status=runtime_status,
    )


@router.post("/profile", response_model=UserResponse)
def update_profile(
    payload: ProfileUpdate,
    request: Request,
    context: WebContext = Depends(current_context),
) -> UserResponse:
    user = services(request).database.update_display_name(
        context.user.id, payload.display_name
    )
    return UserResponse(display_name=user.display_name)


@router.post("/message", status_code=202)
async def send_message(
    payload: MessageRequest,
    request: Request,
    context: WebContext = Depends(current_context),
) -> dict[str, str]:
    try:
        await services(request).runtime.send_message(
            context.session.id, payload.content
        )
    except RuntimeConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"status": "accepted"}


@router.get("/events")
async def events(
    request: Request,
    context: WebContext = Depends(current_context),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    try:
        cursor = int(last_event_id) if last_event_id is not None else after
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


@router.post("/permission")
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


@router.post("/files/upload", status_code=201)
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
        )
    except WorkspaceLimitError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        await upload.close()
    return {"status": "uploaded"}


@router.get("/files/tree")
async def file_tree(
    request: Request,
    context: WebContext = Depends(current_context),
) -> dict[str, object]:
    try:
        tree = await run_in_threadpool(
            services(request).workspace.tree, context.session.id
        )
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"entries": tree}


@router.get("/files/content")
async def file_content(
    request: Request,
    path: str = Query(..., min_length=1),
    context: WebContext = Depends(current_context),
) -> dict[str, str]:
    try:
        content = await run_in_threadpool(
            services(request).workspace.read_text, context.session.id, path
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="File not found.") from error
    except WorkspaceLimitError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"path": path, "content": content}


@router.get("/files/download")
async def download_file(
    request: Request,
    path: str = Query(..., min_length=1),
    context: WebContext = Depends(current_context),
) -> FileResponse:
    try:
        target = services(request).workspace.resolve_file(
            context.session.id, path
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="File not found.") from error
    except WorkspaceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return FileResponse(target, filename=target.name)


@router.get("/workspace/download")
async def download_workspace(
    request: Request,
    context: WebContext = Depends(current_context),
) -> FileResponse:
    try:
        archive = await run_in_threadpool(
            services(request).workspace.build_workspace_zip,
            context.session.id,
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
