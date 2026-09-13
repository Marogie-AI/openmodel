import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, isValidApiKey, request } from "../api/client";
import type { Me } from "../api/types";
import { useSession } from "../auth/session";

export default function Setup() {
  const { apiKey, setApiKey, clearApiKey } = useSession();
  const [draft, setDraft] = useState("");
  const [formatError, setFormatError] = useState<string | null>(null);

  // /api/me is in the authed group with no rate limit: it is the cheap way to
  // find out whether a key is real.
  const me = useQuery({
    queryKey: ["me", apiKey],
    enabled: apiKey !== null,
    queryFn: async () => (await request<Me>("/api/me", {}, apiKey ?? "")).data,
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const key = draft.trim();
    if (!isValidApiKey(key)) {
      setFormatError("That is not an OpenModel key. Expected om_<env>_<key id>_<secret>.");
      return;
    }
    setFormatError(null);
    setDraft("");
    setApiKey(key);
  }

  if (apiKey === null) {
    return (
      <section>
        <h2>Connect a key</h2>
        <p className="muted">
          Paste an API key. It is stored in this browser&rsquo;s localStorage — the same exposure as
          pasting it into curl, and only for your own organization.
        </p>
        <form onSubmit={submit}>
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder="om_dev_..."
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            aria-label="API key"
            aria-invalid={formatError !== null}
          />
          <button type="submit">Connect</button>
        </form>
        {formatError !== null && <p className="error">{formatError}</p>}
      </section>
    );
  }

  return (
    <section>
      <h2>This key</h2>
      <p className="muted mono">{apiKey.slice(0, 15)}…</p>

      {me.isPending && <p>Checking the key…</p>}

      {me.isError && (
        <div className="error">
          <p>{me.error instanceof ApiError ? me.error.message : String(me.error)}</p>
          {me.error instanceof ApiError && me.error.requestId !== undefined && (
            <p className="mono small">request id: {me.error.requestId}</p>
          )}
        </div>
      )}

      {me.data !== undefined && (
        <dl>
          <dt>Organization</dt>
          <dd className="mono">{me.data.organization_id}</dd>
          <dt>User</dt>
          <dd className="mono">{me.data.user_id}</dd>
          <dt>Plan</dt>
          <dd>{me.data.plan_code}</dd>
          <dt>Requests per minute</dt>
          <dd>{me.data.requests_per_minute}</dd>
          <dt>Max concurrency</dt>
          <dd>{me.data.max_concurrency}</dd>
          <dt>Models</dt>
          <dd>
            <ul>
              {me.data.models.map((model) => (
                <li key={model} className="mono">
                  {model}
                </li>
              ))}
            </ul>
          </dd>
        </dl>
      )}

      <button type="button" onClick={clearApiKey}>
        Forget this key
      </button>
    </section>
  );
}
