from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


class SessionRename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


PermissionChoice = Literal["deny", "once", "task", "session"]


class PermissionDecisionRequest(BaseModel):
    decision: PermissionChoice
    request_id: str = Field(min_length=1, max_length=200)


class MCPTrustDecisionRequest(BaseModel):
    approved: bool
    request_id: str = Field(min_length=1, max_length=200)


class ContextStatusResponse(BaseModel):
    estimated: bool | None = None
    estimated_input_tokens: int | None = None
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    reserved_output_tokens: int | None = None
    safety_margin_tokens: int | None = None
    estimate_source: str | None = None
    last_provider_prompt_tokens: int | None = None
    source_message_count: int | None = None
    model_visible_message_count: int | None = None
    memory_entry_count: int | None = None
    memory_estimated_tokens: int | None = None
    compact_status: str | None = None
    compact_covered_message_count: int | None = None
    compressed_tool_result_count: int | None = None


class CompactResultResponse(BaseModel):
    status: str
    reason: str | None = None
    before: ContextStatusResponse | None = None
    after: ContextStatusResponse | None = None


class UserResponse(BaseModel):
    display_name: str | None


class SessionResponse(BaseModel):
    id: str
    name: str | None = None
    created_at: str
    last_active_at: str
    runtime_status: str
    active_turn_id: str | None = None
    pending_permission: dict[str, object] | None = None
    pending_mcp_trust: dict[str, object] | None = None
    pending_control: str | None = None
    context_status: ContextStatusResponse | None = None
    # Detail responses use this as the SSE bootstrap cursor. List/create
    # responses leave it unset because they are not bootstrap snapshots.
    event_cursor: int | None = None


class SessionListResponse(BaseModel):
    display_name: str | None
    sessions: list[SessionResponse]
    has_created_session: bool = False


class ConsoleEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    content: str
    data: dict[str, object]
    created_at: str


class ConsoleSnapshotResponse(BaseModel):
    events: list[ConsoleEventResponse]


class FileTreeEntry(BaseModel):
    name: str
    path: str
    kind: str
    size: int | None = None
    children: list["FileTreeEntry"] | None = None
