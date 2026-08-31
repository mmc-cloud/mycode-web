from pydantic import BaseModel, Field


class ProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class PermissionDecisionRequest(BaseModel):
    allow: bool


class UserResponse(BaseModel):
    display_name: str | None


class SessionResponse(BaseModel):
    display_name: str | None
    created_at: str
    last_active_at: str
    runtime_status: str


class FileTreeEntry(BaseModel):
    name: str
    path: str
    kind: str
    size: int | None = None
    children: list["FileTreeEntry"] | None = None
