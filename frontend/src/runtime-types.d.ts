export interface ContextStatus {
  estimated: boolean | null
  estimated_input_tokens: number | null
  context_window_tokens: number | null
  max_input_tokens: number | null
  reserved_output_tokens: number | null
  safety_margin_tokens: number | null
  estimate_source: string | null
  last_provider_prompt_tokens: number | null
  source_message_count: number | null
  model_visible_message_count: number | null
  memory_entry_count: number | null
  memory_estimated_tokens: number | null
  compact_status: string | null
  compact_covered_message_count: number | null
  compressed_tool_result_count: number | null
}

export type CompactResultStatus = "compacted" | "skipped" | "failed"

export interface CompactResult {
  status: CompactResultStatus
  reason: string | null
  before: ContextStatus | null
  after: ContextStatus | null
}

export type RuntimeControl = "context_status" | "compact"
