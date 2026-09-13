// Wire types mirroring api/app/schemas/*.py. UUIDs and datetimes are strings over
// the wire; field names are the *serialized* names (see UsageSummary.from/to).

export interface ApiErrorEnvelope {
  error: {
    message: string;
    type: "server_error" | "invalid_request_error" | "rate_limit_error";
    code: string | null;
  };
}

/** GET /api/me — api/app/schemas/me.py */
export interface Me {
  organization_id: string;
  user_id: string;
  plan_code: string;
  requests_per_minute: number;
  max_concurrency: number;
  models: string[];
}

/** GET /v1/models — api/app/routes/models.py */
export interface Model {
  id: string;
  object: "model";
  created: number;
  owned_by: string;
}

export interface ModelList {
  object: "list";
  data: Model[];
}

/** GET /api/usage — api/app/schemas/usage.py (`since`/`until` serialize as `from`/`to`) */
export interface UsageGroup {
  key: string | null;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface UsageSummary {
  organization_id: string;
  from: string;
  to: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  groups: UsageGroup[];
}

/** POST /api/keys — api/app/schemas/admin.py. `key` is the raw secret, returned once. */
export interface KeyOut {
  id: string;
  key: string;
  prefix: string;
  created_at: string;
}

/** GET /api/keys — api/app/schemas/keys.py */
export interface KeyInfo {
  id: string;
  prefix: string;
  created_at: string;
  revoked_at: string | null;
}

export interface KeyList {
  object: "list";
  data: KeyInfo[];
}

/** POST /v1/chat/completions — api/app/schemas/chat.py */
export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ChatRequest {
  model: string;
  messages: ChatMessage[];
  stream?: boolean;
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
}

export interface UsageOut {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface ChatChoice {
  index: number;
  message: ChatMessage;
  finish_reason: string;
}

export interface ChatCompletion {
  id: string;
  object: "chat.completion";
  created: number;
  model: string;
  choices: ChatChoice[];
  usage: UsageOut;
}

export interface ChunkChoice {
  index: number;
  delta: { role?: string; content?: string };
  finish_reason: string | null;
}

export interface ChatCompletionChunk {
  id: string;
  object: "chat.completion.chunk";
  created: number;
  model: string;
  choices: ChunkChoice[];
  usage: UsageOut | null;
}
