from pydantic import BaseModel, ConfigDict, Field


class ProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class PermissionDecisionRequest(BaseModel):
    allow: bool


class UserResponse(BaseModel):
    display_name: str | None


class SessionResponse(BaseModel):
    id: str
    created_at: str
    last_active_at: str
    runtime_status: str
    pending_permission: dict[str, object] | None = None


class SessionListResponse(BaseModel):
    display_name: str | None
    sessions: list[SessionResponse]


class ConsoleEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    content: str
    data: dict[str, object]
    created_at: str


class ConsoleSnapshotResponse(BaseModel):
    events: list[ConsoleEventResponse]
    event_cursor: int


class FileTreeEntry(BaseModel):
    name: str
    path: str
    kind: str
    size: int | None = None
    children: list["FileTreeEntry"] | None = None
