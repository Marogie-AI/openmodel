import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, request } from "../api/client";
import type { KeyList, KeyOut } from "../api/types";
import { useSession } from "../auth/session";

function when(iso: string): string {
  return new Date(iso).toLocaleString();
}

export default function Keys() {
  const { apiKey } = useSession();
  const queries = useQueryClient();
  const [minted, setMinted] = useState<KeyOut | null>(null);
  const [copied, setCopied] = useState(false);

  const keys = useQuery({
    queryKey: ["keys", apiKey],
    enabled: apiKey !== null,
    queryFn: async () => (await request<KeyList>("/api/keys", {}, apiKey ?? "")).data,
  });

  const create = useMutation({
    mutationFn: async () =>
      (await request<KeyOut>("/api/keys", { method: "POST" }, apiKey ?? "")).data,
    onSuccess: async (key) => {
      setMinted(key);
      setCopied(false);
      await queries.invalidateQueries({ queryKey: ["keys"] });
    },
  });

  const revoke = useMutation({
    mutationFn: async (id: string) => {
      await request<void>(`/api/keys/${id}`, { method: "DELETE" }, apiKey ?? "");
    },
    onSuccess: async () => {
      await queries.invalidateQueries({ queryKey: ["keys"] });
    },
  });

  if (apiKey === null) {
    return <p className="muted">Connect a key on the Setup tab first.</p>;
  }

  const inUsePrefix = apiKey.slice(0, apiKey.lastIndexOf("_"));

  return (
    <section>
      <h2>Keys</h2>
      <p className="muted">
        Every key here belongs to your user. Creating one costs a request against your rate limit,
        the same as any other call.
      </p>

      {minted !== null && (
        <div className="callout">
          <p>
            <strong>Copy this now.</strong> The raw key is shown once and is not stored anywhere we
            can read it back.
          </p>
          <p className="mono breakable">{minted.key}</p>
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard.writeText(minted.key).then(() => setCopied(true));
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
          <button type="button" onClick={() => setMinted(null)}>
            Dismiss
          </button>
        </div>
      )}

      <div className="row">
        <button type="button" onClick={() => create.mutate()} disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create a key"}
        </button>
      </div>

      {create.isError && <Failure error={create.error} />}
      {revoke.isError && <Failure error={revoke.error} />}
      {keys.isError && <Failure error={keys.error} />}
      {keys.isPending && <p>Loading keys…</p>}

      {keys.data !== undefined &&
        (keys.data.data.length === 0 ? (
          <p className="muted">No keys yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Prefix</th>
                <th>Created</th>
                <th>State</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {keys.data.data.map((key) => {
                const isCurrent = key.prefix === inUsePrefix;
                return (
                  <tr key={key.id}>
                    <td className="mono">
                      {key.prefix}
                      {isCurrent && <span className="muted small"> (in use)</span>}
                    </td>
                    <td className="small">{when(key.created_at)}</td>
                    <td className="small">
                      {key.revoked_at === null ? "active" : `revoked ${when(key.revoked_at)}`}
                    </td>
                    <td>
                      {key.revoked_at === null && (
                        <button
                          type="button"
                          disabled={revoke.isPending}
                          onClick={() => {
                            const warning = isCurrent
                              ? "This is the key this console is using. Revoking it logs you out. Continue?"
                              : `Revoke ${key.prefix}? This cannot be undone.`;
                            if (confirm(warning)) revoke.mutate(key.id);
                          }}
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ))}
    </section>
  );
}

export function Failure({ error }: { error: unknown }) {
  if (!(error instanceof ApiError)) return <p className="error">{String(error)}</p>;
  return (
    <p className="error">
      {error.message}
      {error.retryAfter !== undefined && ` Retry after ${error.retryAfter}s.`}
      {error.requestId !== undefined && <span className="mono small"> ({error.requestId})</span>}
    </p>
  );
}
