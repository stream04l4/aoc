/**
 * Typed HTTP client for AOC Control Center.
 * Strictly sends Bearer Token from sessionStorage only (never hardcoded, never leaks secrets).
 */

const TOKEN_STORAGE_KEY = 'aoc_cc_bearer_token';

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

export function getStoredToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

export function setStoredToken(token: string): void {
  try {
    const clean = token.trim();
    if (clean) {
      sessionStorage.setItem(TOKEN_STORAGE_KEY, clean);
    } else {
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    // ignore storage errors
  }
}

export function clearStoredToken(): void {
  setStoredToken('');
}

export interface PrincipalInfo {
  name: string;
  scopes: string[];
  source: string;
}

export function hasScope(principal: PrincipalInfo | null, required: string): boolean {
  if (!principal) return false;
  return principal.scopes.includes('admin') || principal.scopes.includes(required);
}

export async function apiRequest<T = any>(
  path: string,
  options: {
    method?: string;
    body?: unknown;
    token?: string;
    signal?: AbortSignal;
  } = {}
): Promise<T> {
  const token = options.token ?? getStoredToken();
  const headers: Record<string, string> = {
    Accept: 'application/json',
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  const resp = await fetch(path, {
    method: options.method || 'GET',
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });

  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const data = await resp.json();
      detail = data.detail || JSON.stringify(data);
    } catch {
      try {
        detail = await resp.text();
      } catch {
        // noop
      }
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '未记录';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString('zh-CN', {
      hour12: false,
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return iso;
  }
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return new Intl.NumberFormat('zh-CN').format(n);
}

export function formatPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${(v * 100).toFixed(1)}%`;
}

export function shortId(id: string | null | undefined): string {
  if (!id) return '—';
  return id.length > 10 ? `${id.slice(0, 8)}…` : id;
}
