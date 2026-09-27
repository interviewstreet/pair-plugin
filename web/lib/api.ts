import type {
  Brief,
  CloseResult,
  Draft,
  DraftStatus,
  Excerpt,
  NewDraft,
  PostResult,
  ReviewListItem,
  ReviewMeta,
  RiskyHunk,
} from "./types";

/** Same-origin when unset; `bun run dev` points it at the FastAPI server. */
export const API_BASE = (process.env.NEXT_PUBLIC_PAIR_API ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(API_BASE + path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.clone().json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      detail = (await res.text()) || detail;
    }
    throw new ApiError(res.status, detail);
  }
  return res;
}

const json = <T>(path: string, init?: RequestInit) => request(path, init).then((r) => r.json() as Promise<T>);
const text = (path: string) => request(path).then((r) => r.text());
const rv = (key: string) => `/api/reviews/${encodeURIComponent(key)}`;

export const api = {
  reviews: () => json<ReviewListItem[]>("/api/reviews"),
  meta: (key: string) => json<ReviewMeta>(rv(key)),
  brief: (key: string) => json<Brief>(`${rv(key)}/brief`),
  diff: (key: string) => text(`${rv(key)}/diff`),
  risky: (key: string) => json<RiskyHunk[]>(`${rv(key)}/risky-hunks`),
  excerpt: (key: string, ref: string) => json<Excerpt>(`${rv(key)}/excerpt?ref=${encodeURIComponent(ref)}`),
  file: (key: string, side: "base" | "head", path: string) =>
    text(`${rv(key)}/file?side=${side}&path=${encodeURIComponent(path)}`),
  drafts: (key: string) => json<Draft[]>(`${rv(key)}/drafts`),
  createDraft: (key: string, draft: NewDraft) =>
    json<Draft>(`${rv(key)}/drafts`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(draft),
    }),
  patchDraft: (key: string, id: string, patch: { body?: string; status?: DraftStatus }) =>
    json<Draft>(`${rv(key)}/drafts/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }),
  post: (key: string) => json<PostResult>(`${rv(key)}/post`, { method: "POST" }),
  close: (key: string) => json<CloseResult>(`${rv(key)}/close`, { method: "POST" }),
};

export function chatUrl(key: string): string {
  const path = `${rv(key)}/chat`;
  if (API_BASE) return API_BASE.replace(/^http/, "ws") + path;
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${path}`;
}
