"use client";

import {
  type CodeViewDiffItem,
  type CodeViewLineSelection,
  type DiffLineAnnotation,
  type FileDiffLoadedFiles,
  type FileDiffMetadata,
  parsePatchFiles,
  type SelectedLineRange,
} from "@pierre/diffs";
import { CodeView, type CodeViewHandle } from "@pierre/diffs/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Panel, PanelGroup } from "react-resizable-panels";

import { api } from "@/lib/api";
import { riskyMarks } from "@/lib/risky";
import { selectionToPayload } from "@/lib/selection";
import { useReview } from "@/lib/store";
import type { Draft, FileStat, RiskyHunk, Selection } from "@/lib/types";
import { useNotify } from "@/lib/useNotify";

import { DraftAnnotation } from "./DraftAnnotation";
import { FileJumpList, type JumpFile } from "./FileJumpList";
import { ChevronDown, ChevronRight, PanelLeftClose, PanelLeftOpen } from "./icons";
import { NewComment } from "./NewComment";
import { ResizeHandle } from "./ResizeHandle";
import { Button, Tag } from "./ui";

type Ann =
  | { kind: "risky"; tags: string[]; ref: string }
  | { kind: "draft"; draft: Draft }
  | { kind: "new"; target: Selection };
type Item = CodeViewDiffItem<Ann>;

const INLINE_DRAFTS = new Set<Draft["status"]>(["draft", "approved", "posted"]);

const FILE_RAIL_DEFAULT_SIZE = 22;
const FILE_RAIL_MIN_SIZE = 12;
const FILE_RAIL_MAX_SIZE = 40;

const DIFF_HOST_STYLES = `
  .ade-diff-view diffs-container {
    --diffs-font-family: var(--font-mono) !important;
    --diffs-header-font-family: var(--font-mono) !important;
    --diffs-font-size: 12px !important;
    --diffs-line-height: 18px !important;
    --diffs-bg: var(--card) !important;
    --diffs-fg: var(--foreground) !important;
    --diffs-fg-number: var(--subtle) !important;
    border-bottom: 1px solid var(--border);
  }
`;

function splitFilePath(path: string): { dir: string; base: string } {
  const separator = path.lastIndexOf("/");
  return separator === -1
    ? { dir: "", base: path }
    : { dir: path.slice(0, separator + 1), base: path.slice(separator + 1) };
}

function countLines(fd: FileDiffMetadata): { additions: number; deletions: number } {
  let additions = 0;
  let deletions = 0;
  for (const h of fd.hunks) {
    additions += h.additionLines;
    deletions += h.deletionLines;
  }
  return { additions, deletions };
}

export function DiffPane({
  reviewKey,
  headSha,
  patch,
  risky,
  files,
}: {
  reviewKey: string;
  headSha: string;
  patch: string;
  risky: RiskyHunk[];
  files: FileStat[];
}) {
  const drafts = useReview((s) => s.drafts);
  const composing = useReview((s) => s.composing);
  const setComposing = useReview((s) => s.setComposing);
  const setSelection = useReview((s) => s.setSelection);
  const scrollRequest = useReview((s) => s.scrollRequest);
  const notify = useNotify();
  const viewRef = useRef<CodeViewHandle<Ann, undefined>>(null);
  const [diffStyle, setDiffStyle] = useState<"split" | "unified">("unified");
  const [showList, setShowList] = useState(false);
  const [active, setActive] = useState<string | null>(null);
  const [collapsedPaths, setCollapsedPaths] = useState<Set<string>>(() => new Set());

  // Parse once per patch: CodeView hydrates these objects in place when context is expanded.
  const fileDiffs = useMemo(
    () => parsePatchFiles(patch, `${reviewKey}:${headSha}`).flatMap((p) => p.files),
    [patch, reviewKey, headSha],
  );
  const byPath = useMemo(() => new Map(fileDiffs.map((fd) => [fd.name, fd])), [fileDiffs]);

  const annotations = useMemo(() => {
    const out = new Map<string, DiffLineAnnotation<Ann>[]>();
    const push = (path: string, a: DiffLineAnnotation<Ann>) => {
      const list = out.get(path);
      if (list) list.push(a);
      else out.set(path, [a]);
    };
    for (const r of risky) {
      const fd = byPath.get(r.path);
      if (!fd) continue;
      if (fd.type === "deleted") {
        push(r.path, { side: "deletions", lineNumber: 0, metadata: { kind: "risky", tags: r.tags, ref: r.ref } });
        continue;
      }
      for (const m of riskyMarks(fd, r.line, r.tags)) {
        push(r.path, { side: "additions", lineNumber: m.line, metadata: { kind: "risky", tags: m.tags, ref: r.ref } });
      }
    }
    for (const d of drafts) {
      if (!INLINE_DRAFTS.has(d.status) || !byPath.has(d.path)) continue;
      push(d.path, { side: d.side, lineNumber: d.line, metadata: { kind: "draft", draft: d } });
    }
    if (composing && byPath.has(composing.path)) {
      push(composing.path, { side: composing.side, lineNumber: composing.end, metadata: { kind: "new", target: composing } });
    }
    return out;
  }, [risky, drafts, composing, byPath]);

  // Items keep their identity; `version` bumps only when a file's annotations change.
  const versions = useRef(new Map<string, { sig: string; v: number }>());
  const items = useMemo<Item[]>(
    () =>
      fileDiffs.map((fd) => {
        const anns = annotations.get(fd.name) ?? [];
        const sig = JSON.stringify(anns);
        const prev = versions.current.get(fd.name);
        const v = !prev ? 0 : prev.sig === sig ? prev.v : prev.v + 1;
        versions.current.set(fd.name, { sig, v });
        const collapsed = collapsedPaths.has(fd.name);
        return { id: fd.name, type: "diff", fileDiff: fd, annotations: anns, collapsed, version: v * 2 + (collapsed ? 1 : 0) };
      }),
    [fileDiffs, annotations, collapsedPaths],
  );

  const jumpFiles = useMemo<JumpFile[]>(() => {
    const stats = new Map(files.map((f) => [f.path, f]));
    const riskyBy = new Map<string, number>();
    for (const r of risky) riskyBy.set(r.path, (riskyBy.get(r.path) ?? 0) + 1);
    const draftsBy = new Map<string, number>();
    for (const d of drafts) {
      if (d.status === "draft" || d.status === "approved") draftsBy.set(d.path, (draftsBy.get(d.path) ?? 0) + 1);
    }
    return fileDiffs.map((fd) => {
      const s = stats.get(fd.name) ?? countLines(fd);
      return {
        path: fd.name,
        additions: s.additions,
        deletions: s.deletions,
        risky: riskyBy.get(fd.name) ?? 0,
        drafts: draftsBy.get(fd.name) ?? 0,
      };
    });
  }, [fileDiffs, files, risky, drafts]);

  const totals = useMemo(
    () => jumpFiles.reduce((a, f) => ({ add: a.add + f.additions, del: a.del + f.deletions }), { add: 0, del: 0 }),
    [jumpFiles],
  );

  const loadDiffFiles = useCallback(
    async (fd: FileDiffMetadata): Promise<FileDiffLoadedFiles> => {
      const path = fd.name;
      try {
        const head = fd.type === "deleted" ? "" : await api.file(reviewKey, "head", path);
        const newFile = { name: path, contents: head, cacheKey: `head:${headSha}:${path}` };
        if (fd.type === "new" || fd.type === "rename-pure") return { oldFile: null, newFile };
        const base = await api.file(reviewKey, "base", path);
        return { oldFile: { name: fd.prevName ?? path, contents: base, cacheKey: `base:${headSha}:${path}` }, newFile };
      } catch (e) {
        notify("error", `Couldn't load ${path}: ${(e as Error).message}`);
        throw e;
      }
    },
    [reviewKey, headSha, notify],
  );

  // The gutter "+" opens a comment on the hovered line, the lines dragged over, or the current selection.
  const onGutterUtilityClick = useCallback(
    (range: SelectedLineRange, ctx: { item: { id: string } }) => {
      setComposing(selectionToPayload(ctx.item.id, range, byPath.get(ctx.item.id)));
    },
    [byPath, setComposing],
  );

  const options = useMemo(
    () => ({
      theme: { dark: "pierre-dark", light: "pierre-light" },
      themeType: "dark" as const,
      diffStyle,
      overflow: "wrap" as const,
      enableLineSelection: true,
      enableGutterUtility: true,
      onGutterUtilityClick,
      loadDiffFiles,
      stickyHeaders: true,
      hunkSeparators: "line-info-basic" as const,
      lineDiffType: "word" as const,
      expandUnchanged: false,
      collapsedContextThreshold: 2,
      expansionLineCount: 12,
      lineHoverHighlight: "line" as const,
      tokenizeMaxLineLength: 4000,
      layout: { paddingTop: 0, paddingBottom: 0, gap: 0 },
    }),
    [diffStyle, loadDiffFiles, onGutterUtilityClick],
  );

  const onSelectedLinesChange = useCallback(
    (sel: CodeViewLineSelection | null) => {
      setSelection(sel ? selectionToPayload(sel.id, sel.range, byPath.get(sel.id)) : null);
    },
    [byPath, setSelection],
  );

  // The composer clears the selection after sending or when its chip is removed.
  useEffect(
    () =>
      useReview.subscribe((s, prev) => {
        if (!s.selection && prev.selection) viewRef.current?.clearSelectedLines();
      }),
    [],
  );

  const jump = useCallback(
    (path: string, line?: number, side?: "additions" | "deletions") => {
      const view = viewRef.current;
      if (!view) return;
      if (!byPath.has(path)) {
        notify("warning", `${path} isn't in this diff`);
        return;
      }
      setActive(path);
      if (line && line > 0) {
        view.scrollTo({ type: "line", id: path, lineNumber: line, side: side ?? "additions", align: "center" });
      } else {
        view.scrollTo({ type: "item", id: path, align: "start" });
      }
    },
    [byPath, notify],
  );

  useEffect(() => {
    if (scrollRequest) jump(scrollRequest.path, scrollRequest.line, scrollRequest.side);
  }, [scrollRequest, jump]);
  const renderAnnotation = useCallback(
    (a: { metadata?: Ann; lineNumber: number }) => {
      const m = a.metadata;
      if (!m) return null;
      if (m.kind === "risky") {
        return (
          <div className="annotation" data-risky={m.ref} data-line={a.lineNumber}>
            <div className="risky-note">
              <span className="risky-label">Risky</span>
              {m.tags.map((t) => (
                <Tag key={t} tone="caution">
                  {t}
                </Tag>
              ))}
            </div>
          </div>
        );
      }
      if (m.kind === "new") {
        return (
          <div className="annotation">
            <NewComment reviewKey={reviewKey} target={m.target} />
          </div>
        );
      }
      return (
        <div className="annotation">
          <DraftAnnotation draft={m.draft} reviewKey={reviewKey} />
        </div>
      );
    },
    [reviewKey],
  );

  const renderHeader = useCallback(
    (item: Item) => {
      const { dir, base } = splitFilePath(item.id);
      const counts = countLines(item.fileDiff);
      const collapsed = item.collapsed ?? false;

      return (
        <div className="diff-file-header">
          <button
            type="button"
            className="diff-collapse"
            aria-label={`${collapsed ? "Expand" : "Collapse"} ${item.id}`}
            aria-expanded={!collapsed}
            onClick={() =>
              setCollapsedPaths((paths) => {
                const next = new Set(paths);
                if (next.has(item.id)) next.delete(item.id);
                else next.add(item.id);
                return next;
              })
            }
          >
            {collapsed ? <ChevronRight /> : <ChevronDown />}
          </button>
          <span className="diff-file-name" title={item.id}>
            <span className="diff-file-dir">{dir}</span>
            <span>{base}</span>
          </span>
          <span className="diff-file-stats">
            {counts.deletions > 0 && <span className="dels">−{counts.deletions}</span>}
            {counts.additions > 0 && <span className="adds">+{counts.additions}</span>}
          </span>
        </div>
      );
    },
    [],
  );

  return (
    <div className="diff-pane">
      <div className="diff-toolbar">
        <Button
          variant="ghost"
          size="icon"
          aria-label={showList ? "Collapse file tree" : "Show file tree"}
          title={showList ? "Collapse file tree" : "Show file tree"}
          onClick={() => setShowList(!showList)}
        >
          {showList ? <PanelLeftClose /> : <PanelLeftOpen />}
        </Button>
        <span className="diff-count">
          {fileDiffs.length} {fileDiffs.length === 1 ? "file" : "files"}
        </span>
        <span className="diff-total">
          <span className="dels">−{totals.del}</span>
          <span className="adds">+{totals.add}</span>
        </span>
        <span className="spacer" />
        <div className="seg" role="group" aria-label="Diff layout">
          <button type="button" className={diffStyle === "split" ? "on" : ""} onClick={() => setDiffStyle("split")}>
            Split
          </button>
          <button type="button" className={diffStyle === "unified" ? "on" : ""} onClick={() => setDiffStyle("unified")}>
            Unified
          </button>
        </div>
      </div>
      <style>{DIFF_HOST_STYLES}</style>
      <PanelGroup className="diff-body" direction="horizontal" autoSaveId="pair:diff-body">
        {/* Rendered conditionally rather than as a collapsible panel: a collapsed
            collapsible panel re-expands to its minSize whenever the group resizes. */}
        {showList && (
          <>
            <Panel
              id="file-rail"
              order={1}
              className="jump-panel"
              defaultSize={FILE_RAIL_DEFAULT_SIZE}
              minSize={FILE_RAIL_MIN_SIZE}
              maxSize={FILE_RAIL_MAX_SIZE}
            >
              <FileJumpList files={jumpFiles} active={active} onJump={(p) => jump(p)} />
            </Panel>
            <ResizeHandle label="Resize file list" />
          </>
        )}
        <Panel id="diff-code" order={2} minSize={40}>
          <CodeView<Ann, undefined>
            ref={viewRef}
            className="code-view ade-diff-view"
            items={items}
            options={options}
            onSelectedLinesChange={onSelectedLinesChange}
            renderAnnotation={renderAnnotation}
            renderCustomHeader={renderHeader}
          />
        </Panel>
      </PanelGroup>
    </div>
  );
}
