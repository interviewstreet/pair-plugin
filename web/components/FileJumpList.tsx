"use client";

export interface JumpFile {
  path: string;
  additions: number;
  deletions: number;
  risky: number;
  drafts: number;
}

function split(path: string): { dir: string; base: string } {
  const i = path.lastIndexOf("/");
  return i === -1 ? { dir: "", base: path } : { dir: path.slice(0, i), base: path.slice(i + 1) };
}

export function FileJumpList({
  files,
  active,
  onJump,
}: {
  files: JumpFile[];
  active: string | null;
  onJump: (path: string) => void;
}) {
  return (
    <nav className="jump-list" aria-label="Files in this PR">
      {files.map((f) => {
        const { dir, base } = split(f.path);
        return (
          <button
            key={f.path}
            type="button"
            className={f.path === active ? "active" : ""}
            onClick={() => onJump(f.path)}
            title={f.path}
          >
            <span className="name">
              <span className="base">{base}</span>
              {dir && <span className="dir">{dir}</span>}
            </span>
            {f.risky > 0 && <span className="dot risky" title={`${f.risky} risky hunk(s)`} />}
            {f.drafts > 0 && <span className="dot draft" title={`${f.drafts} draft(s)`} />}
            <span className="stat">
              {f.deletions > 0 && <span className="dels">−{f.deletions}</span>}
              {f.additions > 0 && <span className="adds">+{f.additions}</span>}
            </span>
          </button>
        );
      })}
    </nav>
  );
}
