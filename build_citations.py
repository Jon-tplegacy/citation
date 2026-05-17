"""
Build reverse citation index for tplegacy.net glossary entries.

Scans all posts tagged 'glossary' via Ghost Content API,
extracts internal <a> links with three contexts:
  - verbatim:        link in the attribution paragraph that follows
                     a <blockquote class="moon-quote">
  - further_reading: link in a section under H2/H3 named
                     "Further Reading" or "Key Texts"
  - mentioned:       everything else (default)

Inverts the forward map to produce a reverse citation index,
saves as citations.json sorted alphabetically for stable diffs.
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

CONTEXT_WEIGHT = {"verbatim": 3, "further_reading": 1, "mentioned": 2}
FURTHER_READING_HEADINGS = ("further reading", "key texts")


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
    if path.startswith(("tag/", "author/", "content/", "assets/")):
        return None
    return path.split("/")[0]


def is_after_moon_quote(p_tag):
    """Return True if the given <p> immediately follows a moon-quote blockquote."""
    prev = p_tag.find_previous_sibling()
    if prev is None or prev.name != "blockquote":
        return False
    classes = prev.get("class") or []
    return "moon-quote" in classes


def detect_context(a_tag, current_section):
    """Classify a single <a> tag into verbatim / further_reading / mentioned."""
    # 1. Verbatim: <a> sits inside a <p> that immediately follows moon-quote
    p_parent = a_tag.find_parent("p")
    if p_parent is not None and is_after_moon_quote(p_parent):
        return "verbatim"

    # 2. Further reading: under an H2/H3 with matching title
    section_lc = current_section.lower() if current_section else ""
    if any(marker in section_lc for marker in FURTHER_READING_HEADINGS):
        return "further_reading"

    # 3. Default
    return "mentioned"


def extract_links(html):
    """Walk the post HTML and yield (target_slug, context) tuples."""
    soup = BeautifulSoup(html, "html.parser")
    current_section = ""

    # Walk all elements in document order
    for elem in soup.descendants:
        if not hasattr(elem, "name") or elem.name is None:
            continue
        if elem.name in ("h2", "h3"):
            current_section = elem.get_text(" ", strip=True)
        elif elem.name == "a":
            href = elem.get("href")
            if not href:
                continue
            target = normalize_link(href)
            if not target:
                continue
            context = detect_context(elem, current_section)
            yield target, context


def build_index(posts):
    """Build the reverse citation index from a list of source posts."""
    forward_edges = []

    for post in posts:
        source_slug = post["slug"]
        source_title = post["title"]
        html = post.get("html") or ""

        # Dedupe: same source→target keeps the strongest context
        seen = {}
        for target, ctx in extract_links(html):
            if target == source_slug:
                continue
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

    # Summary by category
    total_v = sum(len(c["verbatim"]) for c in index.values())
    total_m = sum(len(c["mentioned"]) for c in index.values())
    total_fr = sum(len(c["further_reading"]) for c in index.values())
    print(f"  verbatim:        {total_v}")
    print(f"  mentioned:       {total_m}")
    print(f"  further_reading: {total_fr}")

    out_path = "citations.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"Wrote {out_path}")

    print("\nTop 10 most-cited targets:")
    ranked = sorted(
        index.items(),
        key=lambda kv: sum(len(v) for v in kv[1].values()),
        reverse=True,
    )
    for slug, cats in ranked[:10]:
        v = len(cats["verbatim"])
        m = len(cats["mentioned"])
        fr = len(cats["further_reading"])
        total = v + m + fr
        print(f"  {slug:50s}  total={total:3d}  v={v:2d}  m={m:2d}  fr={fr:2d}")


if __name__ == "__main__":
    main()
