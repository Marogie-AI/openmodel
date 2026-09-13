import type { ApiErrorEnvelope } from "./types";

/** The key format the backend enforces (api/app/auth/keys.py). */
export const API_KEY_PATTERN = /^om_[a-z]+_[A-Za-z0-9_-]{12}_[A-Za-z0-9_-]{32}$/;

export function isValidApiKey(key: string): boolean {
  return API_KEY_PATTERN.test(key);
}

export interface RateLimit {
  limit: number;
  remaining: number;
}

export interface ApiResponse<T> {
  data: T;
  rateLimit?: RateLimit;
  requestId?: string;
}

/** A non-2xx response, with the error envelope unpacked. */
export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly code: string | null;
  readonly requestId?: string;
  /** Seconds, from `Retry-After`. Only present on 429. */
  readonly retryAfter?: number;

  constructor(fields: {
    status: number;
    message: string;
    type: string;
    code: string | null;
    requestId?: string;
    retryAfter?: number;
  }) {
    super(fields.message);
    this.name = "ApiError";
    this.status = fields.status;
    this.type = fields.type;
    this.code = fields.code;
    if (fields.requestId !== undefined) this.requestId = fields.requestId;
    if (fields.retryAfter !== undefined) this.retryAfter = fields.retryAfter;
  }
}

function header(response: Response, name: string): string | undefined {
  return response.headers.get(name) ?? undefined;
}

function intHeader(response: Response, name: string): number | undefined {
  const raw = response.headers.get(name);
  if (raw === null) return undefined;
  const value = Number.parseInt(raw, 10);
  return Number.isNaN(value) ? undefined : value;
}

/**
 * `X-RateLimit-*` is only set by routes that ran the limiter; /v1/models,
 * /api/usage and /api/me never set them, so this is optional everywhere.
 */
export function readRateLimit(response: Response): RateLimit | undefined {
  const limit = intHeader(response, "X-RateLimit-Limit");
  const remaining = intHeader(response, "X-RateLimit-Remaining");
  if (limit === undefined || remaining === undefined) return undefined;
  return { limit, remaining };
}

async function toApiError(response: Response): Promise<ApiError> {
  const requestId = header(response, "X-Request-ID");
  const retryAfter = response.status === 429 ? intHeader(response, "Retry-After") : undefined;
  let message = response.statusText || `HTTP ${response.status}`;
  let type = "server_error";
  let code: string | null = null;

  // A proxy, a gateway or a crash can answer with something that is not our
  // envelope; the status is still the truth, so fall back rather than throw.
  try {
    const body = (await response.json()) as Partial<ApiErrorEnvelope>;
    if (body.error && typeof body.error.message === "string") {
      message = body.error.message;
      type = body.error.type;
      code = body.error.code;
    }
  } catch {
    /* non-JSON body: keep the statusText fallback */
  }

  return new ApiError({
    status: response.status,
    message,
    type,
    code,
    ...(requestId !== undefined && { requestId }),
    ...(retryAfter !== undefined && { retryAfter }),
  });
}

/**
 * The single fetch entry point. `auth` is the raw bearer token (an API key for
 * /v1 and /api, the admin token for /admin); omit it for unauthenticated calls.
 */
export async function request<T>(
  path: string,
  init: RequestInit = {},
  auth?: string,
): Promise<ApiResponse<T>> {
  const headers = new Headers(init.headers);
  if (auth !== undefined && auth !== "") headers.set("Authorization", `Bearer ${auth}`);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, { ...init, headers });
  if (!response.ok) throw await toApiError(response);

  const requestId = header(response, "X-Request-ID");
  const rateLimit = readRateLimit(response);
  // 204 has no body; every other success on this API is JSON.
  const data = (response.status === 204 ? undefined : await response.json()) as T;

  return {
    data,
    ...(rateLimit !== undefined && { rateLimit }),
    ...(requestId !== undefined && { requestId }),
  };
}
