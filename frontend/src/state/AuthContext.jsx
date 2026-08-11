import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import {
  getApiConfig,
  getCurrentAdmin,
  loginAdmin,
  setApiAccessToken,
} from "../lib/api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [admin, setAdmin] = useState(null);
  const [checking, setChecking] = useState(true);

  const logout = useCallback(() => {
    setApiAccessToken("");
    setAdmin(null);
    setChecking(false);
  }, []);

  const login = useCallback(async (username, password) => {
    const token = await loginAdmin(username, password);
    setApiAccessToken(token.access_token);
    try {
      const current = await getCurrentAdmin();
      setAdmin(current);
      return current;
    } catch (error) {
      setApiAccessToken("");
      throw error;
    }
  }, []);

  useEffect(() => {
    let active = true;
    const token = getApiConfig().accessToken;
    if (!token) {
      setChecking(false);
      return undefined;
    }
    void getCurrentAdmin()
      .then((current) => { if (active) setAdmin(current); })
      .catch(() => { if (active) logout(); })
      .finally(() => { if (active) setChecking(false); });
    return () => { active = false; };
  }, [logout]);

  useEffect(() => {
    window.addEventListener("auth:expired", logout);
    return () => window.removeEventListener("auth:expired", logout);
  }, [logout]);

  const value = useMemo(() => ({
    admin,
    checking,
    authenticated: Boolean(admin),
    login,
    logout,
  }), [admin, checking, login, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return value;
}
