"""
Reconnaissance script: fetch posts with the three target tags and map
internal links within each segment (intra-segment only).

Tags:
  - exposition-of-the-divine-principle
  - cheon-seong-gyeong
  - sermon

Output:
  - segments_recon.json (full data per segment)
  - Console summary: counts, top-10 hubs, top-10 destinations, isolated posts
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

SEGMENTS = {
    "dp":     "exposition-of-the-divine-principle",
    "csg":    "cheon-seong-gyeong",
    "sermon": "sermon",
}


def fetch_posts_by_tag(tag):
    """Paginate through Ghost Content API for posts tagged with the given tag."""
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
    return sorted(slugs)


def build_segment(key, tag):
    print(f"\n[{key}] Fetching posts with tag '{tag}' ...", flush=True)
    posts = fetch_posts_by_tag(tag)
    print(f"  Got {len(posts)} posts", flush=True)

    slugs_in_segment = {p["slug"] for p in posts}

    forward = {}
    for p in posts:
        outbound_all = extract_outbound_slugs(p.get("html", ""))
        outbound_in = [s for s in outbound_all if s in slugs_in_segment and s != p["slug"]]
        forward[p["slug"]] = {
            "title": p["title"],
            "outbound_in_segment": outbound_in,
            "outbound_all_count": len(outbound_all),
        }

    return {
        "tag": tag,
        "total_posts": len(posts),
        "posts": forward,
    }


def show_summary(key, segment):
    posts = segment["posts"]
    print(f"\n{'='*72}")
    print(f"[{key.upper()}]  tag: {segment['tag']}")
    print(f"{'='*72}")
    print(f"Total posts:                  {segment['total_posts']}")

    edges = sum(len(d["outbound_in_segment"]) for d in posts.values())
    avg = edges / max(1, len(posts))
    print(f"Intra-segment edges:          {edges}")
    print(f"Avg outbound per post:        {avg:.1f}")

    isolated = [s for s, d in posts.items() if not d["outbound_in_segment"]]
    print(f"Isolated posts (no outbound): {len(isolated)}")

    # Sample of titles
    print(f"\nFirst 5 posts (sample):")
    for slug, data in list(posts.items())[:5]:
        print(f"  - {data['title']}  ({slug})")

    # Top outbound hubs
    top_out = sorted(
        posts.items(),
        key=lambda kv: len(kv[1]["outbound_in_segment"]),
        reverse=True,
    )[:10]
    print(f"\nTop 10 by OUTBOUND (rich cross-referencers):")
    for slug, data in top_out:
        n = len(data["outbound_in_segment"])
        if n == 0:
            break
        print(f"  {slug[:55]:55s}  out={n:3d}")

    # Top inbound destinations
    inbound = defaultdict(int)
    for slug, data in posts.items():
        for target in data["outbound_in_segment"]:
            inbound[target] += 1
    top_in = sorted(inbound.items(), key=lambda kv: kv[1], reverse=True)[:10]
    print(f"\nTop 10 by INBOUND (most-cited within segment):")
    for slug, count in top_in:
        title = posts.get(slug, {}).get("title", "?")[:40]
        print(f"  {slug[:55]:55s}  in={count:3d}  ({title})")


def main():
    result = {}
    for key, tag in SEGMENTS.items():
        result[key] = build_segment(key, tag)

    with open("segments_recon.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
    print("\nWrote segments_recon.json")

    for key in SEGMENTS:
        show_summary(key, result[key])


if __name__ == "__main__":
    main()
