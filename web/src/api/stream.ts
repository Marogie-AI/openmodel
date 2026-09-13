import { ApiError, readRateLimit, type RateLimit } from "./client";
import type { ApiErrorEnvelope, ChatCompletionChunk, ChatRequest } from "./types";

/**
 * The backend's SSE framing (api/app/sse.py): every frame is `data: <json>\n\n`
 * and the stream ends with `data: [DONE]\n\n`. There is no `event:` line, no id
 * and no heartbeat. An in-stream failure arrives as an ordinary data frame whose
 * JSON is the error envelope — the HTTP status was already 200 by then, so a 200
 * is only provisional and the caller has to watch for that frame.
 */
const DONE = "[DONE]";

export type StreamEvent =
  | { kind: "chunk"; chunk: ChatCompletionChunk }
  | { kind: "error"; error: ApiError };

/** Split a buffer into whole SSE frames, returning the unterminated remainder. */
function takeFrames(buffer: string): { frames: string[]; rest: string } {
  const parts = buffer.split("\n\n");
  // The last piece has no terminator yet, so it stays in the buffer. A frame
  // can straddle two reads, which is the case this whole function exists for.
  const rest = parts.pop() ?? "";
  return { frames: parts, rest };
}

/** `data: {...}` -> the payload. Anything else (blank line, comment) yields null. */
function payload(frame: string): string | null {
  const line = frame.split("\n").find((candidate) => candidate.startsWith("data:"));
  if (line === undefined) return null;
  return line.slice("data:".length).trim();
}

function toStreamError(raw: string): ApiError | null {
  let body: Partial<ApiErrorEnvelope>;
  try {
    body = JSON.parse(raw) as Partial<ApiErrorEnvelope>;
  } catch {
    return null;
  }
  if (!body.error || typeof body.error.message !== "string") return null;
  // The stream already answered 200, so there is no status to report; 200 keeps
  // the distinction visible to anything that inspects it.
  return new ApiError({
    status: 200,
    message: body.error.message,
    type: body.error.type,
    code: body.error.code,
  });
}

export interface StreamStart {
  rateLimit?: RateLimit;
  requestId?: string;
  events: AsyncGenerator<StreamEvent, void, void>;
}

/**
 * POST a streaming chat completion and yield its frames. Rate-limit headers and
 * the request id are available as soon as the response head arrives, before the
 * first token, so they are returned alongside the generator rather than through it.
 */
export async function streamChat(
  body: ChatRequest,
  apiKey: string,
  signal?: AbortSignal,
): Promise<StreamStart> {
  const response = await fetch("/v1/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ ...body, stream: true }),
    ...(signal !== undefined && { signal }),
  });

  const requestId = response.headers.get("X-Request-ID") ?? undefined;
  const rateLimit = readRateLimit(response);

  if (!response.ok) {
    // A pre-stream failure (401, 403, 429, 404) is a normal error response.
    const retryAfterRaw = response.headers.get("Retry-After");
    const retryAfter = retryAfterRaw === null ? undefined : Number.parseInt(retryAfterRaw, 10);
    let message = response.statusText || `HTTP ${response.status}`;
    let type = "server_error";
    let code: string | null = null;
    try {
      const envelope = (await response.json()) as Partial<ApiErrorEnvelope>;
      if (envelope.error && typeof envelope.error.message === "string") {
        message = envelope.error.message;
        type = envelope.error.type;
        code = envelope.error.code;
      }
    } catch {
      /* non-JSON body: the status is still the truth */
    }
    throw new ApiError({
      status: response.status,
      message,
      type,
      code,
      ...(requestId !== undefined && { requestId }),
      ...(retryAfter !== undefined && !Number.isNaN(retryAfter) && { retryAfter }),
    });
  }

  if (response.body === null) throw new Error("the response carried no body to stream");

  return {
    ...(rateLimit !== undefined && { rateLimit }),
    ...(requestId !== undefined && { requestId }),
    events: readFrames(response.body),
  };
}

async function* readFrames(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<StreamEvent, void, void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const { frames, rest } = takeFrames(buffer);
      buffer = rest;
      for (const frame of frames) {
        const event = parse(frame);
        if (event === "done") return;
        if (event !== null) yield event;
      }
    }

    // A last frame with no trailing blank line still counts.
    const event = parse(buffer);
    if (event !== null && event !== "done") yield event;
  } finally {
    reader.releaseLock();
  }
}

function parse(frame: string): StreamEvent | "done" | null {
  const raw = payload(frame);
  if (raw === null || raw === "") return null;
  if (raw === DONE) return "done";

  const error = toStreamError(raw);
  if (error !== null) return { kind: "error", error };

  try {
    return { kind: "chunk", chunk: JSON.parse(raw) as ChatCompletionChunk };
  } catch {
    // A frame we cannot parse is worth surfacing, not swallowing.
    return {
      kind: "error",
      error: new ApiError({
        status: 200,
        message: `unparseable stream frame: ${raw.slice(0, 120)}`,
        type: "server_error",
        code: null,
      }),
    };
  }
}
