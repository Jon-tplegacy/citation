"""
Build "Cited Across the Archive" — a single corpus-wide backlink index of
which materials cite each text, across seven merged tags.

Corpus (merged, deduped by post id):
  sermon, cheon-seong-gyeong, exposition-of-the-divine-principle,
  messages-of-peace, world-scripture-and-the-teachings-of-sun-myung-moon,
  pyeong-hwa-gyeong, chambumo-gyeong

For each post, outputs the top-N materials that link TO it — backlinks only:
  kind: "bi" (mutual: they cite each other), "in" (they cite this, one-way)
Forward-only links ("this cites them") are intentionally excluded; this block
answers "who across the archive cites this text".

Hub guard: any source post with more than HUB_THRESHOLD outbound links within
the corpus is treated as a navigation / index page and ignored as a backlink
for everyone — so catalog pages can't flood every list.

Each entry carries a short `corpus` label (which book/segment the citing
material comes from) for display as an origin tag.

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
# Reorder if a different label should win for posts that share tags.
CORPUS_TAGS = [
    ("chambumo-gyeong",                                    "CBG"),
    ("pyeong-hwa-gyeong",                                  "PHG"),
    ("cheon-seong-gyeong",                                 "CSG"),
    ("exposition-of-the-divine-principle",                 "DP"),
    ("world-scripture-and-the-teachings-of-sun-myung-moon", "World Scripture"),
    ("messages-of-peace",                                  "Messages of Peace"),
    ("sermon",                                             "Sermon"),
]

TOP_N = 7            # backlinks shown per material
HUB_THRESHOLD = 55   # sources with MORE outbound links than this are ignored


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
    Build the corpus-wide backlink index.

    Ranking within each tier: by inbound popularity (how widely each citing
    material is itself cited), with title as a tie-break for stable diffs.
    Returns (result, forward, hubs) so main() can print diagnostics.
    """
    by_slug = {p["slug"]: p for p in posts}
    slugs_in_corpus = set(by_slug)

    # Forward edges: slug -> set of other corpus slugs it links to
    forward = {}
    for p in posts:
        outbound = extract_outbound_slugs(p.get("html", ""))
        forward[p["slug"]] = {s for s in outbound if s in slugs_in_corpus and s != p["slug"]}

    # Hub guard: sources linking to more than HUB_THRESHOLD targets are
    # navigation/index pages, not real citations — drop them as sources.
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
        fwd = forward.get(slug, set())
        bwd = backward.get(slug, set())   # already hub-free

        bi = fwd & bwd          # mutual: they cite each other
        in_only = bwd - bi      # one-way: they cite this, this does not cite them

        ranked = []
        for s in sort_by_popularity(bi):
            ranked.append({
                "slug": s,
                "title": by_slug[s]["title"],
                "kind": "bi",
                "corpus": by_slug[s]["_corpus"],
            })
        for s in sort_by_popularity(in_only):
            ranked.append({
                "slug": s,
                "title": by_slug[s]["title"],
                "kind": "in",
                "corpus": by_slug[s]["_corpus"],
            })

        if ranked:
            result[slug] = ranked[:TOP_N]

    return result, forward, hubs


def main():
    print(f"Fetching corpus from {GHOST_URL} ...", flush=True)
    posts = fetch_corpus()
    print(f"  {len(posts)} unique posts in corpus", flush=True)

    print("Building Cited-Across-the-Archive index ...", flush=True)
    result, forward, hubs = build_index(posts)
    print(f"  Built backlink lists for {len(result)} posts", flush=True)

    by_slug = {p["slug"]: p for p in posts}

    # Diagnostics — use this to confirm HUB_THRESHOLD fits the real archive.
    ranked_out = sorted(forward.items(), key=lambda kv: -len(kv[1]))
    print(f"\n  Hub threshold = {HUB_THRESHOLD}; {len(hubs)} source(s) flagged as hubs and ignored")
    print("  Top 15 sources by outbound links (within corpus):")
    for slug, targets in ranked_out[:15]:
        flag = "   <-- HUB (ignored)" if slug in hubs else ""
        title = by_slug[slug]["title"][:48]
        print(f"    {len(targets):4d}  {title:50s}{flag}")

    # Stats
    kinds = {"bi": 0, "in": 0}
    sizes = []
    for slug, related in result.items():
        sizes.append(len(related))
        for item in related:
            kinds[item["kind"]] += 1
    avg = sum(sizes) / max(1, len(sizes))
    full = sum(1 for s in sizes if s == TOP_N)
    print(f"\n  Backlink edges shown:  {sum(kinds.values())}")
    print(f"  Mutual (bi):           {kinds['bi']}")
    print(f"  One-way cited (in):    {kinds['in']}")
    print(f"  Avg list size:         {avg:.1f}")
    print(f"  Full lists ({TOP_N}):        {full} of {len(result)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"\nWrote {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
