# ui-walk

**Walk an app's UI the way a user would — screenshot every state, then review it
at ~1/9th the tokens.**

An [OpenCode](https://opencode.ai) skill that explores a desktop app like a
cautious first-time user: triggers every menu action, inserts every tool,
opens every dialog, runs a scripted end-to-end task — photographing each state.
Then it stacks the screenshots into labeled contact sheets and hands off to
parallel visual-judge subagents for a user-perspective QA verdict.

The point: **GUI QA that scales.** Screenshots are the only honest way to judge
a UI, but reading 141 full-resolution PNGs costs ~180k tokens per pass. Contact
sheets collapse that to ~20k, and only flagged cells get expanded.

## What's in the box

```
SKILL.md                      the skill contract (YAML frontmatter + workflow)
scripts/
  driver_pyqt.py              in-process PyQt/Qt driver (offscreen, headless-safe)
  driver_uia.py               any Windows app via Windows UI Automation
  run_walk.py                 orchestrator: partitioned phases, retries, sheets
  contact_sheets.py           labeled 3x3 / 2x2 stacking + index.json (+ --dedup)
  embeddings.py               local EmbeddingGemma client: cache + cosine + dedup
  compare_runs.py             cross-run diff: unchanged / changed / unpaired shots
  test_embeddings.py          unittest suite (no network, no server needed)
references/
  judge-handoff.md            prompt templates + coordinator rules for the judges
```

## Two drivers, two targets

| Driver | Target | How | Headless? |
|---|---|---|---|
| `driver_pyqt` | Python/Qt apps | boots the real `QMainWindow` **offscreen**, patches native file/message dialogs to deterministic answers, walks `QAction`s and tool palettes | **yes** — zero new windows on your desktop |
| `driver_uia` | any Windows app with a window | launches or attaches, drives the accessibility tree the same layer screen readers use. Win32, WPF, WinForms, Qt, Electron/Chromium | **no** — real desktop, real screenshots |

## Install

Copy the folder into your OpenCode skills directory:

```powershell
# Windows
git clone https://github.com/rorshopping/ui-walk "$env:USERPROFILE\.config\opencode\skills\ui-walk"
```

```bash
# macOS / Linux
git clone https://github.com/rorshopping/ui-walk ~/.config/opencode/skills/ui-walk
```

Requires Python 3.9+. `driver_pyqt` needs `PyQt6`/`PySide6` (and `Pillow` for
screenshots). `driver_uia` needs Windows PowerShell 5.1+ — it is built in, no
pip installs. `contact_sheets.py` needs `Pillow`.

## Use

The agent picks the driver and runs the walk for you. By hand:

```bash
# Qt app (in-process, offscreen)
python scripts/run_walk.py pyqt --app-config app.json --out C:/Temp/walk

# any Windows app (real desktop)
python scripts/run_walk.py uia --launch "notepad.exe" --out C:/Temp/walk
python scripts/run_walk.py uia --attach "Untitled - Notepad" --dry-run
```

Stack the gallery:

```bash
python scripts/contact_sheets.py "C:/Temp/walk/*.png" --out C:/Temp/walk/sheets --grid 3x3

# or drop near-duplicate states (repeated screens waste sheet cells):
python scripts/contact_sheets.py C:/Temp/walk --dedup            # cosine >= 0.985
python scripts/contact_sheets.py C:/Temp/walk --dedup 0.99
```

`--dedup` embeds the shots with a local embedding server (EmbeddingGemma 2
behind an OpenAI-compatible `/v1/embeddings` endpoint — see
`scripts/embeddings.py` for the one-line `llama-server` start command;
image input needs the mmproj). Vectors cache to `embeddings.jsonl` keyed by
file + mtime, so re-runs are free. Byte-identical shots are always dropped
by sha256 first — that fallback works with **no server**; if the endpoint is
unreachable only the semantic pass is skipped (with a warning). Duplicates
get no sheet cell and are annotated in `sheets/index.json`
(`"note": "dup of 003 (0.99)"`).

### Diff two runs

After a fix round, compare the new walk against the baseline so only changed
screens need re-judging:

```bash
python scripts/compare_runs.py --a C:/Temp/walk_before --b C:/Temp/walk_after
python scripts/compare_runs.py --a old --b new --threshold 0.97
```

It pairs every run-B shot to its nearest run-A shot by cosine similarity and
writes `compare_report.json` plus a stdout table: UNCHANGED pairs
(score >= threshold), CHANGED pairs (re-judge these), and unpaired shots
(removed or renumbered screens).

### `app.json` for the Qt driver

```json
{
  "module": "src.gui.main_window",
  "class": "MainWindow",
  "env": { "MYAPP_DATA_DIR": "C:/Temp/walk_appdata" },
  "window": { "width": 1680, "height": 1050 },
  "steps": [
    { "call": "_load_from_file", "args": ["samples/demo.json"] },
    { "call": "fit_view" },
    { "shot": "demo_loaded" },
    { "call": "run_graph" },
    { "wait": 5 },
    { "shot": "after_run" }
  ],
  "tools": { "catalog": "src.core.tool_catalog:CATEGORIES", "add": "add_node_auto_connect", "panel": "props_dock" }
}
```

## Outputs

- `NNN_<step>.png` — full-resolution screenshots, stable ordering
- `ui_walk_report_<phase>.json` — machine-readable per-step `{step, ok, detail, png}`
- `sheets/sheet_NN.png` + `sheets/index.json` — contact sheets and the cell→file map (with `--dedup`: a `duplicates` list of skipped shots)
- `compare_report.json` — cross-run pair table from `compare_runs.py`

## The two-pass review

1. **Triage on sheets.** Spawn several judge subagents on the sheets, one per
   ~8 sheets. They judge *layout and state* — empty panes, overlap, clipping,
   broken glyphs, layout collapse — and return one JSON line per suspect cell.
2. **Deep-read the flagged cells.** Expand only `fail`/`suspect` cells to
   full resolution, one judge per ~15 shots, at user-acceptance bar.

Exact prompt templates and coordinator rules: [`references/judge-handoff.md`](references/judge-handoff.md).

## Safety rails

- A blocklist regex skips destructive verbs (exit, quit, delete, format, …) by
  default; extend with `--blocklist`.
- The Qt driver never writes inside your repo — app-data env hooks and save-file
  paths are redirected to the output dir.
- The UIA driver will not type into or invoke blocklisted elements, and
  `--dry-run` prints the planned clicks without executing any.

## Limits

- Contact sheets answer **layout/state** questions, not text questions. A 3×3
  cell of a 796px window has unreadable text — that's what pass 2 is for.
- `driver_uia` drives the real desktop: don't touch mouse or keyboard mid-walk,
  and expect the target window to flash.
- Keep sheets ≤ ~2000px wide; vision models downscale larger images and you lose
  both the savings and the detail.
- **Web apps?** Use a browser automation skill. This one is desktop.

## License

MIT
