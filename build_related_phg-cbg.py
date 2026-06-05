"""
Build related-12 index for the Pyeong Hwa Gyeong + Chambumo Gyeong corpus
based on direct intra-segment links.

Segment:
  - phg-cbg → pyeong-hwa-gyeong + chambumo-gyeong (merged corpus)

For each post in the segment, computes top-12 related posts ranked by:
  1. Bidirectional links (both posts cite each other) — strongest signal
  2. Forward + Backward by edge presence, scored by combined connectivity

Outputs one JSON file:
  - related-phg-cbg.json

Each entry: { "post-slug": [ {slug, title, kind}, ... up to 12 ] }
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

TOP_N = 12


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
        return []
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
    For each post in the segment, return list of up to TOP_N related posts.

    Ranking:
      1. Bidirectional links (post A and B both reference each other)
      2. Forward links (this post references them)
      3. Backward links (they reference this post)

    Within each tier, sorted by "popularity" within segment (inbound count).
    This ensures that if a post has many forward links, the most-cited targets
    appear first.
    """
    by_slug = {p["slug"]: p for p in posts}
    slugs_in_segment = set(by_slug.keys())

    # Build forward edges: slug → set of other slugs in segment it links to
    forward = {}
    for p in posts:
        outbound = extract_outbound_slugs(p.get("html", ""))
        forward[p["slug"]] = {s for s in outbound if s in slugs_in_segment and s != p["slug"]}

    # Build backward edges: slug → set of slugs that link to it
    backward = defaultdict(set)
    for slug, targets in forward.items():
        for target in targets:
            backward[target].add(slug)

    # Compute inbound count per slug (used for tier-internal ranking)
    inbound_count = {slug: len(backward.get(slug, set())) for slug in slugs_in_segment}

    # For each post, build ranked related list
    result = {}
    for p in posts:
        slug = p["slug"]
        fwd = forward.get(slug, set())
        bwd = backward.get(slug, set())

        bi = fwd & bwd               # bidirectional
        out_only = fwd - bi          # only this cites them
        in_only = bwd - bi           # only they cite this

        # Sort each tier by inbound popularity (most-cited first)
        def sort_by_popularity(slug_set):
            return sorted(slug_set, key=lambda s: -inbound_count.get(s, 0))

        ranked = []
        for s in sort_by_popularity(bi):
            ranked.append({
                "slug": s,
                "title": by_slug[s]["title"],
                "kind": "bi",
            })
        for s in sort_by_popularity(out_only):
            ranked.append({
                "slug": s,
                "title": by_slug[s]["title"],
                "kind": "out",
            })
        for s in sort_by_popularity(in_only):
            ranked.append({
                "slug": s,
                "title": by_slug[s]["title"],
                "kind": "in",
            })

        if ranked:
            result[slug] = ranked[:TOP_N]

    return result


def main():
    for key, (tags, filename) in SEGMENTS.items():
        tag_list = ", ".join(f"'{t}'" for t in tags)
        print(f"\n[{key}] Fetching posts with tag(s): {tag_list}", flush=True)
        posts = fetch_posts_by_tags(tags)
        print(f"  Got {len(posts)} unique posts total", flush=True)

        print(f"[{key}] Building related-{TOP_N} index ...", flush=True)
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
        full_lists = sum(1 for s in sizes if s == TOP_N)
        print(f"  Total edges:        {sum(kinds.values())}")
        print(f"  Bidirectional:      {kinds['bi']}")
        print(f"  Forward-only:       {kinds['out']}")
        print(f"  Backward-only:      {kinds['in']}")
        print(f"  Avg list size:      {avg_size:.1f}")
        print(f"  Full lists ({TOP_N}):     {full_lists} of {len(result)}")

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"  Wrote {filename}", flush=True)

    print(f"\nDone — {len(SEGMENTS)} files generated.")


if __name__ == "__main__":
    main()
