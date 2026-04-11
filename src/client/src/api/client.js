function runtimeApiBase() {
  const env = (import.meta.env.VITE_DASHBOARD_API_BASE || "").trim();
  if (env) {
    return env.replace(/\/$/, "");
  }
  // Vite dev serves the UI on :5173. Default API :8080 so it matches Kite's redirect
  // (e.g. http://127.0.0.1:8080/kite/callback). Override with VITE_DASHBOARD_API_BASE.
  if (import.meta.env.DEV) {
    return "http://127.0.0.1:8080";
  }
  if (typeof window !== "undefined" && window.location?.origin) {
    return window.location.origin;
  }
  return "http://127.0.0.1:8080";
}

const TOKEN_KEY = "ak07_access_token";
const USER_KEY = "ak07_username";
const ROLE_KEY = "ak07_role";

export function getApiBase() {
  return runtimeApiBase();
}

export function getWsBase() {
  const base = getApiBase();
  return base.startsWith("https://")
    ? base.replace("https://", "wss://")
    : base.replace("http://", "ws://");
}

export function getStoredAuth() {
  if (typeof window === "undefined") {
    return { token: "", username: "", role: "" };
  }
  return {
    token: window.localStorage.getItem(TOKEN_KEY) || "",
    username: window.localStorage.getItem(USER_KEY) || "",
    role: window.localStorage.getItem(ROLE_KEY) || "",
  };
}

export function setStoredAuth({ token, username, role }) {
  window.localStorage.setItem(TOKEN_KEY, token || "");
  window.localStorage.setItem(USER_KEY, username || "");
  window.localStorage.setItem(ROLE_KEY, role || "");
}

export function clearStoredAuth() {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
  window.localStorage.removeItem(ROLE_KEY);
}

export function authHeaders() {
  const { token } = getStoredAuth();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * @param {string} path — e.g. /api/dashboard/initial
 * @param {RequestInit} options
 */
export async function apiFetch(path, options = {}) {
  const base = getApiBase().replace(/\/$/, "");
  const rel = path.replace(/^\//, "");
  const u = new URL(rel, `${base}/`);
  const headers = {
    ...(options.headers || {}),
    ...authHeaders(),
  };
  return fetch(u.toString(), { ...options, headers });
}
