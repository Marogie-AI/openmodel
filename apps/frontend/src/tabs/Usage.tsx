import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { request } from "../api/client";
import type { UsageSummary } from "../api/types";
import { useSession } from "../auth/session";
import { Failure } from "./Keys";

const GROUPINGS = ["none", "model", "user", "day"] as const;
type Grouping = (typeof GROUPINGS)[number];

/** `YYYY-MM-DD` for a date input, from a day offset relative to today. */
function dayInput(offsetDays: number): string {
  const date = new Date(Date.now() + offsetDays * 86_400_000);
  return date.toISOString().slice(0, 10);
}

export default function Usage() {
  const { apiKey } = useSession();
  // The API's own default window is the last 30 days; match it so the first
  // render shows the same numbers a bare curl would.
  const [from, setFrom] = useState(dayInput(-30));
  const [to, setTo] = useState(dayInput(1));
  const [grouping, setGrouping] = useState<Grouping>("day");

  const usage = useQuery({
    queryKey: ["usage", apiKey, from, to, grouping],
    enabled: apiKey !== null,
    queryFn: async () => {
      const params = new URLSearchParams({
        from: new Date(from).toISOString(),
        to: new Date(to).toISOString(),
      });
      if (grouping !== "none") params.set("group_by", grouping);
      return (await request<UsageSummary>(`/api/usage?${params.toString()}`, {}, apiKey ?? "")).data;
    },
  });

  if (apiKey === null) {
    return <p className="muted">Connect a key on the Setup tab first.</p>;
  }

  const groups = usage.data?.groups ?? [];
  const busiest = Math.max(1, ...groups.map((group) => group.requests));

  return (
    <section>
      <h2>Usage</h2>
      <p className="muted">
        Your whole organization, not just this key. Rows are written after each call, so the newest
        request can lag the table by a moment.
      </p>

      <div className="row">
        <label>
          From <input type="date" value={from} onChange={(event) => setFrom(event.target.value)} />
        </label>
        <label>
          To <input type="date" value={to} onChange={(event) => setTo(event.target.value)} />
        </label>
        <label>
          Group by{" "}
          <select
            value={grouping}
            onChange={(event) => setGrouping(event.target.value as Grouping)}
          >
            {GROUPINGS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      </div>

      {usage.isError && <Failure error={usage.error} />}
      {usage.isPending && <p>Loading usage…</p>}

      {usage.data !== undefined && (
        <>
          <dl>
            <dt>Requests</dt>
            <dd>{usage.data.requests.toLocaleString()}</dd>
            <dt>Prompt tokens</dt>
            <dd>{usage.data.prompt_tokens.toLocaleString()}</dd>
            <dt>Completion tokens</dt>
            <dd>{usage.data.completion_tokens.toLocaleString()}</dd>
          </dl>

          {usage.data.requests === 0 && (
            <p className="muted">
              Nothing in this window. Send a chat request and come back.
            </p>
          )}

          {groups.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>{grouping === "none" ? "Total" : grouping}</th>
                  <th>Requests</th>
                  <th>In</th>
                  <th>Out</th>
                  <th className="wide" />
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => (
                  <tr key={group.key ?? "total"}>
                    <td className="mono">{group.key ?? "all"}</td>
                    <td>{group.requests.toLocaleString()}</td>
                    <td>{group.prompt_tokens.toLocaleString()}</td>
                    <td>{group.completion_tokens.toLocaleString()}</td>
                    <td>
                      <span
                        className="bar"
                        style={{ width: `${Math.round((group.requests / busiest) * 100)}%` }}
                        aria-hidden="true"
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
