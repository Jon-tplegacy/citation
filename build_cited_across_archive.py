"""
Build "Cited Across the Archive" — cross-corpus backlinks for each text,
grouped by book and capped PER CORPUS.

For each text, the materials from OTHER books/segments that cite it, capped at
PER_CORPUS_LIMIT per book so no single book crowds out the others (same logic
as the glossary backlinks block). Same-book citations are excluded — they
appear in the per-segment "Connected Texts" block — so there is no duplication.

Corpus (merged, deduped by post id):
  sermon, cheon-seong-gyeong, exposition-of-the-divine-principle,
  messages-of-peace, world-scripture-and-the-teachings-of-sun-myung-moon,
  pyeong-hwa-gyeong, chambumo-gyeong

Each entry carries its `corpus` label so the renderer can group by book.
Ranking within each book: by how widely the citing source is itself cited
within the corpus (prominent first), title as a tie-break for stable diffs.

Output: cited-across-archive.json
  { "post-slug": [ {slug, title, corpus}, ... ] }
ordered by corpus then rank; sorted alphabetically by key for stable diffs.
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

OUTPUT_FILE = "cited-across-archive.json"

# (tag slug, short display label). Order = precedence when a post carries
# several of these tags, and the order books are emitted within an entry.
CORPUS_TAGS = [
    ("chambumo-gyeong",                                    "CBG"),
    ("pyeong-hwa-gyeong",                                  "PHG"),
    ("cheon-seong-gyeong",                                 "CSG"),
    ("exposition-of-the-divine-principle",                 "DP"),
    ("world-scripture-and-the-teachings-of-sun-myung-moon", "World Scripture"),
    ("messages-of-peace",                                  "Messages of Peace"),
    ("sermon",                                             "Sermon"),
]
CORPUS_ORDER = [label for _, label in CORPUS_TAGS]

# Books that share one per-segment block count as the same scope (excluded as
# "same book"). PHG + CBG share the related-phg-cbg block; others are own scope.
SCOPE_GROUP = {
    "PHG": "phg-cbg",
    "CBG": "phg-cbg",
}

PER_CORPUS_LIMIT = 7   # max citing sources shown per book, per text
HUB_THRESHOLD = None   # None disables the hub guard; set an int to re-enable


def scope_of(corpus_label):
    return SCOPE_GROUP.get(corpus_label, corpus_label)


def fetch_posts_by_tag(tag):
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
                "fields": "id,slug,title,html,url",
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


def fetch_corpus():
    """Fetch all corpus tags, dedupe by id, annotate `_corpus` label."""
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


def build_index(posts):
    """
    For each text, the cross-corpus sources citing it, grouped by book and
    capped at PER_CORPUS_LIMIT per book. Returns (result, forward, hubs).
    """
    by_slug = {p["slug"]: p for p in posts}
    slugs_in_corpus = set(by_slug)

    forward = {}
    for p in posts:
        outbound = extract_outbound_slugs(p.get("html", ""))
        forward[p["slug"]] = {s for s in outbound if s in slugs_in_corpus and s != p["slug"]}

    if HUB_THRESHOLD is None:
        hubs = set()
    else:
        hubs = {slug for slug, t in forward.items() if len(t) > HUB_THRESHOLD}

    backward = defaultdict(set)
    for slug, targets in forward.items():
        if slug in hubs:
            continue
        for t in targets:
            backward[t].add(slug)

    inbound_count = {s: len(backward.get(s, set())) for s in slugs_in_corpus}

    def rank(slug_set):
        return sorted(slug_set, key=lambda s: (-inbound_count.get(s, 0), by_slug[s]["title"].lower()))

    result = {}
    for p in posts:
        slug = p["slug"]
        my_scope = scope_of(p["_corpus"])

        # everyone citing this text, cross-corpus only (drop same-book sources)
        srcs = [s for s in backward.get(slug, set())
                if scope_of(by_slug[s]["_corpus"]) != my_scope]

        # group by book, cap each book independently
        per_corpus = defaultdict(list)
        for s in rank(srcs):
            label = by_slug[s]["_corpus"]
            if len(per_corpus[label]) < PER_CORPUS_LIMIT:
                per_corpus[label].append(s)

        entry = []
        for label in CORPUS_ORDER:
            for s in per_corpus.get(label, []):
                entry.append({"slug": s, "title": by_slug[s]["title"], "corpus": label})

        if entry:
            result[slug] = entry

    return result, forward, hubs


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
    print(f"Fetching corpus from {GHOST_URL} ...", flush=True)
    posts = fetch_corpus()
    print(f"  {len(posts)} unique posts in corpus", flush=True)

    print(f"Building cross-corpus index (cap {PER_CORPUS_LIMIT}/book) ...", flush=True)
    result, forward, hubs = build_index(posts)
    print(f"  Built lists for {len(result)} posts (cross-corpus only)", flush=True)

    by_slug = {p["slug"]: p for p in posts}
    ranked_out = sorted(forward.items(), key=lambda kv: -len(kv[1]))

    total_edges = sum(len(v) for v in result.values())
    sizes = [len(v) for v in result.values()]
    avg = sum(sizes) / max(1, len(sizes))
    biggest = max(sizes) if sizes else 0
    guard_txt = "disabled" if HUB_THRESHOLD is None else f"threshold {HUB_THRESHOLD}; {len(hubs)} ignored"

    print(f"\n  Hub guard: {guard_txt}", flush=True)
    print("  Top 15 sources by outbound links (within corpus):", flush=True)
    for slug, targets in ranked_out[:15]:
        flag = "   <-- HUB (ignored)" if slug in hubs else ""
        print(f"    {len(targets):4d}  {by_slug[slug]['title'][:48]:50s}{flag}", flush=True)
    print(f"\n  Cross-corpus edges:    {total_edges}", flush=True)
    print(f"  Avg list size:         {avg:.1f}", flush=True)
    print(f"  Largest list shown:    {biggest}", flush=True)

    summary = [
        "## Cited Across the Archive — build report (cross-corpus, per book)",
        "",
        f"- Corpus posts: **{len(posts)}**",
        f"- Posts with at least one cross-corpus backlink: **{len(result)}**",
        f"- Cap: **{PER_CORPUS_LIMIT}** per book, per text",
        f"- Cross-corpus edges: **{total_edges}** · avg list {avg:.1f} · largest list **{biggest}**",
        "- Same-book citations are excluded (they appear in the per-segment blocks).",
        "",
        "### Top 15 sources by outbound links",
        "",
        "| Outbound | Title | Status |",
        "|---:|---|:--|",
    ]
    for slug, targets in ranked_out[:15]:
        status = "HUB — ignored" if slug in hubs else "kept"
        title = by_slug[slug]["title"].replace("|", "\\|")
        summary.append(f"| {len(targets)} | {title} | {status} |")
    summary.append("")
    write_step_summary(summary)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"\nWrote {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
