import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, request, type RateLimit } from "../api/client";
import { streamChat } from "../api/stream";
import type {
  ChatCompletion,
  ChatCompletionChunk,
  ChatMessage,
  ModelList,
} from "../api/types";
import { useSession } from "../auth/session";

/** What the footer under a finished exchange reports. */
export interface CallFacts {
  promptTokens?: number;
  completionTokens?: number;
  latencyMs: number;
  remaining?: number;
  limit?: number;
  requestId?: string;
  finishReason?: string;
}

/**
 * Facts for a streamed exchange, or null while it is still mid-stream.
 *
 * Chunks are serialized with `exclude_none=True`, so `usage` and `finish_reason`
 * are absent on intermediate chunks. Comparing them with `!==` against null
 * treats `undefined` as present and reads a property off it — and because this
 * runs inside a `setState` updater, the resulting TypeError surfaces as a render
 * error that unmounts the whole app.
 */
export function factsFrom(
  chunk: ChatCompletionChunk,
  latencyMs: number,
  head: { rateLimit?: RateLimit; requestId?: string },
): CallFacts | null {
  const choice = chunk.choices[0];
  const usage = chunk.usage;
  const finishReason = choice?.finish_reason;
  if (finishReason == null && usage == null) return null;

  return {
    latencyMs,
    ...(usage != null && {
      promptTokens: usage.prompt_tokens,
      completionTokens: usage.completion_tokens,
    }),
    ...(finishReason != null && { finishReason }),
    ...(head.rateLimit !== undefined && {
      remaining: head.rateLimit.remaining,
      limit: head.rateLimit.limit,
    }),
    ...(head.requestId !== undefined && { requestId: head.requestId }),
  };
}

interface Exchange {
  sent: string;
  received: string;
  facts?: CallFacts;
  error?: { message: string; retryAfter?: number; requestId?: string };
}

export default function Chat() {
  const { apiKey } = useSession();
  const [prompt, setPrompt] = useState("");
  const [stream, setStream] = useState(true);
  const [maxTokens, setMaxTokens] = useState(256);
  const [temperature, setTemperature] = useState(0.7);
  const [model, setModel] = useState<string | null>(null);
  const [history, setHistory] = useState<Exchange[]>([]);
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);

  const models = useQuery({
    queryKey: ["models", apiKey],
    enabled: apiKey !== null,
    queryFn: async () => (await request<ModelList>("/v1/models", {}, apiKey ?? "")).data,
  });

  if (apiKey === null) {
    return <p className="muted">Connect a key on the Setup tab first.</p>;
  }

  // Narrowed above, but captured explicitly: the closures below outlive this
  // render, and TypeScript will not carry the narrowing into them.
  const key = apiKey;
  const available = models.data?.data.map((entry) => entry.id) ?? [];
  const chosen = model ?? available[0] ?? null;

  /** Replace the exchange being built, which is always the last one. */
  function patch(update: (current: Exchange) => Exchange): void {
    setHistory((entries) => {
      const last = entries.at(-1);
      if (last === undefined) return entries;
      return [...entries.slice(0, -1), update(last)];
    });
  }

  async function send(event: React.FormEvent): Promise<void> {
    event.preventDefault();
    const text = prompt.trim();
    if (text === "" || chosen === null) return;

    const messages: ChatMessage[] = [{ role: "user", content: text }];
    const body = { model: chosen, messages, temperature, max_tokens: maxTokens };

    setPrompt("");
    setHistory((entries) => [...entries, { sent: text, received: "" }]);
    setBusy(true);
    const started = performance.now();

    try {
      if (stream) {
        const controller = new AbortController();
        abort.current = controller;
        const start = await streamChat(body, key, controller.signal);

        for await (const event_ of start.events) {
          if (event_.kind === "error") {
            patch((current) => ({ ...current, error: { message: event_.error.message } }));
            continue;
          }
          const piece = event_.chunk.choices[0]?.delta.content ?? "";
          const facts = factsFrom(event_.chunk, Math.round(performance.now() - started), start);
          patch((current) => ({
            ...current,
            received: current.received + piece,
            ...(facts !== null && { facts }),
          }));
        }
      } else {
        const answer = await request<ChatCompletion>(
          "/v1/chat/completions",
          { method: "POST", body: JSON.stringify(body) },
          key,
        );
        const choice = answer.data.choices[0];
        patch((current) => ({
          ...current,
          received: choice?.message.content ?? "",
          facts: {
            latencyMs: Math.round(performance.now() - started),
            promptTokens: answer.data.usage.prompt_tokens,
            completionTokens: answer.data.usage.completion_tokens,
            ...(choice !== undefined && { finishReason: choice.finish_reason }),
            ...(answer.rateLimit !== undefined && {
              remaining: answer.rateLimit.remaining,
              limit: answer.rateLimit.limit,
            }),
            ...(answer.requestId !== undefined && { requestId: answer.requestId }),
          },
        }));
      }
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") {
        patch((current) => ({ ...current, error: { message: "Stopped." } }));
      } else if (caught instanceof ApiError) {
        patch((current) => ({
          ...current,
          error: {
            message: caught.message,
            ...(caught.retryAfter !== undefined && { retryAfter: caught.retryAfter }),
            ...(caught.requestId !== undefined && { requestId: caught.requestId }),
          },
        }));
      } else {
        patch((current) => ({ ...current, error: { message: String(caught) } }));
      }
    } finally {
      abort.current = null;
      setBusy(false);
    }
  }

  return (
    <section>
      <h2>Chat</h2>

      {models.isError && (
        <p className="error">
          {models.error instanceof ApiError ? models.error.message : String(models.error)}
        </p>
      )}

      <div className="row">
        <label>
          Model{" "}
          <select
            value={chosen ?? ""}
            onChange={(event) => setModel(event.target.value)}
            disabled={available.length === 0}
          >
            {available.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </label>
        <label>
          Max tokens{" "}
          <input
            type="number"
            min={1}
            max={2048}
            value={maxTokens}
            onChange={(event) => setMaxTokens(Number(event.target.value))}
            className="narrow"
          />
        </label>
        <label>
          Temperature{" "}
          <input
            type="number"
            min={0}
            max={2}
            step={0.1}
            value={temperature}
            onChange={(event) => setTemperature(Number(event.target.value))}
            className="narrow"
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={stream}
            onChange={(event) => setStream(event.target.checked)}
          />{" "}
          Stream
        </label>
      </div>

      <ol className="thread">
        {history.map((entry, index) => (
          <li key={index}>
            <p className="sent">{entry.sent}</p>
            <p className="received">
              {entry.received}
              {entry.received === "" && entry.error === undefined && busy && (
                <span className="muted">…</span>
              )}
            </p>
            {entry.error !== undefined && (
              <p className="error small">
                {entry.error.message}
                {entry.error.retryAfter !== undefined && ` Retry after ${entry.error.retryAfter}s.`}
                {entry.error.requestId !== undefined && (
                  <span className="mono"> ({entry.error.requestId})</span>
                )}
              </p>
            )}
            {entry.facts !== undefined && <Facts facts={entry.facts} />}
          </li>
        ))}
      </ol>

      <form onSubmit={send}>
        <input
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="Ask something"
          aria-label="Prompt"
          disabled={busy}
        />
        <button type="submit" disabled={busy || chosen === null}>
          Send
        </button>
        {busy && stream && (
          <button type="button" onClick={() => abort.current?.abort()}>
            Stop
          </button>
        )}
      </form>
    </section>
  );
}

function Facts({ facts }: { facts: CallFacts }) {
  const parts: string[] = [`${facts.latencyMs} ms`];
  if (facts.promptTokens !== undefined && facts.completionTokens !== undefined) {
    parts.push(`${facts.promptTokens} in / ${facts.completionTokens} out`);
  }
  if (facts.finishReason !== undefined) parts.push(facts.finishReason);
  if (facts.remaining !== undefined && facts.limit !== undefined) {
    parts.push(`${facts.remaining}/${facts.limit} left this minute`);
  }
  return (
    <p className="muted small mono">
      {parts.join(" · ")}
      {facts.requestId !== undefined && ` · ${facts.requestId}`}
    </p>
  );
}
