#!/usr/bin/env python3
"""
recall.py — multi-signal memory recall over a folder of markdown memory files.

This is the heart of the "memory" layer: given a query, score every memory by
blending five signals and return the most relevant. No single signal is enough;
each catches what the others miss.

    score = lexical            (IDF-weighted term hits; frontmatter weighted higher)
          + graph              (BFS over typed relations from top lexical seeds, hop-decayed)
          + semantic           (cosine vs. a query embedding — OPTIONAL, plug your provider)
          + pagerank           (graph-centrality prior — OPTIONAL, from graph.json)
          + decay/reinforce    (freshness via type half-life + Ebbinghaus importance + recent use)

Runnable with the standard library alone (lexical + graph + decay). Semantic and
pagerank are optional and pluggable — fill the two marked functions to enable them.

Usage:
    python recall.py "when does acme pay" --top 5 --dir ./memory
"""
import os, re, sys, json, math, argparse
from datetime import datetime
from collections import defaultdict, deque

# ── tunable weights (start here, tune to your data) ──────────────────────────
W = {
    "graph_new": 2.0,        # a memory pulled in ONLY by the graph
    "graph_existing": 1.0,   # a memory already found lexically, reinforced by graph
    "semantic": 3.0,         # cosine similarity contribution (if enabled)
    "semantic_top": 15,      # consider top-N semantic hits
    "pagerank": 20.0,        # graph-centrality prior (if enabled)
    "decay": 2.0,            # freshness * (1 + recent-use)
    "seed_limit": 5,         # how many lexical hits seed the graph walk
}
# decay half-life per type (days): a name barely decays, a token expires fast
TYPE_HALF_LIFE = {"user": 730, "feedback": 365, "procedure": 365, "knowledge": 180,
                  "project": 90, "reference": 90, "incident": 365, "pattern": 365}
REL_WEIGHT = {"related": 1.0, "cites": 1.0, "builds_on": 1.2, "contradicts": 1.2, "supersedes": 1.3}

# ── parse memory files ───────────────────────────────────────────────────────
def parse(path):
    text = open(path, encoding="utf-8").read()
    fm, body, front = {}, text, ""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        front, body = m.group(1), m.group(2)
        for line in front.splitlines():
            mm = re.match(r"^(\w+):\s*(.*)$", line)
            if mm and mm.group(1) != "relations":
                fm[mm.group(1)] = mm.group(2).strip()
    # typed edges: support `{type: X, target: Y}` in either field order
    rels = re.findall(r"type:\s*(\w+),\s*target:\s*([A-Za-z0-9_\-]+)", front)
    rels += [(ty, tgt) for tgt, ty in
             re.findall(r"target:\s*([A-Za-z0-9_\-]+),\s*type:\s*(\w+)", front)]
    # plus [[wikilinks]] in the body as untyped 'related' edges
    rels += [("related", w) for w in re.findall(r"\[\[([A-Za-z0-9_\-]+)\]\]", body)]
    fm["_relations"] = rels
    fm["name"] = fm.get("name") or os.path.splitext(os.path.basename(path))[0]
    return fm, body

def load(dirpath):
    out = {}
    for fn in os.listdir(dirpath):
        if fn.endswith(".md") and fn.upper() != "README.MD":
            fm, body = parse(os.path.join(dirpath, fn))
            out[fm["name"]] = {"fm": fm, "body": body, "path": os.path.join(dirpath, fn)}
    return out

# ── 1. lexical (IDF-weighted) ────────────────────────────────────────────────
def build_idf(mem):
    N = len(mem) or 1
    df = defaultdict(int)
    for m in mem.values():
        seen = set(re.findall(r"\w+", (m["fm"].get("description", "") + " " + m["body"]).lower()))
        for t in seen:
            df[t] += 1
    return {t: math.log((N + 1) / (c + 0.5)) for t, c in df.items()}, N

def lexical(query_terms, m, idf):
    fm_blob = " ".join(str(v) for k, v in m["fm"].items() if not k.startswith("_")).lower()
    body = m["body"].lower()
    score = 0.0
    for t in query_terms:
        t = t.lower(); w = idf.get(t, 1.0)
        if t in fm_blob:
            score += 3.0 * w                       # match in frontmatter = strong
        score += 1.5 * fm_blob.count(t) * w
        score += min(body.count(t) * 0.5, 5.0) * w # body matches, capped
    return score

# ── 2. graph (BFS over relations, hop-decayed) ───────────────────────────────
def graph_expand(seeds, mem, hops):
    weights = defaultdict(float)
    q = deque((s, 0) for s in seeds)
    seen = set(seeds)
    while q:
        name, hop = q.popleft()
        if hop >= hops or name not in mem:
            continue
        decay = 0.5 ** (hop + 1)
        for rel_type, target in mem[name]["fm"].get("_relations", []):
            w = decay * REL_WEIGHT.get(rel_type, 1.0)
            if w > weights[target]:
                weights[target] = w
            if target not in seen:
                seen.add(target); q.append((target, hop + 1))
    return weights

# ── 3. semantic (OPTIONAL — plug your embedding provider) ─────────────────────
def embed(text):
    """Return a vector for `text`, or None to disable semantic recall.
    Plug any provider here (OpenAI, Gemini, a local model). Keep the key in env."""
    return None  # e.g. call your embeddings API with os.environ["YOUR_EMBED_KEY"]

def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0

# ── 4. pagerank (OPTIONAL — precomputed centrality) ──────────────────────────
def load_pagerank(dirpath):
    """Return {name: score}. Compute once from the relation graph and cache to
    graph.json, or return {} to disable."""
    p = os.path.join(dirpath, "graph.json")
    if os.path.exists(p):
        return {n: meta.get("pagerank", 0.0) for n, meta in json.load(open(p)).get("nodes", {}).items()}
    return {}

# ── 5. decay / reinforcement (freshness + Ebbinghaus + recent use) ───────────
def decay_score(fm, access_hits):
    fresh = 1.0
    lv = fm.get("lastVerified")
    if lv:
        try:
            days = max(0, (datetime.now() - datetime.strptime(lv, "%Y-%m-%d")).days)
            hl = int(fm.get("half_life_days") or TYPE_HALF_LIFE.get((fm.get("type") or "").lower(), 90))
            imp = max(0.2, min(1.0, float(fm.get("importance", 3)) / 5.0))
            lam = 0.16 * (1.0 - imp * 0.8)                       # important → slower decay
            fresh = max(math.exp(-days / max(1, hl)), math.exp(-lam * days / 30.0))
        except (ValueError, TypeError):
            pass
    now = datetime.now().timestamp()
    recent = [t for t in (access_hits or []) if now - t < 7 * 86400]
    return fresh, math.log(1 + len(recent))

# ── fuse ─────────────────────────────────────────────────────────────────────
def recall(query, mem, dirpath, hops=2, top=10):
    terms = query.split()
    idf, _ = build_idf(mem)
    access = json.load(open(os.path.join(dirpath, "access_log.json"))) \
        if os.path.exists(os.path.join(dirpath, "access_log.json")) else {}

    combined = defaultdict(float)
    lex = {n: lexical(terms, m, idf) for n, m in mem.items()}
    lex = {n: s for n, s in lex.items() if s > 0}
    for n, s in lex.items():
        combined[n] += s

    seeds = sorted(lex, key=lex.get, reverse=True)[:W["seed_limit"]]
    for n, w in graph_expand(seeds, mem, hops).items():
        combined[n] += (W["graph_new"] if n not in lex else W["graph_existing"]) * w

    qv = embed(query)
    if qv:
        sims = sorted(((n, cosine(qv, embed(m["body"]) or [])) for n, m in mem.items()),
                      key=lambda x: -x[1])[:W["semantic_top"]]
        vals = [s for _, s in sims] or [0]
        lo, hi = min(vals), max(vals); span = (hi - lo) or 1.0
        for n, s in sims:
            if s >= 0.45:
                combined[n] += W["semantic"] * (s - lo) / span   # min-max normalized

    pr = load_pagerank(dirpath)
    for n in list(combined):
        combined[n] += W["pagerank"] * pr.get(n, 0.0)

    for n in list(combined):
        fresh, used = decay_score(mem[n]["fm"], access.get(n, {}).get("hits"))
        combined[n] += W["decay"] * fresh * (1.0 + used)

    return sorted(combined.items(), key=lambda x: -x[1])[:top]

# ── cli ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="+")
    ap.add_argument("--dir", default="./memory")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--hops", type=int, default=2)
    a = ap.parse_args()
    mem = load(a.dir)
    if not mem:
        print(f"no .md memories in {a.dir}"); sys.exit(1)
    for name, score in recall(" ".join(a.query), mem, a.dir, a.hops, a.top):
        desc = mem[name]["fm"].get("description", "")[:70]
        print(f"{score:7.2f}  {name}  — {desc}")
