"""Stack screenshots into labeled contact sheets (token-efficient triage).

One 3x3 sheet replaces nine single-image reads for a vision model, cutting
review tokens roughly by the grid factor. Cells are scaled to a sheet width
vision models handle well (~1600-2000 px); per-cell text is NOT expected to
be readable at that scale — sheets are for layout/state triage. Each cell is
labeled with its source filename so a judge can flag "sheet 02, cell 5" and
the coordinator expands exactly those shots at full resolution.

Usage:
  python contact_sheets.py "C:/Temp/walk/*.png" --out C:/Temp/walk/sheets
  python contact_sheets.py C:/Temp/walk --grid 2x2 --width 1800
  python contact_sheets.py C:/Temp/walk --dedup          # drop near-dupes
  python contact_sheets.py C:/Temp/walk --dedup 0.99
  python contact_sheets.py C:/Temp/walk --dedup --start-server

Requires Pillow (pip install pillow). Writes sheets/sheet_NN.png plus
sheets/index.json mapping (sheet, grid position) -> source filename.

With --dedup [threshold] (default 0.985), near-duplicate shots (cosine >=
threshold under the local embedding server, see embeddings.py) are dropped:
they get no sheet cell and are annotated in index.json as "dup of NNN".
Exact byte-identical shots are always dropped by sha256 first — that part
works with NO server; if the endpoint is unreachable only semantic dedup
is skipped (with a warning). --start-server makes the tool manage the
server's lifecycle itself: start before dedup, stop after; a server that
was already running is left alone.
"""
import argparse
import glob
import json
import os
import sys
from contextlib import nullcontext

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Pillow is required: pip install pillow")

from embeddings import (EmbedUnavailable, cosine, embed_files,
                        embedding_server, file_hashes)

DEDUP_DEFAULT = 0.985


def dup_note(dup, orig):
    """Readable annotation, e.g. 'dup of 003 (0.99)' / 'dup of 003 (identical)'."""
    n = f"{orig[dup['dup_of']]:03d}"
    if dup["score"] is None:
        return f"dup of {n} (identical)"
    return f"dup of {n} ({dup['score']:.2f})"


def dedupe(files, threshold, cache_dir, orig):
    """Drop exact and near-duplicate shots -> (kept, dups).

    Pass 1 is pure sha256: byte-identical files lose to their first
    occurrence, no server involved. Pass 2 embeds what is left and drops
    any shot whose cosine to the previous KEPT shot reaches threshold.
    """
    dups, kept, seen = [], [], {}
    for f in files:
        h = file_hashes([f])[f]
        if h in seen:
            dups.append({"file": f, "dup_of": seen[h], "score": None})
        else:
            seen[h] = f
            kept.append(f)
    try:
        vecs = embed_files(kept, cache_dir=cache_dir)
    except EmbedUnavailable as e:
        print(f"WARNING: semantic dedup skipped ({e})", file=sys.stderr)
        return kept, dups
    survivors, prev = [], None
    for f in kept:
        if prev is not None:
            score = cosine(vecs[f], vecs[prev])
            if score >= threshold:
                dups.append({"file": f, "dup_of": prev, "score": score})
                continue
        prev = f
        survivors.append(f)
    return survivors, dups


def natural_key(path):
    """Sort 001_foo.png before 010_bar.png (string sort puts 10 first)."""
    import re
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", os.path.basename(path))]


def build(src, out_dir, cols, rows, sheet_width, exts=(".png", ".jpg", ".jpeg"),
          dedup=None, start_server=False):
    if os.path.isdir(src):
        files = [os.path.join(src, f) for f in os.listdir(src)
                 if f.lower().endswith(exts)]
    else:
        files = []
        for pattern in src if isinstance(src, (list, tuple)) else [src]:
            files.extend(glob.glob(pattern))
        files = [f for f in files if f.lower().endswith(exts)]
    files = sorted(set(files), key=natural_key)
    if not files:
        sys.exit(f"no images matched: {src}")

    # stable shot numbers (1-based position in the full sorted list) so
    # labels and dup notes keep pointing at the same PNG with or without
    # duplicates removed
    orig = {f: i + 1 for i, f in enumerate(files)}
    dups = []
    if dedup is not None:
        # --start-server owns the server's lifecycle: start before dedup,
        # stop after (always, also on error); pre-existing servers are
        # left alone — that is embedding_server()'s None-yield contract
        with embedding_server() if start_server else nullcontext():
            files, dups = dedupe(files, dedup, out_dir, orig)
        for d in dups:
            print(f"  {os.path.basename(d['file'])}: {dup_note(d, orig)}")
        print(f"dedup: {len(dups)} of {len(orig)} shots dropped "
              f"(threshold {dedup})")

    os.makedirs(out_dir, exist_ok=True)
    per_sheet = cols * rows
    cell_w = sheet_width // cols
    index = {"sheet_width": sheet_width, "grid": f"{cols}x{rows}",
             "sheets": [],
             "duplicates": [{"file": os.path.basename(d["file"]),
                             "dup_of": os.path.basename(d["dup_of"]),
                             "note": dup_note(d, orig)} for d in dups]}
    n_sheets = 0

    for start in range(0, len(files), per_sheet):
        batch = files[start:start + per_sheet]
        # cell height from the first image's aspect ratio; odd sizes padded
        probes = [Image.open(f) for f in batch[:4]]
        cell_h = max(int(p.height * cell_w / p.width) for p in probes)
        for p in probes:
            p.close()
        sheet = Image.new("RGB", (sheet_width, cell_h * rows), (24, 24, 28))
        draw = ImageDraw.Draw(sheet)
        entries = []

        for i, f in enumerate(batch):
            r, c = divmod(i, cols)
            img = Image.open(f).convert("RGB")
            scale = min(cell_w / img.width, cell_h / img.height)
            img = img.resize((max(1, int(img.width * scale)),
                              max(1, int(img.height * scale))))
            x = c * cell_w + (cell_w - img.width) // 2
            y = r * cell_h + (cell_h - img.height) // 2
            sheet.paste(img, (x, y))
            label = f"[{orig[f]:03d}] {os.path.basename(f)}"
            draw.rectangle([c * cell_w, r * cell_h,
                            c * cell_w + cell_w - 1, r * cell_h + 22],
                           fill=(10, 10, 12))
            draw.text((c * cell_w + 6, r * cell_h + 4), label,
                      fill=(255, 220, 80))
            draw.rectangle([c * cell_w, r * cell_h,
                            c * cell_w + cell_w - 1,
                            r * cell_h + cell_h - 1], outline=(70, 70, 80))
            entries.append({"cell": i + 1, "index": orig[f],
                            "file": os.path.basename(f)})

        out_path = os.path.join(out_dir, f"sheet_{n_sheets + 1:02d}.png")
        sheet.save(out_path)
        index["sheets"].append({"sheet": os.path.basename(out_path),
                                "cells": entries})
        print(f"{os.path.basename(out_path)}: {len(batch)} shots")
        n_sheets += 1
        sheet.close()

    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=1)
    if n_sheets:
        print(f"{n_sheets} sheets for {len(files)} shots "
              f"({len(files) / n_sheets:.1f} shots/sheet) -> {out_dir}")
    else:
        print(f"no sheets — every shot was a duplicate -> {out_dir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", help="image dir or glob pattern(s)")
    ap.add_argument("--out", default=None, help="sheets output dir "
                    "(default: <src>/sheets)")
    ap.add_argument("--grid", default="3x3", help="cols x rows, default 3x3")
    ap.add_argument("--width", type=int, default=1920,
                    help="sheet width in px, default 1920")
    ap.add_argument("--dedup", nargs="?", const=DEDUP_DEFAULT, type=float,
                    default=None, metavar="THRESHOLD",
                    help="drop near-duplicate shots (cosine >= threshold, "
                         f"default {DEDUP_DEFAULT}); needs the local "
                         "embedding server, exact byte-identical shots are "
                         "dropped regardless")
    ap.add_argument("--start-server", action="store_true",
                    help="with --dedup: start the local embedding server "
                         "before the run and stop it afterwards; a server "
                         "that is already running is left alone")
    args = ap.parse_args()
    cols, rows = (int(x) for x in args.grid.lower().split("x"))
    out_dir = args.out or os.path.join(
        args.src if os.path.isdir(args.src) else os.path.dirname(args.src)
        or ".", "sheets")
    build(args.src, out_dir, cols, rows, args.width, dedup=args.dedup,
          start_server=args.start_server)


if __name__ == "__main__":
    main()
