import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

const STORAGE_KEY = "openmodel.apiKey";

export interface Session {
  /**
   * The caller's own API key. Persisted in localStorage: it is a bearer token
   * for that one user's organization, the same exposure as pasting it into curl.
   */
  apiKey: string | null;
  setApiKey: (key: string) => void;
  clearApiKey: () => void;
  /**
   * The admin token is deliberately NOT persisted. It is one shared secret that
   * grants control of every tenant, so it lives in memory for the life of the
   * tab and nowhere else.
   */
  adminToken: string | null;
  setAdminToken: (token: string | null) => void;
}

const SessionContext = createContext<Session | null>(null);

function readStoredKey(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null; // private mode / storage disabled
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [apiKey, setKey] = useState<string | null>(readStoredKey);
  const [adminToken, setAdminToken] = useState<string | null>(null);

  const setApiKey = useCallback((key: string) => {
    try {
      localStorage.setItem(STORAGE_KEY, key);
    } catch {
      /* not persisted; the in-memory value still works for this tab */
    }
    setKey(key);
  }, []);

  const clearApiKey = useCallback(() => {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* nothing to remove */
    }
    setKey(null);
  }, []);

  const value = useMemo<Session>(
    () => ({ apiKey, setApiKey, clearApiKey, adminToken, setAdminToken }),
    [apiKey, setApiKey, clearApiKey, adminToken],
  );

  return <SessionContext value={value}>{children}</SessionContext>;
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (session === null) throw new Error("useSession must be used inside <SessionProvider>");
  return session;
}
