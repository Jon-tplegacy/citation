"""
Build glossary backlink index: for each glossary term, the primary-source
materials across the archive that reference it.

Targets: posts tagged 'glossary' (the term pages).
Sources: the primary-source corpus (sermons + scriptures), tagged via
CORPUS_TAGS. Each source's internal links into a glossary term become a
backlink for that term.

Glossary <-> glossary links are intentionally NOT included here — those are
handled by the existing in-glossary citation index (citations.json) — so this
block never duplicates it.

Each entry carries a `corpus` origin label (Sermon, DP, CSG, ...).
Ranking: by how widely the citing source is itself cited within the corpus
(prominent sources first), with title as a tie-break for stable diffs.

Capping is PER CORPUS (PER_CORPUS_LIMIT): each book/segment is limited
independently, so a term cited by many sermons can't crowd out the other
books. Per term the list can therefore hold up to PER_CORPUS_LIMIT × (number
of citing corpora) entries.

Output: glossary-backlinks.json
  { "term-slug": [ {slug, title, corpus}, ... ] }
sorted alphabetically for stable diffs; within a term, ordered by corpus then
by rank.
"""

import json
import os
import sys
from collections import defaultdict
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

GHOST_URL = "https://tplegacy.net"
SITE_HOST = "tplegacy.net"
API_KEY = os.environ.get("GHOST_CONTENT_API_KEY")

if not API_KEY:
    sys.exit("Missing GHOST_CONTENT_API_KEY environment variable")

OUTPUT_FILE = "glossary-backlinks.json"
GLOSSARY_TAG = "glossary"

# Source corpus: (tag slug, short display label). Order = precedence when a
# source post carries several of these tags, and the order corpora are emitted.
# Note: 'chambumo-gyeong' is deliberately excluded from the source corpus.
CORPUS_TAGS = [
    ("pyeong-hwa-gyeong",                                  "PHG"),
    ("cheon-seong-gyeong",                                 "CSG"),
    ("exposition-of-the-divine-principle",                 "DP"),
    ("world-scripture-and-the-teachings-of-sun-myung-moon", "World Scripture"),
    ("messages-of-peace",                                  "Messages of Peace"),
    ("sermon",                                             "Sermon"),
]
CORPUS_ORDER = [label for _, label in CORPUS_TAGS]

# Posts carrying any of these tags are dropped from the source corpus even if
# they also carry one of the CORPUS_TAGS above.
EXCLUDE_TAGS = {"chambumo-gyeong"}

PER_CORPUS_LIMIT = 7   # max sources shown per corpus, per term


def fetch_posts_by_tag(tag, fields="id,slug,title,html,url", include_tags=False):
    """Paginate through Ghost Content API for posts with the given tag."""
    posts = []
    page = 1
    while True:
        params = {
            "key": API_KEY,
            "filter": f"tag:{tag}",
            "limit": "50",
            "page": str(page),
            "fields": fields,
        }
        if include_tags:
            params["include"] = "tags"
        r = requests.get(
            f"{GHOST_URL}/ghost/api/content/posts/",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        posts.extend(data.get("posts", []))
        pagination = data.get("meta", {}).get("pagination", {})
        if page >= pagination.get("pages", 1):
            break
        page += 1
    return posts


def fetch_glossary_targets():
    """Map glossary term slug -> title."""
    targets = {}
    for p in fetch_posts_by_tag(GLOSSARY_TAG, fields="id,slug,title"):
        targets[p["slug"]] = p["title"]
    return targets


def is_excluded(post):
    """True if the post carries any tag listed in EXCLUDE_TAGS."""
    return any(t.get("slug") in EXCLUDE_TAGS for t in post.get("tags", []))


def fetch_corpus_sources():
    """
    Fetch the source corpus across CORPUS_TAGS, dedupe by id, drop posts
    carrying an excluded tag, and annotate each post with `_corpus` = label of
    the first tag it appeared under.
    """
    seen_ids = set()
    merged = []
    for tag, label in CORPUS_TAGS:
        tag_posts = fetch_posts_by_tag(tag, include_tags=True)
        new_count = 0
        skipped = 0
        for p in tag_posts:
            pid = p.get("id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            if is_excluded(p):
                skipped += 1
                continue
            p["_corpus"] = label
            merged.append(p)
            new_count += 1
        note = f" ({new_count} new after dedup"
        note += f", {skipped} excluded)" if skipped else ")"
        print(f"    tag '{tag}' [{label}]: {len(tag_posts)} posts{note}", flush=True)
    return merged


def extract_outbound_slugs(html):
    """Return unique internal post slugs linked from this HTML."""
    if not html:
        return set()
    soup = BeautifulSoup(html, "html.parser")
    slugs = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "mailto:", "tel:")):
            continue
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != SITE_HOST:
            continue
        path = parsed.path.strip("/")
        if not path or path.startswith(("tag/", "author/", "content/", "assets/")):
            continue
        slugs.add(path.split("/")[0])
    return slugs


def build_index(glossary_titles, sources):
    """
    For each glossary term, the corpus sources that link to it, capped PER
    CORPUS at PER_CORPUS_LIMIT and ranked within each corpus by the source's
    own prominence (inbound citations within corpus).
    Returns (result, glossary_edges) for diagnostics.
    """
    glossary_slugs = set(glossary_titles)
    by_slug = {p["slug"]: p for p in sources}
    source_slugs = set(by_slug)

    backward = defaultdict(set)             # corpus-internal backlinks (for ranking sources)
    glossary_edges = defaultdict(set)       # term slug -> set of source slugs citing it

    for p in sources:
        s = p["slug"]
        for t in extract_outbound_slugs(p.get("html", "")):
            if t == s:
                continue
            if t in source_slugs:
                backward[t].add(s)
            elif t in glossary_slugs:
                glossary_edges[t].add(s)

    inbound_count = {s: len(backward.get(s, set())) for s in source_slugs}

    def rank(src_set):
        return sorted(src_set, key=lambda s: (-inbound_count.get(s, 0), by_slug[s]["title"].lower()))

    result = {}
    for term, srcs in glossary_edges.items():
        # group ranked sources by corpus, capping each corpus independently
        per_corpus = defaultdict(list)
        for s in rank(srcs):
            label = by_slug[s]["_corpus"]
            if len(per_corpus[label]) < PER_CORPUS_LIMIT:
                per_corpus[label].append(s)

        # emit in corpus order, preserving within-corpus rank
        entry = []
        for label in CORPUS_ORDER:
            for s in per_corpus.get(label, []):
                entry.append({"slug": s, "title": by_slug[s]["title"], "corpus": label})

        if entry:
            result[term] = entry

    return result, glossary_edges


def write_step_summary(lines):
    """Append markdown to the GitHub Step Summary, if running in Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        print(f"  (could not write step summary: {e})", flush=True)


def main():
    print(f"Fetching glossary terms from {GHOST_URL} ...", flush=True)
    glossary_titles = fetch_glossary_targets()
    print(f"  {len(glossary_titles)} glossary terms", flush=True)

    print("Fetching primary-source corpus ...", flush=True)
    sources = fetch_corpus_sources()
    print(f"  {len(sources)} source posts", flush=True)

    print(f"Building glossary backlink index (cap {PER_CORPUS_LIMIT}/corpus) ...", flush=True)
    result, glossary_edges = build_index(glossary_titles, sources)
    print(f"  {len(result)} terms have at least one backlink", flush=True)

    ranked_terms = sorted(glossary_edges.items(), key=lambda kv: -len(kv[1]))
    total_edges = sum(len(v) for v in result.values())
    sizes = [len(v) for v in result.values()]
    avg = sum(sizes) / max(1, len(sizes))
    biggest = max(sizes) if sizes else 0

    print("\n  Top 15 most-referenced terms (corpus sources citing them, pre-cap):", flush=True)
    for term, srcs in ranked_terms[:15]:
        print(f"    {len(srcs):4d}  {glossary_titles.get(term, term)[:48]}", flush=True)
    print(f"\n  Backlink edges shown:  {total_edges}", flush=True)
    print(f"  Avg list size:         {avg:.1f}", flush=True)
    print(f"  Largest list shown:    {biggest}", flush=True)

    summary = [
        "## Glossary backlinks — build report",
        "",
        f"- Glossary terms: **{len(glossary_titles)}**",
        f"- Source corpus posts: **{len(sources)}**",
        f"- Terms with at least one backlink: **{len(result)}**",
        f"- Cap: **{PER_CORPUS_LIMIT}** per corpus, per term",
        f"- Backlink edges shown: **{total_edges}** · avg list {avg:.1f} · largest list **{biggest}**",
        "",
        "### Top 15 most-referenced terms (pre-cap)",
        "",
        "| Sources | Term |",
        "|---:|---|",
    ]
    for term, srcs in ranked_terms[:15]:
        title = glossary_titles.get(term, term).replace("|", "\\|")
        summary.append(f"| {len(srcs)} | {title} |")
    summary.append("")
    write_step_summary(summary)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"\nWrote {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
