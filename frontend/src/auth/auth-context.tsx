import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ApiError, api } from "../api/client";

export interface CurrentUser {
  id: string;
  username: string;
}

interface AuthValue {
  user: CurrentUser | null;
  loading: boolean;
  register: (username: string, password: string) => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Report an API failure so a rejected session clears the UI immediately. */
  reportFailure: (error: unknown) => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export interface AuthProviderProps {
  children: ReactNode;
  /** Omit to load /api/me; pass a value (or null) to skip the request entirely. */
  initialUser?: CurrentUser | null;
}

export function AuthProvider({ children, initialUser }: AuthProviderProps) {
  const [user, setUser] = useState<CurrentUser | null>(initialUser ?? null);
  const [loading, setLoading] = useState(initialUser === undefined);

  useEffect(() => {
    if (initialUser !== undefined) {
      return;
    }
    let cancelled = false;
    api
      .get<CurrentUser>("/api/me")
      .then((me) => {
        if (!cancelled) {
          setUser(me);
        }
      })
      .catch(() => {
        // A 401 here is the normal signed-out case, not an error worth showing.
        if (!cancelled) {
          setUser(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [initialUser]);

  const register = useCallback(async (username: string, password: string) => {
    setUser(await api.post<CurrentUser>("/api/auth/register", { username, password }));
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    setUser(await api.post<CurrentUser>("/api/auth/login", { username, password }));
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post<void>("/api/auth/logout");
    } finally {
      // The cookie is gone either way; keeping the user signed in would be a lie.
      setUser(null);
    }
  }, []);

  const reportFailure = useCallback((error: unknown) => {
    if (error instanceof ApiError && error.status === 401) {
      setUser(null);
    }
  }, []);

  const value = useMemo<AuthValue>(
    () => ({ user, loading, register, login, logout, reportFailure }),
    [user, loading, register, login, logout, reportFailure],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used inside an AuthProvider");
  }
  return value;
}
