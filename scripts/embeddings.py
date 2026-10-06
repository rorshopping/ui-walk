"""Local multimodal embeddings for screenshot comparison (stdlib only).

Talks to any OpenAI-compatible POST /v1/embeddings endpoint. The intended
backend is a local EmbeddingGemma 2 server:

  llama-server -hf ggml-org/embeddinggemma-2-GGUF --embeddings

(image input needs the mmproj; without it the server rejects image parts and
EmbedUnavailable says so). Images travel as base64 data URIs in small
batches; results are cached on disk keyed by file + mtime, so unchanged
shots never re-embed. Byte-identical files are embedded once and share a
vector — that part needs no server at all.

Env:
  UIWALK_EMBED_URL    default http://127.0.0.1:8091/v1/embeddings
  UIWALK_EMBED_MODEL  default embeddinggemma-2
"""
import base64
import hashlib
import json
import mimetypes
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8091/v1/embeddings"
DEFAULT_MODEL = "embeddinggemma-2"
BATCH = 8        # images per request — keeps payloads sane
TIMEOUT = 120    # seconds per batch request
CACHE_NAME = "embeddings.jsonl"


class EmbedUnavailable(Exception):
    """The embeddings endpoint is unreachable or rejected the request."""


def embed_url():
    return os.environ.get("UIWALK_EMBED_URL", DEFAULT_URL)


def embed_model():
    return os.environ.get("UIWALK_EMBED_MODEL", DEFAULT_MODEL)


def cosine(a, b):
    """Cosine similarity; 0.0 when either vector is all zeros."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def file_hashes(paths):
    """sha256 byte hash per path — exact-duplicate detection, no server."""
    out = {}
    for p in paths:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        out[p] = h.hexdigest()
    return out


def _data_uri(path):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _post(url, payload):
    """One batched POST; raises EmbedUnavailable with an actionable message."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise EmbedUnavailable(
            f"embeddings endpoint returned HTTP {e.code}: {detail} — if the "
            f"input contained images, the endpoint does not accept them "
            f"(llama-server needs the mmproj for image input)") from e
    except (urllib.error.URLError, OSError) as e:
        raise EmbedUnavailable(
            f"cannot reach embeddings endpoint {url}: {e} — start it with: "
            f"llama-server -hf ggml-org/embeddinggemma-2-GGUF --embeddings"
        ) from e
    try:
        return json.loads(body)
    except ValueError as e:
        raise EmbedUnavailable(
            f"embeddings endpoint returned non-JSON: {body[:200]}") from e


def _load_cache(cache_dir):
    entries = {}
    path = os.path.join(cache_dir, CACHE_NAME)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    entries[os.path.abspath(e["file"])] = (e["vec"], e["mtime"])
                except (ValueError, KeyError, TypeError):
                    continue  # tolerate a torn/corrupt line
    return entries


def _save_cache(cache_dir, entries):
    path = os.path.join(cache_dir, CACHE_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        for f, (vec, mtime) in entries.items():
            fh.write(json.dumps({"file": f, "vec": vec, "mtime": mtime})
                     + "\n")


def embed_files(paths, cache_dir=None, batch=BATCH):
    """Embed image files via the local endpoint -> {abspath: vec}.

    cache_dir holds embeddings.jsonl (one JSON line per file:
    {"file","vec","mtime"}); a file is re-embedded only when its mtime
    changed. Byte-identical files are hashed first and embedded once —
    the duplicate gets the representative's vector without a server call.
    """
    paths = [os.path.abspath(p) for p in paths]
    if not paths:
        return {}
    if cache_dir is None:
        cache_dir = os.path.dirname(paths[0])
    cache = _load_cache(cache_dir)
    vecs, stale = {}, []
    for p in paths:
        ent = cache.get(p)
        if ent is not None and ent[1] == os.path.getmtime(p):
            vecs[p] = ent[0]
        else:
            stale.append(p)

    by_hash = {}
    for p, h in file_hashes(stale).items():
        by_hash.setdefault(h, []).append(p)
    reps = [ps[0] for ps in by_hash.values()]
    for i in range(0, len(reps), batch):
        chunk = reps[i:i + batch]
        payload = {"model": embed_model(),
                   "input": [{"type": "image_url",
                              "image_url": {"url": _data_uri(p)}}
                             for p in chunk]}
        data = _post(embed_url(), payload).get("data")
        if not isinstance(data, list) or len(data) != len(chunk):
            raise EmbedUnavailable(
                f"embeddings endpoint returned {len(data) if isinstance(data, list) else 'no'} "
                f"vectors for {len(chunk)} images")
        if all("index" in d for d in data):
            data = sorted(data, key=lambda d: d["index"])
        for entry, p in zip(data, chunk):
            vec = entry.get("embedding")
            if not vec:
                raise EmbedUnavailable(
                    f"embeddings endpoint returned no embedding for "
                    f"{os.path.basename(p)}")
            vecs[p] = vec
    for ps in by_hash.values():
        for p in ps[1:]:
            vecs[p] = vecs[ps[0]]

    for p in paths:
        cache[p] = (vecs[p], os.path.getmtime(p))
    _save_cache(cache_dir, cache)
    return vecs
