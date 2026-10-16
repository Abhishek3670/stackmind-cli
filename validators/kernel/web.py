"""Web research utilities providing live web search and webpage fetching.

Supports multiple search providers:
1. DuckDuckGo Lite (default, zero-configuration, no API key required)
2. Tavily API (when TAVILY_API_KEY is configured)
3. SerpAPI (when SERPAPI_API_KEY is configured)
4. Brave Search (when BRAVE_API_KEY is configured)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser
from typing import Any

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 12


class _HTMLToTextExtractor(HTMLParser):
    """Clean HTML parser that strips scripts/styles and extracts readable text and media."""

    def __init__(self, base_url: str = "") -> None:
        super().__init__()
        self.base_url = base_url
        self.result: list[str] = []
        self.images: list[dict[str, str]] = []
        self.skip_tags = {"script", "style", "noscript", "svg", "iframe"}
        self.current_skip = 0
        self.in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in self.skip_tags:
            self.current_skip += 1
        elif tag_lower == "title":
            self.in_title = True
        elif tag_lower in ("p", "div", "article", "section", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li", "br"):
            self.result.append("\n")
        elif tag_lower == "img":
            attrs_dict = {k.lower(): (v or "") for k, v in attrs if k}
            src = attrs_dict.get("src") or attrs_dict.get("data-src") or ""
            srcset = attrs_dict.get("srcset") or ""
            if not src and srcset:
                parts = srcset.split(",")
                if parts:
                    src = parts[0].strip().split()[0]
            alt = attrs_dict.get("alt", "").strip()
            title = attrs_dict.get("title", "").strip()
            if src and not src.startswith("data:image/svg"):
                full_src = urllib.parse.urljoin(self.base_url, src) if self.base_url else src
                lower_src = full_src.lower()
                if not any(k in lower_src for k in ("tracking", "pixel", "analytics", "beacon", "1x1")):
                    self.images.append({
                        "src": full_src,
                        "alt": alt or title or "Image",
                    })

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in self.skip_tags:
            self.current_skip = max(0, self.current_skip - 1)
        elif tag_lower == "title":
            self.in_title = False
        elif tag_lower in ("p", "div", "article", "section", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li"):
            self.result.append("\n")

    def handle_data(self, data: str) -> None:
        if self.current_skip > 0:
            return
        if self.in_title:
            self.title += data
        if data:
            self.result.append(data)


def search_duckduckgo_lite(query: str, limit: int = 5, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    """Search DuckDuckGo Lite (zero-dependency, no API key required)."""
    clean_query = query.strip()
    if not clean_query:
        return []

    data = urllib.parse.urlencode({"q": clean_query}).encode("utf-8")
    req = urllib.request.Request(
        "https://lite.duckduckgo.com/lite/",
        data=data,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    # lite.duckduckgo.com layout:
    # <a ... href="..." class='result-link'>Title</a>
    # <td ... class='result-snippet'>Snippet</td>
    link_pattern = re.compile(
        r'<a\s+[^>]*?(?:href=["\']([^"\']+)["\'][^>]*?class=["\']result-link["\']|class=["\']result-link["\'][^>]*?href=["\']([^"\']+)["\'])[^>]*?>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    snippet_pattern = re.compile(
        r'<td\s+[^>]*?class=["\']result-snippet["\'][^>]*?>(.*?)</td>',
        re.IGNORECASE | re.DOTALL,
    )

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    tag_cleaner = re.compile(r"<[^>]+>")
    results: list[dict[str, Any]] = []

    for i in range(min(limit, len(links))):
        match = links[i]
        url = match[0] or match[1]
        raw_title = match[2]

        # Unwrap DDG redirect link if present (/l/?kh=-1&uddg=...)
        if "duckduckgo.com/l/?" in url:
            try:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                if "uddg" in qs and qs["uddg"]:
                    url = qs["uddg"][0]
            except Exception:
                pass

        clean_title = unescape(tag_cleaner.sub("", raw_title).strip())
        raw_snip = snippets[i] if i < len(snippets) else ""
        clean_snip = unescape(tag_cleaner.sub("", raw_snip).strip())

        results.append({
            "title": clean_title,
            "url": url,
            "snippet": clean_snip,
        })

    return results


def search_tavily(query: str, api_key: str, limit: int = 5, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    """Search via Tavily Search API."""
    payload = json.dumps({
        "api_key": api_key,
        "query": query,
        "max_results": limit,
        "include_answer": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results: list[dict[str, Any]] = []
    for item in data.get("results", [])[:limit]:
        results.append({
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", "")),
        })
    return results


def search_serpapi(
    query: str,
    api_key: str,
    limit: int = 5,
    media_type: str = "all",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Search via SerpAPI (supports general and images engines)."""
    params_dict = {
        "q": query,
        "api_key": api_key,
        "num": limit,
    }
    if media_type in ("images", "image"):
        params_dict["engine"] = "google_images"
        params_dict["tbm"] = "isch"
    else:
        params_dict["engine"] = "google"

    params = urllib.parse.urlencode(params_dict)
    url = f"https://serpapi.com/search.json?{params}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results: list[dict[str, Any]] = []
    if media_type in ("images", "image"):
        for item in data.get("images_results", [])[:limit]:
            results.append({
                "title": str(item.get("title", "")),
                "url": str(item.get("original", item.get("thumbnail", ""))),
                "thumbnail_url": str(item.get("thumbnail", "")),
                "source_url": str(item.get("link", "")),
                "snippet": str(item.get("source", "")),
                "media_type": "image",
            })
    else:
        for item in data.get("organic_results", [])[:limit]:
            results.append({
                "title": str(item.get("title", "")),
                "url": str(item.get("link", "")),
                "snippet": str(item.get("snippet", "")),
            })
    return results


def search_brave(query: str, api_key: str, limit: int = 5, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    """Search via Brave Search API."""
    params = urllib.parse.urlencode({
        "q": query,
        "count": limit,
    })
    url = f"https://api.search.brave.com/res/v1/web/search?{params}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results: list[dict[str, Any]] = []
    web_res = data.get("web", {}).get("results", [])
    for item in web_res[:limit]:
        results.append({
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("description", "")),
        })
    return results


def search_duckduckgo_images(
    query: str,
    limit: int = 5,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Search DuckDuckGo Images for direct, live image assets (zero-configuration)."""
    clean_query = query.strip()
    if not clean_query:
        return []

    # 1. Acquire vqd token from main search endpoint
    params = urllib.parse.urlencode({"q": clean_query, "iax": "images", "ia": "images"})
    req = urllib.request.Request(
        f"https://duckduckgo.com/?{params}",
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    m = (
        re.search(r'vqd=([\"\']?)([\d\-]+)\1', html)
        or re.search(r'vqd:\s*([\"\']?)([\d\-]+)\1', html)
        or re.search(r'vqd=([\d\-]+)', html)
    )
    if not m:
        return []

    vqd = m.group(2) if len(m.groups()) >= 2 and m.group(2) else m.group(1)

    # 2. Query DuckDuckGo image JSON API
    i_params = urllib.parse.urlencode({
        "l": "us-en",
        "o": "json",
        "q": clean_query,
        "vqd": vqd,
        "f": ",,,",
        "p": "1",
    })
    i_req = urllib.request.Request(
        f"https://duckduckgo.com/i.js?{i_params}",
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://duckduckgo.com/",
        },
    )

    with urllib.request.urlopen(i_req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    results: list[dict[str, Any]] = []
    for item in data.get("results", [])[:limit]:
        img_url = item.get("image") or item.get("thumbnail") or ""
        if img_url:
            results.append({
                "title": unescape(str(item.get("title", "")).strip()),
                "url": img_url,
                "thumbnail_url": item.get("thumbnail", ""),
                "source_url": item.get("url", ""),
                "snippet": f"Dimensions: {item.get('width', 0)}x{item.get('height', 0)} | Source: {item.get('url', '')}",
                "media_type": "image",
            })
    return results


def search_wikimedia_images(
    query: str,
    limit: int = 5,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Search Wikimedia Commons for free, public domain or Creative Commons image assets."""
    clean_query = query.strip()
    if not clean_query:
        return []

    params = urllib.parse.urlencode({
        "action": "query",
        "generator": "search",
        "gsrsearch": f"{clean_query} filetype:bitmap",
        "gsrlimit": max(1, min(limit, 20)),
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "format": "json",
    })
    url = f"https://commons.wikimedia.org/w/api.php?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StackMind/1.0"},
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    pages = data.get("query", {}).get("pages", {})
    results: list[dict[str, Any]] = []
    for page in pages.values():
        title = page.get("title", "").replace("File:", "").strip()
        info_list = page.get("imageinfo", [])
        if info_list:
            info = info_list[0]
            img_url = info.get("url", "")
            if img_url:
                results.append({
                    "title": title,
                    "url": img_url,
                    "thumbnail_url": info.get("thumburl", img_url),
                    "source_url": info.get("descriptionurl", img_url),
                    "snippet": f"{title} ({info.get('width', 0)}x{info.get('height', 0)}, {info.get('mime', 'image')})",
                    "media_type": "image",
                })
        if len(results) >= limit:
            break
    return results


def perform_web_search(
    query: str,
    limit: int = 5,
    media_type: str = "all",
) -> list[dict[str, Any]]:
    """Execute live web search across configured or default search providers."""
    clean_query = str(query or "").strip()
    if not clean_query:
        return []

    effective_limit = max(1, min(limit or 5, 20))
    norm_media = str(media_type or "all").lower().strip()

    # Image Search Mode
    if norm_media in ("images", "image"):
        # 1. SerpAPI if configured
        serpapi_key = os.environ.get("SERPAPI_API_KEY", "").strip()
        if serpapi_key:
            try:
                results = search_serpapi(clean_query, serpapi_key, limit=effective_limit, media_type="images")
                if results:
                    return results
            except Exception:
                pass

        # 2. DuckDuckGo Images
        try:
            results = search_duckduckgo_images(clean_query, limit=effective_limit)
            if results:
                return results
        except Exception:
            pass

        # 3. Wikimedia Commons fallback
        try:
            results = search_wikimedia_images(clean_query, limit=effective_limit)
            if results:
                return results
        except Exception:
            pass

        return [{
            "title": "Image Search Unavailable",
            "url": "",
            "snippet": f"No image assets found for '{clean_query}'. Check network connectivity or query terms.",
            "media_type": "image",
        }]

    # General Web Search Mode
    # 1. Check for configured search API keys
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        try:
            return search_tavily(clean_query, tavily_key, limit=effective_limit)
        except Exception:
            pass

    serpapi_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if serpapi_key:
        try:
            return search_serpapi(clean_query, serpapi_key, limit=effective_limit, media_type="all")
        except Exception:
            pass

    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if brave_key:
        try:
            return search_brave(clean_query, brave_key, limit=effective_limit)
        except Exception:
            pass

    # 2. Default: DuckDuckGo Lite (no API key required)
    try:
        results = search_duckduckgo_lite(clean_query, limit=effective_limit)
        if results:
            return results
    except Exception as exc:
        return [{
            "title": "Search Unavailable",
            "url": "",
            "snippet": f"Web search failed: {type(exc).__name__}: {exc}. Check network connectivity.",
        }]

    return []


def perform_image_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search for real, accessible web image assets and photos with direct image URLs."""
    return perform_web_search(query, limit=limit, media_type="images")


def perform_web_fetch(url: str, max_chars: int = 12_000, extract_images: bool = True) -> str:
    """Fetch and extract readable text/documentation and discovered media from a web URL."""
    clean_url = str(url or "").strip()
    if not clean_url:
        return "Error: URL is required"

    if not clean_url.startswith(("http://", "https://")):
        clean_url = f"https://{clean_url}"

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    req = urllib.request.Request(clean_url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as resp:
            content_type = str(resp.headers.get_content_type() or "text/html").lower()
            raw_bytes = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            text = raw_bytes.decode(charset, errors="replace")

        if "html" not in content_type:
            return text[:max_chars]

        extractor = _HTMLToTextExtractor(base_url=clean_url)
        extractor.feed(text)
        raw_content = "".join(extractor.result)

        # Clean multiple blank lines and extra whitespace
        lines = [line.strip() for line in raw_content.splitlines()]
        clean_lines: list[str] = []
        prev_empty = False
        for line in lines:
            if not line:
                if not prev_empty:
                    clean_lines.append("")
                    prev_empty = True
            else:
                clean_lines.append(line)
                prev_empty = False

        title_header = f"# {extractor.title.strip()}\nSource: {clean_url}\n\n" if extractor.title else f"Source: {clean_url}\n\n"
        body = "\n".join(clean_lines).strip()
        full_result = title_header + body

        if extract_images and extractor.images:
            seen_src: set[str] = set()
            unique_imgs = []
            for img in extractor.images:
                if img["src"] not in seen_src:
                    seen_src.add(img["src"])
                    unique_imgs.append(img)
            if unique_imgs:
                media_lines = ["\n\n## Discovered Images & Media Assets:"]
                for img in unique_imgs[:15]:
                    media_lines.append(f"- ![{img['alt']}]({img['src']})")
                full_result += "\n".join(media_lines)

        return full_result[:max_chars]

    except urllib.error.HTTPError as exc:
        return f"Error: HTTP {exc.code} {exc.reason} when fetching {clean_url}"
    except urllib.error.URLError as exc:
        return f"Error: Network error ({exc.reason}) when connecting to {clean_url}"
    except Exception as exc:
        return f"Error: Failed to fetch {clean_url}: {type(exc).__name__}: {exc}"
