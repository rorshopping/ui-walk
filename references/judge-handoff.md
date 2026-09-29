# Judge handoff — token-efficient visual review

## The math

A vision agent reading a ~800×800 PNG costs roughly 1.1–1.6k tokens per
image (plus prompt). A 141-shot gallery read one-by-one is ~180k tokens of
image input per full pass — and thorough reviews want more than one pass.
Contact sheets collapse that: a 3×3 sheet at 1920 px wide costs about the
same as ONE full-res image but shows nine shots. A full gallery then reads
as ~16 sheets (~20k tokens), and only flagged cells (typically 10–20%) get
expanded to full resolution.

The tradeoff: text inside a 3×3 cell is not readable. Sheets answer
*layout and state* questions ("is the panel empty, overlapping, clipped,
showing an error state, missing data?"). They cannot answer text-level
questions ("does the label say X?"). That's why the review is two passes.

## Grid sizing

| Shots are... | Grid | Why |
|---|---|---|
| full-window (≥700 px wide) | 3×3 at 1920 px | each cell ≈ 640 px — layout is clearly readable |
| panels/tall shots | 2×2 at 1600 px | cells stay ~800 px, text becomes squint-readable |
| anything you'll deep-read anyway | don't stack | stacking pays off only when most cells are fine |

Never exceed ~2000 px sheet width: larger images get downscaled by vision
models and you lose the savings AND the detail.

## Pass 1 — triage on sheets (one judge agent per ~8 sheets)

Prompt template:

> You are the visual triage judge for <APP>, a <what it is>. These are
> contact sheets: each cell is one screenshot of the app, labeled
> `[NNN] filename` at the top. The walk report (attached/passed as path)
> lists which user action produced each shot.
>
> Read every sheet in <sheets dir>. For each cell judge: empty panes that
> should have data, overlapping/clipped widgets, broken glyphs, error
> states, layout collapse. Text in cells is too small to read — judge
> structure and state only, and do NOT guess at cell text.
>
> Return one line per SUSPECT cell only:
> `{"sheet": "sheet_02.png", "cell": 5, "file": "041_tool_Join.png",
>   "suspect": "<one-line reason>"}`
> Then a one-paragraph overall health summary. If a sheet is entirely
> clean, just name it as clean.

## Pass 2 — deep-read flagged cells (one judge agent per ~15 flagged shots)

> You are the visual acceptance judge for <APP>. These full-resolution
> screenshots were flagged by a triage pass for: <reasons>. For each, judge
> at user-acceptance bar: text readable? controls render correctly? is
> this a real defect or acceptable state? Return
> `{"file": "...", "verdict": "pass|fail", "issues": [...]}` per image and
> a ranked top-5 fix list across all of them.

## Coordinator rules

- Spawn pass-1 judges in ONE message, run_in_background, and do nothing
  else with the screenshots yourself (the judges are the review).
- Expand only `fail`/`suspect` cells to pass 2; a clean sheet is done.
- Carry the walk report paths into both prompts — knowing WHICH action
  produced a screenshot doubles the value of every finding.
- After fixes, re-run only the affected phase (`--only <phase>`) and
  re-judge those sheets, not the whole gallery.
