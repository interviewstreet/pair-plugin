export type Ref =
  | { kind: "hunk"; raw: string; path: string; line: number }
  | { kind: "file"; raw: string; path: string }
  | { kind: "claude" | "codex"; raw: string; session: string; id: string }
  | { kind: "pr"; raw: string }
  | { kind: "unknown"; raw: string };

/** Parses the brief's evidence refs: hunk:<path>:<line>, file:<path>, claude:<session>:<uid>, codex:<session>:<turn>, pr:body. */
export function parseRef(raw: string): Ref {
  const colon = raw.indexOf(":");
  if (colon < 0) return { kind: "unknown", raw };
  const kind = raw.slice(0, colon);
  const rest = raw.slice(colon + 1);
  switch (kind) {
    case "hunk": {
      const last = rest.lastIndexOf(":");
      const line = Number(rest.slice(last + 1));
      if (last <= 0 || !Number.isInteger(line) || line < 0) return { kind: "unknown", raw };
      return { kind: "hunk", raw, path: rest.slice(0, last), line };
    }
    case "file":
      return rest ? { kind: "file", raw, path: rest } : { kind: "unknown", raw };
    case "claude":
    case "codex": {
      const sep = rest.indexOf(":");
      if (sep <= 0 || sep === rest.length - 1) return { kind: "unknown", raw };
      return { kind, raw, session: rest.slice(0, sep), id: rest.slice(sep + 1) };
    }
    case "pr":
      return rest === "body" ? { kind: "pr", raw } : { kind: "unknown", raw };
    default:
      return { kind: "unknown", raw };
  }
}

export function isExcerptRef(ref: Ref): boolean {
  return ref.kind === "claude" || ref.kind === "codex" || ref.kind === "pr";
}

export function refLabel(ref: Ref): string {
  switch (ref.kind) {
    case "hunk":
      return `${basename(ref.path)}:${ref.line}`;
    case "file":
      return basename(ref.path);
    case "claude":
    case "codex":
      return `${ref.kind} ${ref.session.slice(0, 8)}`;
    case "pr":
      return "PR description";
    default:
      return ref.raw;
  }
}

export function firstHunkRef(refs: readonly string[]): Extract<Ref, { kind: "hunk" }> | null {
  for (const raw of refs) {
    const r = parseRef(raw);
    if (r.kind === "hunk") return r;
  }
  return null;
}

export function basename(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

/** Session and message refs the pair agent writes inline in its answers. */
export const INLINE_REF_RX = /(?:claude|codex):[\w.-]+:[\w.-]+|pr:body|hunk:[^\s`:]+:\d+/g;
