"use client";

import { type ComponentProps, createContext, memo, useContext, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { INLINE_REF_RX, parseRef } from "@/lib/refs";
import { useReview } from "@/lib/store";

import { RefChip } from "./RefChip";

/** Paths of the files in the PR, so `path:line` code spans can link to the diff. */
export const PathsContext = createContext<readonly string[]>([]);

const FENCE_RX = /(```[\s\S]*?(?:```|$))/g;
const PATH_LINE_RX = /^([\w@./-]+\.[\w]+):(\d+)(?:-(\d+))?$/;

/** Wraps bare refs in backticks so the code renderer can turn them into chips. Leaves code fences alone. */
export function linkifyRefs(text: string): string {
  return text
    .split(FENCE_RX)
    .map((part, i) => (i % 2 === 1 ? part : part.replace(new RegExp(`(?<!\`)(${INLINE_REF_RX.source})(?!\`)`, "g"), "`$1`")))
    .join("");
}

function resolvePath(p: string, paths: readonly string[]): string | null {
  if (paths.includes(p)) return p;
  const hits = paths.filter((x) => x.endsWith(`/${p}`));
  return hits.length === 1 ? hits[0] : null;
}

function makeCode(paths: readonly string[], scrollTo: (path: string, line?: number) => void) {
  return function Code({ className, children, ...rest }: ComponentProps<"code">) {
    const text = typeof children === "string" ? children : Array.isArray(children) ? children.join("") : "";
    if (!className && text && !text.includes("\n")) {
      if (parseRef(text).kind !== "unknown") return <RefChip value={text} />;
      const m = PATH_LINE_RX.exec(text);
      const path = m ? resolvePath(m[1], paths) : resolvePath(text, paths);
      if (path) {
        const go = () => scrollTo(path, m ? Number(m[2]) : undefined);
        return (
          <code
            className="path-link"
            role="link"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation();
              go();
            }}
            onKeyDown={(e) => e.key === "Enter" && go()}
          >
            {children}
          </code>
        );
      }
    }
    return (
      <code className={className} {...rest}>
        {children}
      </code>
    );
  };
}

const PLUGINS = [remarkGfm];

function Anchor({ href, children }: ComponentProps<"a">) {
  return (
    <a href={href} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
      {children}
    </a>
  );
}

function MarkdownImpl({ text, tight = false }: { text: string; tight?: boolean }) {
  const paths = useContext(PathsContext);
  const scrollTo = useReview((s) => s.scrollTo);
  const components = useMemo(
    () => ({ code: makeCode(paths, (p, line) => scrollTo(p, line, "additions")), a: Anchor }),
    [paths, scrollTo],
  );
  return (
    <div className={tight ? "md tight" : "md"}>
      <ReactMarkdown remarkPlugins={PLUGINS} components={components}>
        {linkifyRefs(text)}
      </ReactMarkdown>
    </div>
  );
}

export const Markdown = memo(MarkdownImpl);
