"""
Build "Cited Across the Archive" — a corpus-wide, CROSS-CORPUS backlink index:
for each text, the materials from OTHER books/segments that cite it.

Corpus (merged, deduped by post id):
  sermon, cheon-seong-gyeong, exposition-of-the-divine-principle,
  messages-of-peace, world-scripture-and-the-teachings-of-sun-myung-moon,
  pyeong-hwa-gyeong, chambumo-gyeong

Backlinks only:
  kind: "bi" (mutual: they cite each other), "in" (they cite this, one-way)

Cross-corpus filter: a citing material is dropped from a text's list when it
belongs to the SAME scope group as that text. Same-book/same-segment links are
already shown by the per-segment "Connected Texts" blocks, so excluding them
here removes the duplication and keeps this block strictly cross-corpus.
PHG and CBG share one scope (they share a per-segment block); every other
corpus is its own scope.

Each entry carries a short `corpus` label (which book the citing material comes
from) for display as an origin tag — now always a DIFFERENT book.

Hub guard: optional, off by default (HUB_THRESHOLD = None). The archive has no
catalog pages; dense cross-citation between long sermons is real content.

Output: cited-across-archive.json
  { "post-slug": [ {slug, title, kind, corpus}, ... up to TOP_N ] }
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

OUTPUT_FILE = "cited-across-archive.json"

# (tag slug, short display label). Order = precedence: when a post carries
# several of these tags, the first match in this list wins as its corpus label.
CORPUS_TAGS = [
    ("chambumo-gyeong",                                    "CBG"),
    ("pyeong-hwa-gyeong",                                  "PHG"),
    ("cheon-seong-gyeong",                                 "CSG"),
    ("exposition-of-the-divine-principle",                 "DP"),
    ("world-scripture-and-the-teachings-of-sun-myung-moon", "World Scripture"),
    ("messages-of-peace",                                  "Messages of Peace"),
    ("sermon",                                             "Sermon"),
]

# Corpus labels that share one per-segment block are grouped into the same
# "scope" — a citing material in the same scope as the target is excluded
# (it already appears in that target's per-segment block). Labels not listed
# here are their own scope. Edit this to change what counts as "same book".
SCOPE_GROUP = {
    "PHG": "phg-cbg",
    "CBG": "phg-cbg",
}

TOP_N = 7              # backlinks shown per material
HUB_THRESHOLD = None   # None disables the hub guard; set an int (e.g. 200) to re-enable


def scope_of(corpus_label):
    """Map a corpus label to its scope group (defaults to itself)."""
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
    """
    Fetch all tags in CORPUS_TAGS order, dedupe by post id, and annotate each
    post with `_corpus` = the label of the first tag it appeared under.
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


def build_index(posts):
    """
    Build the cross-corpus backlink index.

    Ranking within each tier: by inbound popularity (how widely each citing
    material is itself cited), with title as a tie-break for stable diffs.
    Same-scope sources are filtered out per target (cross-corpus only).
    Returns (result, forward, hubs).
    """
    by_slug = {p["slug"]: p for p in posts}
    slugs_in_corpus = set(by_slug)

    # Forward edges: slug -> set of other corpus slugs it links to
    forward = {}
    for p in posts:
        outbound = extract_outbound_slugs(p.get("html", ""))
        forward[p["slug"]] = {s for s in outbound if s in slugs_in_corpus and s != p["slug"]}

    # Hub guard (optional)
    if HUB_THRESHOLD is None:
        hubs = set()
    else:
        hubs = {slug for slug, targets in forward.items() if len(targets) > HUB_THRESHOLD}

    # Backward edges (excluding hub sources): slug -> set of slugs citing it
    backward = defaultdict(set)
    for slug, targets in forward.items():
        if slug in hubs:
            continue
        for target in targets:
            backward[target].add(slug)

    inbound_count = {slug: len(backward.get(slug, set())) for slug in slugs_in_corpus}

    def sort_by_popularity(slug_set):
        return sorted(slug_set, key=lambda s: (-inbound_count.get(s, 0), by_slug[s]["title"].lower()))

    result = {}
    for p in posts:
        slug = p["slug"]
        my_scope = scope_of(p["_corpus"])
        fwd = forward.get(slug, set())
        bwd = backward.get(slug, set())

        bi = fwd & bwd
        in_only = bwd - bi

        def cross_corpus(slug_set):
            # keep only sources from a DIFFERENT scope than the current text
            return [s for s in sort_by_popularity(slug_set)
                    if scope_of(by_slug[s]["_corpus"]) != my_scope]

        ranked = []
        for s in cross_corpus(bi):
            ranked.append({"slug": s, "title": by_slug[s]["title"], "kind": "bi", "corpus": by_slug[s]["_corpus"]})
        for s in cross_corpus(in_only):
            ranked.append({"slug": s, "title": by_slug[s]["title"], "kind": "in", "corpus": by_slug[s]["_corpus"]})

        if ranked:
            result[slug] = ranked[:TOP_N]

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

    print("Building cross-corpus backlink index ...", flush=True)
    result, forward, hubs = build_index(posts)
    print(f"  Built lists for {len(result)} posts (cross-corpus only)", flush=True)

    by_slug = {p["slug"]: p for p in posts}
    ranked_out = sorted(forward.items(), key=lambda kv: -len(kv[1]))

    kinds = {"bi": 0, "in": 0}
    sizes = []
    for slug, related in result.items():
        sizes.append(len(related))
        for item in related:
            kinds[item["kind"]] += 1
    avg = sum(sizes) / max(1, len(sizes))
    full = sum(1 for s in sizes if s == TOP_N)

    guard_txt = "disabled" if HUB_THRESHOLD is None \
        else f"threshold {HUB_THRESHOLD}; {len(hubs)} ignored"

    print(f"\n  Hub guard: {guard_txt}", flush=True)
    print("  Top 15 sources by outbound links (within corpus):", flush=True)
    for slug, targets in ranked_out[:15]:
        flag = "   <-- HUB (ignored)" if slug in hubs else ""
        print(f"    {len(targets):4d}  {by_slug[slug]['title'][:48]:50s}{flag}", flush=True)
    print(f"\n  Cross-corpus edges:    {sum(kinds.values())}", flush=True)
    print(f"  Mutual (bi):           {kinds['bi']}", flush=True)
    print(f"  One-way cited (in):    {kinds['in']}", flush=True)
    print(f"  Avg list size:         {avg:.1f}", flush=True)
    print(f"  Full lists ({TOP_N}):        {full} of {len(result)}", flush=True)

    summary = [
        "## Cited Across the Archive — build report (cross-corpus)",
        "",
        f"- Corpus posts: **{len(posts)}**",
        f"- Posts with at least one cross-corpus backlink: **{len(result)}**",
        f"- Hub guard: **{guard_txt}**",
        f"- Cross-corpus edges: **{sum(kinds.values())}** (mutual {kinds['bi']}, one-way {kinds['in']})",
        f"- Avg list size: **{avg:.1f}** · full lists of {TOP_N}: **{full}**",
        "- Same-book citations are excluded here (they appear in the per-segment blocks).",
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
