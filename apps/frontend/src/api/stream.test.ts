import { describe, expect, it, vi } from "vitest";
import { streamChat } from "./stream";
import type { StreamEvent } from "./stream";

/** A Response whose body streams the given string pieces, one per read(). */
function streaming(pieces: string[], init: ResponseInit = {}): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const piece of pieces) controller.enqueue(encoder.encode(piece));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream", ...init.headers },
    ...init,
  });
}

function chunk(content: string): string {
  return JSON.stringify({
    id: "chatcmpl-1",
    object: "chat.completion.chunk",
    created: 1,
    model: "qwen2.5:0.5b",
    choices: [{ index: 0, delta: { content }, finish_reason: null }],
  });
}

async function collect(pieces: string[], init?: ResponseInit): Promise<StreamEvent[]> {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streaming(pieces, init)));
  const { events } = await streamChat(
    { model: "qwen2.5:0.5b", messages: [{ role: "user", content: "hi" }] },
    "om_dev_abcdefghijkl_abcdefghijklmnopqrstuvwxyz012345",
  );
  const seen: StreamEvent[] = [];
  for await (const event of events) seen.push(event);
  return seen;
}

function contents(events: StreamEvent[]): string[] {
  return events.flatMap((event) =>
    event.kind === "chunk" ? [event.chunk.choices[0]?.delta.content ?? ""] : [],
  );
}

describe("streamChat framing", () => {
  it("reads several frames out of one read", async () => {
    const events = await collect([
      `data: ${chunk("one")}\n\ndata: ${chunk(" two")}\n\ndata: [DONE]\n\n`,
    ]);
    expect(contents(events)).toEqual(["one", " two"]);
  });

  it("joins a frame split across two reads", async () => {
    const frame = `data: ${chunk("split")}\n\n`;
    const cut = Math.floor(frame.length / 2);
    const events = await collect([frame.slice(0, cut), frame.slice(cut), "data: [DONE]\n\n"]);
    expect(contents(events)).toEqual(["split"]);
  });

  it("stops at the terminator and ignores anything after it", async () => {
    const events = await collect([`data: ${chunk("a")}\n\ndata: [DONE]\n\ndata: ${chunk("b")}\n\n`]);
    expect(contents(events)).toEqual(["a"]);
  });

  it("accepts a final frame with no trailing blank line", async () => {
    const events = await collect([`data: ${chunk("last")}`]);
    expect(contents(events)).toEqual(["last"]);
  });

  it("surfaces an in-stream error envelope, then the terminator ends it", async () => {
    const envelope = JSON.stringify({
      error: { message: "model server unavailable", type: "server_error", code: "backend_unavailable" },
    });
    const events = await collect([`data: ${chunk("part")}\n\ndata: ${envelope}\n\ndata: [DONE]\n\n`]);

    expect(contents(events)).toEqual(["part"]);
    const failure = events.at(-1);
    expect(failure?.kind).toBe("error");
    if (failure?.kind !== "error") throw new Error("expected an error event");
    expect(failure.error.message).toBe("model server unavailable");
    expect(failure.error.code).toBe("backend_unavailable");
  });

  it("reports a frame it cannot parse instead of dropping it", async () => {
    const events = await collect(["data: {not json\n\ndata: [DONE]\n\n"]);
    expect(events).toHaveLength(1);
    expect(events[0]?.kind).toBe("error");
  });

  it("exposes rate-limit headers and the request id from the response head", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streaming(["data: [DONE]\n\n"], {
          headers: {
            "X-RateLimit-Limit": "600",
            "X-RateLimit-Remaining": "599",
            "X-Request-ID": "req-1",
          },
        }),
      ),
    );
    const start = await streamChat(
      { model: "qwen2.5:0.5b", messages: [] },
      "om_dev_abcdefghijkl_abcdefghijklmnopqrstuvwxyz012345",
    );
    expect(start.rateLimit).toEqual({ limit: 600, remaining: 599 });
    expect(start.requestId).toBe("req-1");
  });
});

describe("streamChat pre-stream failures", () => {
  it("throws the envelope for a 429 with Retry-After", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: { message: "Too many requests", type: "rate_limit_error", code: "rate_limit_exceeded" },
          }),
          { status: 429, headers: { "Retry-After": "30", "X-RateLimit-Limit": "2" } },
        ),
      ),
    );

    await expect(
      streamChat({ model: "m", messages: [] }, "om_dev_abcdefghijkl_abcdefghijklmnopqrstuvwxyz012345"),
    ).rejects.toMatchObject({ status: 429, retryAfter: 30, code: "rate_limit_exceeded" });
  });

  it("falls back to the status when the error body is not our envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("upstream connect error", { status: 503 })),
    );

    await expect(
      streamChat({ model: "m", messages: [] }, "om_dev_abcdefghijkl_abcdefghijklmnopqrstuvwxyz012345"),
    ).rejects.toMatchObject({ status: 503 });
  });
});
