// Thin typed client for the FIRA REST API. The access token lives in sessionStorage
// (cleared when the tab closes) and is sent as a Bearer token.

const BASE = (import.meta as any).env?.VITE_API_URL ?? "";
const TOKEN_KEY = "fira.token";
const USER_KEY = "fira.user";

export type Json = any;

export interface Session {
  token: string;
  username: string;
  role: "analyst" | "admin";
  expiresAt: string;
}

export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(USER_KEY);
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (!raw || !token) return null;
    const s = JSON.parse(raw) as Omit<Session, "token">;
    if (new Date(s.expiresAt).getTime() < Date.now()) return null;
    return { ...s, token };
  } catch {
    return null;
  }
}

export function saveSession(s: Session | null): void {
  try {
    if (!s) {
      sessionStorage.removeItem(TOKEN_KEY);
      sessionStorage.removeItem(USER_KEY);
      return;
    }
    sessionStorage.setItem(TOKEN_KEY, s.token);
    sessionStorage.setItem(USER_KEY, JSON.stringify({ username: s.username, role: s.role, expiresAt: s.expiresAt }));
  } catch {
    /* storage unavailable: session lives in memory only */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public requestId?: string) {
    super(message);
  }
}

let onUnauthorized: () => void = () => undefined;
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

async function request<T = Json>(method: string, path: string, body?: unknown, raw = false): Promise<T> {
  const session = loadSession();
  const headers: Record<string, string> = {};
  if (session) headers["Authorization"] = `Bearer ${session.token}`;
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(`${BASE}${path}`, { method, headers, body: payload });
  const rid = res.headers.get("X-Request-ID") ?? undefined;
  if (res.status === 401) {
    onUnauthorized();
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, msg, rid);
  }
  if (raw) return (await res.text()) as unknown as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T = Json>(p: string) => request<T>("GET", p),
  post: <T = Json>(p: string, body?: unknown) => request<T>("POST", p, body ?? {}),
  upload: <T = Json>(p: string, form: FormData) => request<T>("POST", p, form),
  text: (p: string) => request<string>("GET", p, undefined, true),
};

export async function login(username: string, password: string): Promise<Session> {
  const r = await request<Json>("POST", "/api/auth/login", { username, password });
  const s: Session = { token: r.access_token, username: r.username, role: r.role, expiresAt: r.expires_at };
  saveSession(s);
  return s;
}

export function qs(params: Record<string, string | number | undefined | null>): string {
  const p = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return p.length ? `?${p.join("&")}` : "";
}
