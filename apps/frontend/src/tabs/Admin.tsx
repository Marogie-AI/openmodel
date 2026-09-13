import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request } from "../api/client";
import type { KeyOut } from "../api/types";
import { useSession } from "../auth/session";
import { Failure } from "./Keys";

// Admin shapes live here rather than in api/types.ts: nothing else calls /admin.
interface Plan {
  code: string;
  requests_per_minute: number;
  max_concurrency: number;
  models: string[];
}

interface Org {
  id: string;
  name: string;
  plan_code: string;
  created_at: string;
}

interface User {
  id: string;
  organization_id: string;
  email: string;
  created_at: string;
}

export default function Admin() {
  const { adminToken, setAdminToken } = useSession();
  const [draft, setDraft] = useState("");

  if (adminToken === null) {
    return (
      <section>
        <h2>Admin</h2>
        <div className="callout">
          <p>
            <strong>This token controls every tenant.</strong> It is held in memory for this tab
            only — never written to storage — so closing the tab forgets it.
          </p>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const token = draft.trim();
            if (token !== "") {
              setDraft("");
              setAdminToken(token);
            }
          }}
        >
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder="X-Admin-Token"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            aria-label="Admin token"
          />
          <button type="submit">Unlock</button>
        </form>
      </section>
    );
  }

  return <AdminPanel token={adminToken} onLock={() => setAdminToken(null)} />;
}

function AdminPanel({ token, onLock }: { token: string; onLock: () => void }) {
  const queries = useQueryClient();
  const [orgName, setOrgName] = useState("");
  const [orgPlan, setOrgPlan] = useState("free");
  const [userOrg, setUserOrg] = useState("");
  const [userEmail, setUserEmail] = useState("");
  const [minted, setMinted] = useState<KeyOut | null>(null);

  // The admin token goes in a header, not as a bearer, so these calls build
  // their own init rather than using the `auth` argument.
  const headers = { "X-Admin-Token": token };

  const plans = useQuery({
    queryKey: ["admin", "plans"],
    queryFn: async () => (await request<Plan[]>("/admin/plans", { headers })).data,
  });

  const orgs = useQuery({
    queryKey: ["admin", "orgs"],
    queryFn: async () => (await request<Org[]>("/admin/orgs", { headers })).data,
  });

  const createOrg = useMutation({
    mutationFn: async () =>
      (
        await request<Org>("/admin/orgs", {
          method: "POST",
          headers,
          body: JSON.stringify({ name: orgName.trim(), plan_code: orgPlan }),
        })
      ).data,
    onSuccess: async (org) => {
      setOrgName("");
      setUserOrg(org.id);
      await queries.invalidateQueries({ queryKey: ["admin", "orgs"] });
    },
  });

  const createUser = useMutation({
    mutationFn: async () =>
      (
        await request<User>("/admin/users", {
          method: "POST",
          headers,
          body: JSON.stringify({ organization_id: userOrg.trim(), email: userEmail.trim() }),
        })
      ).data,
    onSuccess: (user) => {
      setUserEmail("");
      mintKey.mutate(user.id);
    },
  });

  const mintKey = useMutation({
    mutationFn: async (userId: string) =>
      (await request<KeyOut>(`/admin/users/${userId}/keys`, { method: "POST", headers })).data,
    onSuccess: (key) => setMinted(key),
  });

  return (
    <section>
      <h2>Admin</h2>
      <div className="row">
        <span className="muted small">Token held in memory for this tab.</span>
        <button type="button" onClick={onLock}>
          Forget it
        </button>
      </div>

      {minted !== null && (
        <div className="callout">
          <p>
            <strong>New key — copy it now.</strong> It is shown once.
          </p>
          <p className="mono breakable">{minted.key}</p>
          <button type="button" onClick={() => setMinted(null)}>
            Dismiss
          </button>
        </div>
      )}

      <h3>Plans</h3>
      {plans.isError && <Failure error={plans.error} />}
      {plans.data !== undefined && (
        <table>
          <thead>
            <tr>
              <th>Code</th>
              <th>Req/min</th>
              <th>Concurrency</th>
              <th>Models</th>
            </tr>
          </thead>
          <tbody>
            {plans.data.map((plan) => (
              <tr key={plan.code}>
                <td className="mono">{plan.code}</td>
                <td>{plan.requests_per_minute}</td>
                <td>{plan.max_concurrency}</td>
                <td className="mono small">{plan.models.join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Create an organization</h3>
      {createOrg.isError && <Failure error={createOrg.error} />}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (orgName.trim() !== "" && confirm(`Create organization "${orgName.trim()}"?`)) {
            createOrg.mutate();
          }
        }}
      >
        <input
          value={orgName}
          onChange={(event) => setOrgName(event.target.value)}
          placeholder="Organization name"
          aria-label="Organization name"
        />
        <select value={orgPlan} onChange={(event) => setOrgPlan(event.target.value)}>
          {(plans.data ?? []).map((plan) => (
            <option key={plan.code} value={plan.code}>
              {plan.code}
            </option>
          ))}
        </select>
        <button type="submit" disabled={createOrg.isPending}>
          Create
        </button>
      </form>

      {orgs.isError && <Failure error={orgs.error} />}
      {orgs.data !== undefined && orgs.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Organization</th>
              <th>Plan</th>
              <th>Id</th>
            </tr>
          </thead>
          <tbody>
            {orgs.data.map((org) => (
              <tr key={org.id}>
                <td>{org.name}</td>
                <td className="mono">{org.plan_code}</td>
                <td>
                  <button type="button" onClick={() => setUserOrg(org.id)}>
                    Use
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Create a user and its first key</h3>
      <p className="muted small">
        The user is created and a key is minted in one step, because a user with no key cannot do
        anything.
      </p>
      {createUser.isError && <Failure error={createUser.error} />}
      {mintKey.isError && <Failure error={mintKey.error} />}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (userOrg.trim() !== "" && userEmail.trim() !== "" && confirm(`Create ${userEmail.trim()}?`)) {
            createUser.mutate();
          }
        }}
      >
        <input
          value={userOrg}
          onChange={(event) => setUserOrg(event.target.value)}
          placeholder="Organization id"
          aria-label="Organization id"
          className="mono"
        />
        <input
          type="email"
          value={userEmail}
          onChange={(event) => setUserEmail(event.target.value)}
          placeholder="email@example.test"
          aria-label="Email"
        />
        <button type="submit" disabled={createUser.isPending || mintKey.isPending}>
          Create
        </button>
      </form>
    </section>
  );
}
