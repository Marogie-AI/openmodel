import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, isValidApiKey, readRateLimit, request } from "./client";

function respond(body: string | null, init: ResponseInit): void {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(body, init)));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request", () => {
  it("returns the parsed body, rate headers and request id", async () => {
    respond(JSON.stringify({ ok: true }), {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "X-Request-ID": "req-1",
        "X-RateLimit-Limit": "60",
        "X-RateLimit-Remaining": "59",
      },
    });
    const result = await request<{ ok: boolean }>("/api/me", {}, "om_dev_key");
    expect(result.data).toEqual({ ok: true });
    expect(result.rateLimit).toEqual({ limit: 60, remaining: 59 });
    expect(result.requestId).toBe("req-1");
  });

  it("omits rateLimit when the route does not set the headers", async () => {
    respond(JSON.stringify({}), { status: 200, headers: { "X-Request-ID": "req-2" } });
    const result = await request("/api/usage");
    expect(result.rateLimit).toBeUndefined();
    expect(result.requestId).toBe("req-2");
  });

  it("sends the bearer header only when a token is given", async () => {
    respond(JSON.stringify({}), { status: 200 });
    await request("/api/me", {}, "om_dev_abc");
    const [, authed] = vi.mocked(fetch).mock.calls[0] ?? [];
    expect(new Headers(authed?.headers).get("Authorization")).toBe("Bearer om_dev_abc");

    await request("/api/me");
    const [, anon] = vi.mocked(fetch).mock.calls[1] ?? [];
    expect(new Headers(anon?.headers).has("Authorization")).toBe(false);
  });

  it("returns undefined data for 204", async () => {
    respond(null, { status: 204 });
    const result = await request("/api/keys/abc", { method: "DELETE" }, "k");
    expect(result.data).toBeUndefined();
  });

  it("unpacks the error envelope", async () => {
    respond(
      JSON.stringify({
        error: { message: "Missing or invalid API key", type: "invalid_request_error", code: null },
      }),
      {
        status: 401,
        headers: { "Content-Type": "application/json", "X-Request-ID": "req-3" },
      },
    );
    const error = await request("/api/me").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    const api = error as ApiError;
    expect(api.status).toBe(401);
    expect(api.message).toBe("Missing or invalid API key");
    expect(api.type).toBe("invalid_request_error");
    expect(api.code).toBeNull();
    expect(api.requestId).toBe("req-3");
    expect(api.retryAfter).toBeUndefined();
  });

  it("falls back to statusText on a non-JSON error body", async () => {
    respond("<html>502 Bad Gateway</html>", {
      status: 502,
      statusText: "Bad Gateway",
      headers: { "Content-Type": "text/html" },
    });
    const error = (await request("/api/me").catch((caught: unknown) => caught)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("Bad Gateway");
    expect(error.type).toBe("server_error");
    expect(error.code).toBeNull();
  });

  it("surfaces Retry-After on 429", async () => {
    respond(
      JSON.stringify({
        error: { message: "Rate limit exceeded", type: "rate_limit_error", code: "rate_limited" },
      }),
      {
        status: 429,
        headers: {
          "Content-Type": "application/json",
          "Retry-After": "17",
          "X-RateLimit-Limit": "60",
          "X-RateLimit-Remaining": "0",
        },
      },
    );
    const error = (await request("/v1/chat/completions", { method: "POST" }, "k").catch(
      (caught: unknown) => caught,
    )) as ApiError;
    expect(error.status).toBe(429);
    expect(error.retryAfter).toBe(17);
    expect(error.code).toBe("rate_limited");
  });
});

describe("readRateLimit", () => {
  it("is undefined when only one header is present or a value is junk", () => {
    expect(readRateLimit(new Response(null, { headers: { "X-RateLimit-Limit": "60" } }))).toBeUndefined();
    expect(
      readRateLimit(
        new Response(null, { headers: { "X-RateLimit-Limit": "n/a", "X-RateLimit-Remaining": "0" } }),
      ),
    ).toBeUndefined();
  });
});

describe("isValidApiKey", () => {
  it("accepts real keys", () => {
    expect(isValidApiKey("om_dev_abcdefghijkl_abcdefghijklmnopqrstuvwxyz012345")).toBe(true);
    expect(isValidApiKey("om_prod_A1b2C3d4E5f6_-_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")).toBe(true);
  });

  it("rejects malformed keys", () => {
    for (const bad of [
      "",
      "om_dev_abcdefghijkl_tooshort",
      "sk_dev_abcdefghijkl_abcdefghijklmnopqrstuvwxyz012345",
      "om_DEV_abcdefghijkl_abcdefghijklmnopqrstuvwxyz012345",
      "om_dev_abcdefghijk_abcdefghijklmnopqrstuvwxyz012345",
      " om_dev_abcdefghijkl_abcdefghijklmnopqrstuvwxyz012345",
      "om_dev_abcdefghijkl_abcdefghijklmnopqrstuvwxyz012345x",
    ]) {
      expect(isValidApiKey(bad), bad).toBe(false);
    }
  });
});
