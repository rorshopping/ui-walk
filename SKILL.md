---
name: ui-walk
description: Walk an app's UI the way a user would — click through every menu, tool, and dialog end-to-end, screenshot each state, stack screenshots into contact sheets, and hand off to visual-judge subagents for a user-perspective QA verdict. Use whenever the user asks to review, test, or QA an app's UI from a user perspective, click all the buttons, walk the menus, take screenshots of an app, find UI bugs, or visually accept a desktop or web UI — for Python/Qt apps in-process AND any Windows desktop app via UI Automation (Notepad, Electron, WPF, Win32 — anything with a window). Also use when asked to save tokens on screenshot review by stacking images into grids.
---

# ui-walk — user-perspective UI walking + screenshot review

A QA harness that explores an app like a cautious first-time user: triggers
every menu action, inserts every palette tool, opens every dialog, performs
a scripted end-to-end task — photographing every state — then stacks the
screenshots into contact sheets so review agents read ~N/9 images instead of N.

## Workflow

1. **Pick the driver** for the target app:
   - **Python/Qt app (in-process, headless-safe)** → `driver_pyqt` — boots the
     real `QMainWindow` offscreen, patches native file/message dialogs to
     deterministic answers, walks QActions/tool palettes with a modal-closer
     "hammer". Zero new windows on the user's desktop.
   - **Any Windows app with a window** → `driver_uia` — launches (or attaches
     to) the app and drives it via Windows UI Automation (works for Win32,
     WPF, WinForms, Qt, and Electron/Chromium apps that expose accessibility).
     Requires an interactive desktop session; screenshots come from the real
     screen, so this is NOT headless.

   Read the driver's header (`scripts/driver_pyqt.py`, `scripts/driver_uia.py`)
   before first use — each documents its config JSON and limits.

2. **Run the walk** (partitioned into fresh processes with one retry per
   phase — long UI churn trips native fail-fasts on some stacks; see the
   "partition + retry" note in `scripts/run_walk.py`):

   ```bash
   # Qt app
   python scripts/run_walk.py pyqt --app-config app.json --out C:/Temp/walk
   # Any Windows app
   python scripts/run_walk.py uia --launch "notepad.exe" --out C:/Temp/walk
   ```

   Output: numbered PNGs (`001_<step>.png`…), `ui_walk_report_*.json` with
   one `{step, ok, detail, png}` record per action, and contact sheets.

3. **Stack the screenshots** (token savings: a 141-shot gallery reads as ~16
   sheets instead of 141 single images):

   ```bash
   python scripts/contact_sheets.py "C:/Temp/walk/*.png" --out C:/Temp/walk/sheets --grid 3x3
   ```

   Keep sheets ≤ ~2000 px wide (vision models downscale large images).
   Rule of thumb: 3×3 for full-window shots, 2×2 when you need panel text
   roughly legible. **Triage, then zoom**: judge agents read the sheets,
   flag suspect cells by their printed index, and only then Read the
   individual full-resolution PNGs that were flagged. This is where the
   token savings actually come from — not from judging everything twice.

4. **Hand off to visual-judge subagents** (spawn several in ONE message,
   running in background). Prompt templates for the two passes are in
   `references/judge-handoff.md` — pass the sheets directory, the per-phase
   report JSONs, and what the app is supposed to do. Require one JSON verdict
   line per image/cell plus a ranked top-5 problems list.

5. **Fix → re-shoot → re-judge.** The walk is deterministic (seeded, patched
   dialogs), so a re-run after fixes produces a directly comparable gallery.
   Re-run only the affected phase with `--only <phase>` when that is enough.

## Safety rails (both drivers)

- Blocklist regex skips destructive actions (exit/quit/delete/format/...;
  default list in the drivers, extendable via `--blocklist`).
- The Qt driver sandboxes app data (`BAETL_APPDATA_DIR`-style env hooks are
  the app's own concern; the driver never writes inside the target repo —
  save-file paths are redirected to the output dir).
- The UIA driver never types into or invokes elements whose name matches the
  blocklist, and `--dry-run` lists the planned clicks without executing.

## Outputs

- `NNN_<step>.png` — full-resolution screenshots, stable ordering.
- `ui_walk_report_<phase>.json` — machine-readable per-step results.
- `sheets/sheet_NN.png` + `sheets/index.json` — contact sheets + the
  cell→filename map judges use to expand flagged cells.

## Limits worth telling the user

- Contact sheets are for **layout/state triage**; pixel text in a 3×3 cell of
  a 796-px window is not readable. Deep text checks happen on flagged
  full-res shots.
- `driver_uia` drives the real desktop: don't move the mouse/keyboard during
  a walk, and expect the target app's window to flash.
- Web apps: use the browser automation skill instead; this skill is desktop.
