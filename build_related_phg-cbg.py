"""
Build related index for the Pyeong Hwa Gyeong + Chambumo Gyeong corpus
based on direct intra-segment links.

Segment:
  - phg-cbg → pyeong-hwa-gyeong + chambumo-gyeong (merged corpus)

For each post in the segment, computes related posts grouped by link type:
  - bi  : bidirectional (both posts cite each other) — strongest signal
  - out : this post cites them
  - in  : they cite this post

Each type is capped independently at PER_KIND_LIMIT, so no single type crowds
out the others. Within a type, sorted by inbound popularity (most-cited first).

Outputs one JSON file:
  - related-phg-cbg.json

Each entry: { "post-slug": [ {slug, title, kind}, ... ] }
  kind: "bi" (bidirectional), "out" (this cites them), "in" (they cite this)
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

# Each segment: (list of tags to merge, output filename)
SEGMENTS = {
    "phg-cbg": (["pyeong-hwa-gyeong", "chambumo-gyeong"], "related-phg-cbg.json"),
}

PER_KIND_LIMIT = 7   # max items shown per type (bi / out / in), independently


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


def fetch_posts_by_tags(tags):
    """
    Fetch and merge posts across multiple tags, deduplicating by post id.
    Returns a flat list of unique posts.
    """
    seen_ids = set()
    merged = []
    for tag in tags:
        tag_posts = fetch_posts_by_tag(tag)
        new_count = 0
        for p in tag_posts:
            pid = p.get("id")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                merged.append(p)
                new_count += 1
        print(f"    tag '{tag}': {len(tag_posts)} posts ({new_count} new after dedup)", flush=True)
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


def build_related(posts):
    """
    For each post in the segment, return related posts grouped by link type,
    each type capped independently at PER_KIND_LIMIT.

    Ranking within each type: by inbound popularity within the segment
    (most-cited targets first).
    """
    by_slug = {p["slug"]: p for p in posts}
    slugs_in_segment = set(by_slug.keys())

    # Forward edges: slug → set of other slugs in segment it links to
    forward = {}
    for p in posts:
        outbound = extract_outbound_slugs(p.get("html", ""))
        forward[p["slug"]] = {s for s in outbound if s in slugs_in_segment and s != p["slug"]}

    # Backward edges: slug → set of slugs that link to it
    backward = defaultdict(set)
    for slug, targets in forward.items():
        for target in targets:
            backward[target].add(slug)

    # Inbound count per slug (used for tier-internal ranking)
    inbound_count = {slug: len(backward.get(slug, set())) for slug in slugs_in_segment}

    def sort_by_popularity(slug_set):
        return sorted(slug_set, key=lambda s: (-inbound_count.get(s, 0), by_slug[s]["title"].lower()))

    result = {}
    for p in posts:
        slug = p["slug"]
        fwd = forward.get(slug, set())
        bwd = backward.get(slug, set())

        bi = fwd & bwd               # bidirectional
        out_only = fwd - bi          # only this cites them
        in_only = bwd - bi           # only they cite this

        ranked = []
        for s in sort_by_popularity(bi)[:PER_KIND_LIMIT]:
            ranked.append({"slug": s, "title": by_slug[s]["title"], "kind": "bi"})
        for s in sort_by_popularity(out_only)[:PER_KIND_LIMIT]:
            ranked.append({"slug": s, "title": by_slug[s]["title"], "kind": "out"})
        for s in sort_by_popularity(in_only)[:PER_KIND_LIMIT]:
            ranked.append({"slug": s, "title": by_slug[s]["title"], "kind": "in"})

        if ranked:
            result[slug] = ranked

    return result


def main():
    for key, (tags, filename) in SEGMENTS.items():
        tag_list = ", ".join(f"'{t}'" for t in tags)
        print(f"\n[{key}] Fetching posts with tag(s): {tag_list}", flush=True)
        posts = fetch_posts_by_tags(tags)
        print(f"  Got {len(posts)} unique posts total", flush=True)

        print(f"[{key}] Building related index (cap {PER_KIND_LIMIT}/type) ...", flush=True)
        result = build_related(posts)
        print(f"  Built related lists for {len(result)} posts", flush=True)

        # Stats
        kinds = {"bi": 0, "out": 0, "in": 0}
        sizes = []
        for slug, related in result.items():
            sizes.append(len(related))
            for item in related:
                kinds[item["kind"]] += 1

        avg_size = sum(sizes) / max(1, len(sizes))
        biggest = max(sizes) if sizes else 0
        print(f"  Total edges:        {sum(kinds.values())}")
        print(f"  Bidirectional:      {kinds['bi']}")
        print(f"  Forward-only:       {kinds['out']}")
        print(f"  Backward-only:      {kinds['in']}")
        print(f"  Avg list size:      {avg_size:.1f}")
        print(f"  Largest list:       {biggest}")

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"  Wrote {filename}", flush=True)

    print(f"\nDone — {len(SEGMENTS)} files generated.")


if __name__ == "__main__":
    main()
