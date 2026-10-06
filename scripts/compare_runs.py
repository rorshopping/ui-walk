"""compare_runs — which screenshots changed between two walk runs?

Embeds both runs' PNGs with the local embedding server (cache-aware — see
embeddings.py), pairs every run-B shot to its nearest run-A shot by cosine
similarity, and reports unchanged / changed pairs plus unpaired shots, so
only changed screens need re-judging after a fix round.

Usage:
  python compare_runs.py --a C:/Temp/walk_before --b C:/Temp/walk_after
  python compare_runs.py --a old --b new --threshold 0.97
  python compare_runs.py --a old --b new --start-server

Writes compare_report.json (default: into run B's dir, override with --out)
and prints a table: UNCHANGED rows (score >= threshold), CHANGED rows
(score below — re-judge these), and UNPAIRED shots with no partner.

Requires the local embedding endpoint (see embeddings.py for the one-liner);
vectors are cached per run dir, so repeated compares cost nothing.
--start-server makes the tool manage the server's lifecycle itself: start
before the compare, stop after; a server that was already running is left
alone.
"""
import argparse
import json
import os
import re
import sys
from contextlib import nullcontext

from embeddings import EmbedUnavailable, cosine, embed_files, embedding_server


def natural_key(path):
    """Sort 001_foo.png before 010_bar.png (string sort puts 10 first)."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", os.path.basename(path))]


def shots(run_dir, exts=(".png",)):
    """Sorted screenshot paths of one run (natural order)."""
    if not os.path.isdir(run_dir):
        sys.exit(f"not a directory: {run_dir}")
    return sorted([os.path.join(run_dir, f) for f in os.listdir(run_dir)
                   if f.lower().endswith(exts)], key=natural_key)


def pair_shots(keys_a, vecs_a, keys_b, vecs_b):
    """Nearest-neighbor pairing: every B shot -> its most similar A shot.

    Returns (pairs, unpaired_a): pairs is a list of (b_idx, a_idx, score)
    in B order (a B shot with no A shots at all gets no entry); unpaired_a
    lists A indices that no B shot chose — those screens were removed or
    renumbered in run B.
    """
    pairs, chosen = [], set()
    for j, kb in enumerate(keys_b):
        best_i, best_s = None, -1.0
        for i, ka in enumerate(keys_a):
            s = cosine(vecs_b[kb], vecs_a[ka])
            if s > best_s:
                best_i, best_s = i, s
        if best_i is not None:
            pairs.append((j, best_i, best_s))
            chosen.add(best_i)
    unpaired_a = [i for i in range(len(keys_a)) if i not in chosen]
    return pairs, unpaired_a


def classify(pairs, keys_a, keys_b, threshold):
    """Split pairs into unchanged (>= threshold) and changed rows."""
    unchanged, changed = [], []
    for j, i, s in pairs:
        row = {"a": os.path.basename(keys_a[i]),
               "b": os.path.basename(keys_b[j]),
               "score": round(s, 4)}
        (unchanged if s >= threshold else changed).append(row)
    return unchanged, changed


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--a", required=True, help="earlier run dir (baseline)")
    ap.add_argument("--b", required=True, help="later run dir (compare)")
    ap.add_argument("--threshold", type=float, default=0.97,
                    help="cosine >= threshold counts as unchanged "
                         "(default 0.97)")
    ap.add_argument("--out", default=None,
                    help="report path (default: <b>/compare_report.json)")
    ap.add_argument("--start-server", action="store_true",
                    help="start the local embedding server before the run "
                         "and stop it afterwards; a server that is already "
                         "running is left alone")
    args = ap.parse_args()

    keys_a, keys_b = shots(args.a), shots(args.b)
    if not keys_a and not keys_b:
        sys.exit("both runs are empty — nothing to compare")
    with embedding_server() if args.start_server else nullcontext():
        try:
            vecs_a = embed_files(keys_a, cache_dir=args.a)
            vecs_b = embed_files(keys_b, cache_dir=args.b)
        except EmbedUnavailable as e:
            sys.exit(str(e))

    pairs, unpaired_a = pair_shots(keys_a, vecs_a, keys_b, vecs_b)
    unpaired_b = [j for j in range(len(keys_b))
                  if not any(p[0] == j for p in pairs)]
    unchanged, changed = classify(pairs, keys_a, keys_b, args.threshold)

    print(f"pairing {len(keys_b)} B shots against {len(keys_a)} A shots "
          f"(threshold {args.threshold})")
    for row in unchanged:
        print(f"  UNCHANGED  {row['b']} <-> {row['a']}  {row['score']:.3f}")
    for row in changed:
        print(f"  CHANGED    {row['b']} <-> {row['a']}  {row['score']:.3f}")
    for i in unpaired_a:
        print(f"  UNPAIRED-A {os.path.basename(keys_a[i])}")
    for j in unpaired_b:
        print(f"  UNPAIRED-B {os.path.basename(keys_b[j])}")
    print(f"summary: {len(unchanged)} unchanged, {len(changed)} changed, "
          f"{len(unpaired_a)} unpaired A, {len(unpaired_b)} unpaired B")

    report = {"run_a": os.path.abspath(args.a),
              "run_b": os.path.abspath(args.b),
              "threshold": args.threshold,
              "unchanged": unchanged, "changed": changed,
              "unpaired_a": [os.path.basename(keys_a[i]) for i in unpaired_a],
              "unpaired_b": [os.path.basename(keys_b[j]) for j in unpaired_b]}
    out_path = args.out or os.path.join(args.b, "compare_report.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    print(f"report -> {out_path}")


if __name__ == "__main__":
    main()
