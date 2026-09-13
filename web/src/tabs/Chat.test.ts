import { describe, expect, it } from "vitest";
import { factsFrom } from "./Chat";
import type { ChatCompletionChunk } from "../api/types";

/**
 * These are the literal frames the backend sends, captured from
 * `curl -N /v1/chat/completions`. Intermediate chunks carry neither
 * `finish_reason` nor `usage` — `exclude_none=True` drops the keys rather than
 * sending null, which is what broke the first version of this code.
 */
const PRELUDE = JSON.parse(
  '{"id":"chatcmpl-857d15","object":"chat.completion.chunk","created":1789330896,"model":"qwen2.5:0.5b","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}',
) as ChatCompletionChunk;

const CONTENT = JSON.parse(
  '{"id":"chatcmpl-857d15","object":"chat.completion.chunk","created":1789330896,"model":"qwen2.5:0.5b","choices":[{"index":0,"delta":{"content":"Hello"}}]}',
) as ChatCompletionChunk;

const FINAL = JSON.parse(
  '{"id":"chatcmpl-857d15","object":"chat.completion.chunk","created":1789330896,"model":"qwen2.5:0.5b","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":31,"completion_tokens":9,"total_tokens":40}}',
) as ChatCompletionChunk;

describe("factsFrom", () => {
  it("returns null for a chunk that carries neither usage nor a finish reason", () => {
    // The regression: `usage !== null` was true for an absent key, and reading
    // `.prompt_tokens` off undefined threw inside a setState updater, which
    // React reports as a render error and unmounts the app.
    expect(factsFrom(PRELUDE, 10, {})).toBeNull();
    expect(factsFrom(CONTENT, 20, {})).toBeNull();
  });

  it("builds the footer from the final chunk", () => {
    const facts = factsFrom(FINAL, 1234, {
      rateLimit: { limit: 600, remaining: 598 },
      requestId: "req-9",
    });

    expect(facts).toEqual({
      latencyMs: 1234,
      promptTokens: 31,
      completionTokens: 9,
      finishReason: "stop",
      limit: 600,
      remaining: 598,
      requestId: "req-9",
    });
  });

  it("omits rate limit and request id when the response head carried neither", () => {
    const facts = factsFrom(FINAL, 5, {});
    expect(facts).not.toBeNull();
    expect(facts).not.toHaveProperty("limit");
    expect(facts).not.toHaveProperty("requestId");
  });

  it("reports a finish reason that arrives without usage", () => {
    const truncated = JSON.parse(
      '{"id":"c","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"length"}]}',
    ) as ChatCompletionChunk;

    expect(factsFrom(truncated, 7, {})).toEqual({ latencyMs: 7, finishReason: "length" });
  });
});
