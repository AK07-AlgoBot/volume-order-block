const API_BASE =
  (import.meta.env.VITE_DASHBOARD_API_BASE || "").trim() || "http://127.0.0.1:8000";

const TOKEN_KEY = "ak07_access_token";
const USER_KEY = "ak07_username";
const ROLE_KEY = "ak07_role";

export function getApiBase() {
  return API_BASE;
}

export function getWsBase() {
  return API_BASE.startsWith("https://")
    ? API_BASE.replace("https://", "wss://")
    : API_BASE.replace("http://", "ws://");
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
  window.localStorage.removeItem("dashboardAdminToken");
}

export function authHeaders() {
  const { token } = getStoredAuth();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * @param {string} path — e.g. /api/dashboard/initial or api/dashboard/weekly-pnl?week_offset=0
 * @param {RequestInit} options
 * @param {string} [viewAs] — admin: scope data to another user
 */
export async function apiFetch(path, options = {}, viewAs) {
  const base = API_BASE.replace(/\/$/, "");
  const rel = path.replace(/^\//, "");
  const u = new URL(rel, `${base}/`);
  if (viewAs) {
    u.searchParams.set("view_as", viewAs);
  }
  const headers = {
    ...(options.headers || {}),
    ...authHeaders(),
  };
  return fetch(u.toString(), { ...options, headers });
}
