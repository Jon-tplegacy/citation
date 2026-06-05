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

Output: glossary-backlinks.json
  { "term-slug": [ {slug, title, corpus}, ... up to TOP_N ] }
sorted alphabetically for stable diffs.
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
# source post carries several of these tags.
CORPUS_TAGS = [
    ("chambumo-gyeong",                                    "CBG"),
    ("pyeong-hwa-gyeong",                                  "PHG"),
    ("cheon-seong-gyeong",                                 "CSG"),
    ("exposition-of-the-divine-principle",                 "DP"),
    ("world-scripture-and-the-teachings-of-sun-myung-moon", "World Scripture"),
    ("messages-of-peace",                                  "Messages of Peace"),
    ("sermon",                                             "Sermon"),
]

TOP_N = 7   # sources shown per term


def fetch_posts_by_tag(tag, fields="id,slug,title,html,url"):
    """Paginate through Ghost Content API for posts with the given tag."""
    posts = []
    page = 1
    while True:
        r = requests.get(
            f"{GHOST_URL}/ghost/api/content/posts/",
            params={
                "key": API_KEY,
                "filter": f"tag:{tag}",
                "limit": "50",
                "page": str(page),
                "fields": fields,
            },
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


def fetch_corpus_sources():
    """
    Fetch the source corpus across CORPUS_TAGS, dedupe by id, annotate each
    post with `_corpus` = label of the first tag it appeared under.
    """
    seen_ids = set()
    merged = []
    for tag, label in CORPUS_TAGS:
        tag_posts = fetch_posts_by_tag(tag)
        new_count = 0
        for p in tag_posts:
            pid = p.get("id")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                p["_corpus"] = label
                merged.append(p)
                new_count += 1
        print(f"    tag '{tag}' [{label}]: {len(tag_posts)} posts ({new_count} new after dedup)", flush=True)
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
    For each glossary term, the corpus sources that link to it (top TOP_N),
    ranked by the source's own prominence (inbound citations within corpus).
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
        ranked = [
            {"slug": s, "title": by_slug[s]["title"], "corpus": by_slug[s]["_corpus"]}
            for s in rank(srcs)
        ]
        if ranked:
            result[term] = ranked[:TOP_N]

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

    print("Building glossary backlink index ...", flush=True)
    result, glossary_edges = build_index(glossary_titles, sources)
    print(f"  {len(result)} terms have at least one backlink", flush=True)

    ranked_terms = sorted(glossary_edges.items(), key=lambda kv: -len(kv[1]))
    total_edges = sum(len(v) for v in result.values())
    sizes = [len(v) for v in result.values()]
    avg = sum(sizes) / max(1, len(sizes))
    full = sum(1 for s in sizes if s == TOP_N)

    print("\n  Top 15 most-referenced terms (corpus sources citing them):", flush=True)
    for term, srcs in ranked_terms[:15]:
        print(f"    {len(srcs):4d}  {glossary_titles.get(term, term)[:48]}", flush=True)
    print(f"\n  Backlink edges shown:  {total_edges}", flush=True)
    print(f"  Avg list size:         {avg:.1f}", flush=True)
    print(f"  Full lists ({TOP_N}):        {full} of {len(result)}", flush=True)

    summary = [
        "## Glossary backlinks — build report",
        "",
        f"- Glossary terms: **{len(glossary_titles)}**",
        f"- Source corpus posts: **{len(sources)}**",
        f"- Terms with at least one backlink: **{len(result)}**",
        f"- Backlink edges shown: **{total_edges}** · avg list {avg:.1f} · full lists of {TOP_N}: **{full}**",
        "",
        "### Top 15 most-referenced terms",
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
