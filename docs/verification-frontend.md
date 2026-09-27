# F0 verification (Sep 24, 2026)

Versions: Bun 1.3.14, Next 16.3.6 (Turbopack, `output: "export"`), React 19.3.0, `@hackerrank/hrds-components` 10.3.3, `@hackerrank/hrds-styles-v3` 1.1.2, `@pierre/diffs` 1.4.3, Vitest 5.0.1. The browser checks ran in headless Chrome, driven by `playwright-core` from a git-ignored scratch folder.

## 1. Installing from Nexus

- `bun install` resolves `@hackerrank/*` and everything else through the registry in `~/.npmrc`, read as is. The repo has no `.npmrc`.
- `web/bun.lock` records 560 tarball URLs. All of them are on the Nexus host and none are on `registry.npmjs.org`, so an install on a machine without Nexus access will fail. The lockfile has no auth keys or tokens.
- `ignore-scripts=true` changes nothing: none of the installed packages has an install script. Bun also skips dependency lifecycle scripts by default.
- hrds declares React peer ranges that stop before 19, so `bun install` prints peer warnings. They're harmless; see the next section.

## 2. hrds on a Next 16 static export with React 19

Yes. hrds 10.3.3 with styles-v3 1.1.2 builds under the static export with no overrides, and the `9.62.1-retheme-3` fallback wasn't needed.

- `HRButton`, `HRAccordion`, `HRDrawer` (the drafts and excerpt drawers), `HRConfirm`, `HRTag` and `HRSpinner` render in the dark theme (body background `rgb(18, 20, 24)`) with no console errors or warnings.
- The dark v3 tokens come from `@hackerrank/hrds-styles-v3/dist/main-with-theme.css`, imported in `app/layout.tsx`.
- `HRThemeProvider` sets `data-theme` and `data-theme-version` on `<body>` in an effect, so the first paint would be unthemed. The layout also sets `data-theme="dark" data-theme-version="3"` on `<body>` directly.

Gotchas:

- `HRHint`'s `variant` takes the exported `HRHintVariant` enum; a string literal doesn't type-check.
- `HRConfirm.Footer`'s props are typed as `Pick<any, ...>`, which makes every prop required. The confirm dialog uses a plain `div` footer instead.
- `useToast().notify` changes identity on every render. Anything that depends on it re-runs each render; this canceled pending `CodeView` scrolls. `lib/useNotify.ts` wraps it in a stable callback.

## 3. `CodeView` on awt#1

awt#1's `diff.patch` has 3,534 lines across 23 files. `CodeView` renders it smoothly in the export, and line selection works.

- The PR title appears 0.34 to 0.6 s after navigation, and the first diff lines 0.37 to 0.66 s after.
- A wheel scroll from top to bottom (about 68,500 px, 170 steps) took 11.1 s. The 95th-percentile frame time was 33.4 ms, which is headless Chrome's fixed 30 fps cadence, and there was one long task of 50 ms. A worker pool wasn't needed.
- Selection gave the right path, side, and line range in all six cases tried: additions only, deletions only, and a range crossing sides, each in split and unified view. A range that crosses sides is mapped to the additions side.
- Context expansion works through `loadDiffFiles` and the `file` endpoint. Expanding one gap in a file grew it from 22 to 27 rendered lines.

Two settings are required:

- The `CodeView` root needs `overflow: auto` and a fixed height. Without them the pane doesn't scroll and `scrollTo` does nothing: the component's internal scroll position changes while the element's `scrollTop` stays at 0.
- `options` must be memoized and keep its identity. A new `options` object cancels a pending `scrollTo`.

## 4. Brief item extra fields

From `src/pair/extract.py`, `src/pair/brief.py` and the section agents:

| Section | Extra fields |
| --- | --- |
| `ask` | `session`, `at` |
| `plan_vs_result` | `kind`: `not_done`, `not_planned` or `changed` |
| `decisions` | `question`, `answer`, `options`, `superseded`, `superseded_by` |
| `verification` | `status` (`passed`, `failed`, `stale` or `never_run`), `runs`, `last_run`, `command` |
| `pushback` | `supersedes` (decision refs; only refs that name a decision are kept) |
| `churn` | `path`, `edits`, `turns` |

Every item also has `text` and `refs`. The UI renders the fields above and ignores any others.
