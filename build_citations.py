"""
Build reverse citation index for tplegacy.net glossary entries.

Scans all posts tagged 'glossary' via Ghost Content API,
extracts internal <a> links with context (moon-quote / mentioned / further-reading),
inverts the forward map to produce a reverse citation index,
saves as citations.json sorted alphabetically for stable diffs.
"""

import json
import os
import sys
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

GHOST_URL = "https://tplegacy.net"
SITE_HOST = "tplegacy.net"
API_KEY = os.environ.get("GHOST_CONTENT_API_KEY")

if not API_KEY:
    sys.exit("Missing GHOST_CONTENT_API_KEY environment variable")

CONTEXT_WEIGHT = {"verbatim": 3, "mentioned": 2, "further_reading": 1}


class LinkExtractor(HTMLParser):
    """Walk the HTML, capture <a> hrefs with their structural context."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._stack = []
        self._current_section = ""
        self._in_heading = False
        self._heading_buf = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag in ("h2", "h3"):
            self._in_heading = True
            self._heading_buf = []
        if tag == "a" and "href" in attrs_dict:
            self.links.append({
                "href": attrs_dict["href"],
                "context": self._detect_context(),
            })
        self._stack.append((tag, attrs_dict))

    def handle_endtag(self, tag):
        if tag in ("h2", "h3") and self._in_heading:
            self._current_section = "".join(self._heading_buf).strip().lower()
            self._in_heading = False
        if self._stack and self._stack[-1][0] == tag:
            self._stack.pop()

    def handle_data(self, data):
        if self._in_heading:
            self._heading_buf.append(data)

    def _detect_context(self):
        # Inside <blockquote class="moon-quote"> = verbatim citation
        for tag, attrs in reversed(self._stack):
            if tag == "blockquote":
                cls = attrs.get("class", "")
                if "moon-quote" in cls:
                    return "verbatim"
        # Inside "Further Reading" or "Key Texts" section
        if self._current_section:
            if "further reading" in self._current_section:
                return "further_reading"
            if "key texts" in self._current_section:
                return "further_reading"
        return "mentioned"


def fetch_all_glossary_posts():
    """Paginate through Ghost Content API for all glossary-tagged posts."""
    posts = []
    page = 1
    while True:
        r = requests.get(
            f"{GHOST_URL}/ghost/api/content/posts/",
            params={
                "key": API_KEY,
                "filter": "tag:glossary",
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


def normalize_link(href):
    """Return target slug for internal post links, or None."""
    if not href or href.startswith(("#", "mailto:", "tel:")):
        return None
    parsed = urlparse(href)
    if parsed.netloc and parsed.netloc != SITE_HOST:
        return None
    path = parsed.path.strip("/")
    if not path:
        return None
    # Skip system paths
    if path.startswith(("tag/", "author/", "content/", "assets/")):
        return None
    # Slug = first path segment
    return path.split("/")[0]


def build_index(posts):
    """Build the reverse citation index from a list of source posts."""
    forward_edges = []

    for post in posts:
        source_slug = post["slug"]
        source_title = post["title"]
        html = post.get("html") or ""

        extractor = LinkExtractor()
        extractor.feed(html)

        # Dedupe: same source→target keeps the strongest context
        seen = {}
        for link in extractor.links:
            target = normalize_link(link["href"])
            if not target or target == source_slug:
                continue
            ctx = link["context"]
            prev = seen.get(target)
            if prev is None or CONTEXT_WEIGHT[ctx] > CONTEXT_WEIGHT[prev]:
                seen[target] = ctx

        for target, ctx in seen.items():
            forward_edges.append({
                "source_slug": source_slug,
                "source_title": source_title,
                "target_slug": target,
                "context": ctx,
            })

    # Invert to reverse index
    reverse = defaultdict(lambda: {
        "verbatim": [],
        "mentioned": [],
        "further_reading": [],
    })
    for edge in forward_edges:
        reverse[edge["target_slug"]][edge["context"]].append({
            "slug": edge["source_slug"],
            "title": edge["source_title"],
        })

    # Sort each category alphabetically for stable diffs
    for target in reverse:
        for cat in reverse[target]:
            reverse[target][cat].sort(key=lambda x: x["title"].lower())

    return dict(reverse)


def main():
    print(f"Fetching glossary posts from {GHOST_URL} ...")
    posts = fetch_all_glossary_posts()
    print(f"  Found {len(posts)} posts")

    print("Building reverse citation index ...")
    index = build_index(posts)
    print(f"  Index covers {len(index)} target slugs")

    out_path = "citations.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"Wrote {out_path}")

    # Show top 10 most-cited targets so you can sanity-check
    print("\nTop 10 most-cited targets:")
    ranked = sorted(
        index.items(),
        key=lambda kv: sum(len(v) for v in kv[1].values()),
        reverse=True,
    )
    for slug, cats in ranked[:10]:
        total = sum(len(v) for v in cats.values())
        v = len(cats["verbatim"])
        m = len(cats["mentioned"])
        fr = len(cats["further_reading"])
        print(f"  {slug:50s}  {total:3d}  (v={v} m={m} fr={fr})")


if __name__ == "__main__":
    main()
