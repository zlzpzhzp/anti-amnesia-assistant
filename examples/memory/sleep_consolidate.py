#!/usr/bin/env python3
"""
sleep_consolidate.py — periodic "sleep" for the memory layer.

Memory that only ever appends becomes a junk drawer. A background job (run nightly)
does two brain-inspired passes and emits a *worklist of proposals* — it does NOT
mutate memory itself. Your agent reviews the proposals and applies the good ones,
so a bad auto-merge can never silently corrupt memory.

  REM  — discover MISSING LINKS: pairs of memories that are clearly about the same
         thing but aren't connected in the graph → propose an edge.
  NREM — PROMOTE episodes: a topic that keeps recurring in your day-to-day journal
         but has no durable knowledge file → propose creating one.

Runnable with the stdlib (uses a TF-IDF cosine as the similarity signal). Swap in
real embeddings for stronger REM — see the marked function.

Usage:
    python sleep_consolidate.py --dir ./memory --journal ./journal > proposals.json
"""
import os, re, sys, json, math, argparse
from collections import defaultdict, Counter

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
    rels = set(re.findall(r"target:\s*([A-Za-z0-9_\-]+)", front))
    rels |= set(re.findall(r"\[\[([A-Za-z0-9_\-]+)\]\]", body))
    fm["_links"] = rels
    fm["name"] = fm.get("name") or os.path.splitext(os.path.basename(path))[0]
    return fm, body

def tokens(s):
    return [w for w in re.findall(r"[a-z0-9가-힣]+", s.lower()) if len(w) > 1]

def tfidf_vectors(docs):
    N = len(docs) or 1
    df = defaultdict(int)
    tfs = {}
    for name, text in docs.items():
        tf = Counter(tokens(text)); tfs[name] = tf
        for t in tf: df[t] += 1
    idf = {t: math.log((N + 1) / (c + 0.5)) for t, c in df.items()}
    vecs = {}
    for name, tf in tfs.items():
        vecs[name] = {t: f * idf[t] for t, f in tf.items()}
    return vecs

def cosine(a, b):
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    na = math.sqrt(sum(v * v for v in a.values())); nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0

# OPTIONAL: replace tfidf with real embeddings for stronger REM.
def embed(text):
    return None  # plug your embeddings provider; return None to use TF-IDF cosine

# ── REM: missing-link discovery ──────────────────────────────────────────────
def rem(mem, sim_threshold=0.30, top=20):
    docs = {n: (m["fm"].get("description", "") + " " + m["body"]) for n, m in mem.items()}
    vecs = tfidf_vectors(docs)
    names = list(mem)
    out = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if b in mem[a]["fm"]["_links"] or a in mem[b]["fm"]["_links"]:
                continue  # already connected
            sim = cosine(vecs[a], vecs[b])
            if sim >= sim_threshold:
                out.append({"kind": "link", "a": a, "b": b, "similarity": round(sim, 3),
                            "why": "highly similar but not linked — consider relating"})
    out.sort(key=lambda x: -x["similarity"])
    return out[:top]

# ── NREM: episode → knowledge promotion ──────────────────────────────────────
def nrem(mem, journal_dir, min_recurrence=3, top=20):
    if not journal_dir or not os.path.isdir(journal_dir):
        return []
    counts = Counter()
    for fn in os.listdir(journal_dir):
        if fn.endswith(".md"):
            for t in set(tokens(open(os.path.join(journal_dir, fn), encoding="utf-8").read())):
                counts[t] += 1
    known = " ".join((m["fm"].get("description", "") + " " + n) for n, m in mem.items()).lower()
    out = []
    for term, c in counts.most_common(200):
        if c >= min_recurrence and len(term) > 3 and term not in known:
            out.append({"kind": "promote", "topic": term, "recurrence": c,
                        "why": f"appears in {c} journal entries but has no durable memory yet"})
    return out[:top]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="./memory")
    ap.add_argument("--journal", default="")
    ap.add_argument("--sim", type=float, default=0.30)
    a = ap.parse_args()
    mem = {}
    for fn in os.listdir(a.dir):
        if fn.endswith(".md") and fn.upper() != "README.MD":
            fm, body = parse(os.path.join(a.dir, fn)); mem[fm["name"]] = {"fm": fm, "body": body}
    proposals = {"rem_missing_links": rem(mem, a.sim), "nrem_promotions": nrem(mem, a.journal)}
    print(json.dumps(proposals, ensure_ascii=False, indent=2))
