#!/usr/bin/env python3
"""
Reddit → Instagram (Top 8, public) + IG Graph (no captions), CI-safe
- JSON → old JSON → RSS → mirror (jina) fallbacks
- Accepts i.redd.it, preview.redd.it, i.imgur.com
- Normalizes preview.redd.it → i.redd.it where possible
- Logs + history next to the script
"""

import os
import sys
import time
import json
import logging
import re
from html import unescape
from xml.etree import ElementTree as ET
from typing import List, Dict, Any, Optional
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
HISTORY_FILE = os.path.join(BASE_DIR, "posted_history.json")
os.makedirs(LOG_DIR, exist_ok=True)

# A more "real" UA helps in CI
REDDIT_UA = os.getenv(
    "REDDIT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RedditToInstaBot/1.1 (+https://github.com/youruser/yourrepo)"
)

SUBREDDIT = os.getenv("SUBREDDIT", "ProgrammerHumor")
LIMIT = int(os.getenv("REDDIT_LIMIT", "8"))

IG_USER_ID = os.getenv("IG_USER_ID")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
GRAPH_BASE = "https://graph.facebook.com/v21.0"

# Accept these image hosts
ALLOWED_IMG_HOSTS = ("i.redd.it", "preview.redd.it", "i.imgur.com", "i.stack.imgur.com", "i.reddituploads.com")


log_filename = os.path.join(LOG_DIR, f"run_{datetime.now().strftime('%Y-%m-%d')}.log")
logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(_console)

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))
    logging.info("Loaded .env from script folder.")
except Exception:
    pass

def require_env(var: str):
    if not os.getenv(var):
        logging.error(f"Missing required environment variable: {var}")
        sys.exit(1)

for var in ["IG_USER_ID", "IG_ACCESS_TOKEN"]:
    require_env(var)

# ---------------- History ----------------
def load_history() -> List[dict]:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Could not read history file; starting fresh: {e}")
        return []

def save_history(history: List[dict]):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Failed to save history: {e}")

def already_posted(image_url: str, history: List[dict]) -> bool:
    return any(entry.get("image_url") == image_url for entry in history)

def add_to_history(image_url: str, reddit_permalink: str, media_id: str):
    history = load_history()
    history.append({
        "image_url": image_url,
        "reddit_url": reddit_permalink,
        "media_id": media_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })
    save_history(history)

# ------------- Graph helpers --------------
def log_usage_headers(resp: requests.Response, label: str):
    for h in ("x-app-usage", "x-page-usage", "x-business-use-case-usage"):
        v = resp.headers.get(h)
        if v:
            logging.info(f"[MetaUsage] {label} {h}: {v}")

def post_with_backoff(url: str, data: dict, timeout: int = 60, max_retries: int = 5) -> requests.Response:
    for i in range(max_retries):
        r = requests.post(url, data=data, timeout=timeout)
        if r.status_code in (200, 201):
            log_usage_headers(r, "POST")
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            wait = min(2 ** i, 30)
            logging.warning(f"[Graph] {r.status_code} – retrying in {wait}s … {r.text[:240]}")
            time.sleep(wait); continue
        log_usage_headers(r, "POST")
        raise RuntimeError(f"Graph error {r.status_code}: {r.text}")
    raise RuntimeError("Graph: max retries reached")

def get_with_backoff(url: str, params: dict, timeout: int = 30, max_retries: int = 5) -> requests.Response:
    for i in range(max_retries):
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            log_usage_headers(r, "GET ")
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            wait = min(2 ** i, 30)
            logging.warning(f"[Graph] {r.status_code} – retrying in {wait}s … {r.text[:240]}")
            time.sleep(wait); continue
        log_usage_headers(r, "GET ")
        raise RuntimeError(f"Graph error {r.status_code}: {r.text}")
    raise RuntimeError("Graph: max retries reached")

def ig_create_container(image_url: str) -> str:
    endpoint = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    params = {"image_url": image_url, "access_token": IG_ACCESS_TOKEN}
    resp = post_with_backoff(endpoint, params, timeout=60, max_retries=5)
    data = resp.json()
    creation_id = data.get("id")
    if not creation_id:
        raise RuntimeError(f"IG: missing creation id in response: {data}")
    return creation_id

def ig_check_status(creation_id: str, max_wait: int = 50) -> str:
    endpoint = f"{GRAPH_BASE}/{creation_id}"
    params = {"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN}
    waited = 0
    while waited < max_wait:
        resp = get_with_backoff(endpoint, params, timeout=30, max_retries=3)
        data = resp.json()
        status_code = data.get("status_code") or data.get("status")
        if status_code in ("FINISHED", "PUBLISHED"):
            return status_code
        if status_code in ("ERROR", "FAILED"):
            raise RuntimeError(f"IG container processing failed: {data}")
        time.sleep(5); waited += 5
    return "TIMEOUT"

def ig_publish(creation_id: str) -> str:
    endpoint = f"{GRAPH_BASE}/{IG_USER_ID}/media_publish"
    params = {"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN}
    resp = post_with_backoff(endpoint, params, timeout=60, max_retries=5)
    data = resp.json()
    media_id = data.get("id")
    if not media_id:
        raise RuntimeError(f"IG: missing media id in response: {data}")
    return media_id

# ------------- Reddit helpers -------------
class ForbiddenError(Exception): pass

def _req(url: str, accept: str, timeout: int = 20) -> requests.Response:
    r = requests.get(
        url,
        headers={
            "User-Agent": REDDIT_UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
        timeout=timeout,
    )
    if r.status_code == 403:
        raise ForbiddenError(f"403 from {url}")
    r.raise_for_status()
    return r

def _is_allowed_image(url: str) -> bool:
    try:
        host = url.split("/")[2].lower()
    except Exception:
        return False
    if not any(host.endswith(h) for h in ALLOWED_IMG_HOSTS):
        return False
    return bool(re.search(r"\.(jpg|jpeg|png)(\?|$)", url, re.IGNORECASE))

def _normalize_preview(url: str) -> str:
    # Convert preview.redd.it → i.redd.it when possible
    try:
        host = url.split("/")[2].lower()
        if host.startswith("preview.redd.it"):
            core = url.split("?")[0]
            return core.replace("//preview.", "//i.")
    except Exception:
        pass
    return url

def fetch_hot_json(sub: str, limit: int) -> Dict[str, Any]:
    url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
    return _req(url, "application/json").json()

def fetch_hot_json_old(sub: str, limit: int) -> Dict[str, Any]:
    url = f"https://old.reddit.com/r/{sub}/hot.json?limit={limit}"
    return _req(url, "application/json").json()

def fetch_hot_rss(sub: str, limit: int) -> List[Dict[str, Any]]:
    """
    Try multiple RSS feeds (hot/new/top?=day) and accept protocol-relative URLs.
    """
    feeds = [
        f"https://www.reddit.com/r/{sub}/hot/.rss",
        f"https://www.reddit.com/r/{sub}/new/.rss",
        f"https://www.reddit.com/r/{sub}/top/.rss?t=day",
    ]
    items: List[Dict[str, Any]] = []
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}

    for url in feeds:
        try:
            text = _req(url, "application/rss+xml").text
            root = ET.fromstring(text)
            for item in root.findall("./channel/item"):
                title = (item.findtext("title") or "").strip()
                permalink = (item.findtext("link") or "").strip()
                html = unescape(item.findtext("content:encoded", default="", namespaces=ns) or "")
                img = _find_first_image_in_html(html)
                if not img:
                    continue
                items.append({
                    "title": title,
                    "image_url": img,
                    "permalink": permalink,
                    "author": "u/unknown",
                    "ups": 0, "num_comments": 0, "awards": 0, "upvote_ratio": 0.0,
                    "score_interaction": 0
                })
                if len(items) >= max(1, limit):
                    return items
        except Exception as e:
            logging.warning(f"RSS fetch failed for {url}: {e}")
    return items


def fetch_hot_from_mirror(sub: str, limit: int) -> List[Dict[str, Any]]:
    """
    Last resort: use r.jina.ai to read old Reddit pages (hot/new/top?=day) and extract images.
    """
    pages = [
        f"https://r.jina.ai/http://old.reddit.com/r/{sub}/hot/",
        f"https://r.jina.ai/http://old.reddit.com/r/{sub}/new/",
        f"https://r.jina.ai/http://old.reddit.com/r/{sub}/top/?t=day",
    ]
    items: List[Dict[str, Any]] = []
    seen = set()

    for url in pages:
        try:
            text = _req(url, "text/plain").text
            for m in re.finditer(r'(?:https?:)?//[a-z0-9.-]+/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png)(?:\?[^\s"<>)]*)?',
                                 text, re.IGNORECASE):
                img = m.group(0)
                if img.startswith("//"):
                    img = "https:" + img
                if not _is_allowed_image(img):
                    continue
                img = _normalize_preview(img)
                if img in seen:
                    continue
                seen.add(img)
                items.append({
                    "title": "(mirror)",
                    "image_url": img,
                    "permalink": f"https://www.reddit.com/r/{sub}/",
                    "author": "u/unknown",
                    "ups": 0, "num_comments": 0, "awards": 0, "upvote_ratio": 0.0,
                    "score_interaction": 0
                })
                if len(items) >= max(1, limit):
                    return items
        except Exception as e:
            logging.warning(f"Mirror fetch failed for {url}: {e}")
    return items


def image_candidates_with_interactions(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {}) or {}
        url = p.get("url_overridden_by_dest") or p.get("url") or ""
        if not isinstance(url, str) or not url:
            continue
        if not any(url.split("/")[2].lower().endswith(h) for h in ALLOWED_IMG_HOSTS):
            continue
        if not re.search(r"\.(jpg|jpeg|png)(\?|$)", url, re.IGNORECASE):
            continue
        if p.get("over_18") or p.get("stickied"):
            continue

        url = _normalize_preview(url)

        ups = int(p.get("ups") or p.get("score") or 0)
        comments = int(p.get("num_comments") or 0)
        awards = int(p.get("total_awards_received") or 0)
        ratio = float(p.get("upvote_ratio") or 0.0)

        score = ups + 2 * comments + 10 * awards + int(ups * max(0.0, (ratio - 0.85)) * 2)

        items.append({
            "title": p.get("title", ""),
            "image_url": url,
            "permalink": f"https://reddit.com{p.get('permalink', '')}",
            "author": f"u/{p.get('author')}" if p.get("author") else "u/unknown",
            "ups": ups,
            "num_comments": comments,
            "awards": awards,
            "upvote_ratio": ratio,
            "score_interaction": score,
        })

    items.sort(key=lambda x: x["score_interaction"], reverse=True)
    return items

def _find_first_image_in_html(html: str) -> Optional[str]:
    """
    Find first allowed image URL in an HTML snippet.
    Handles protocol-relative URLs like //preview.redd.it/...
    Returns normalized https URL or None.
    """
    if not html:
        return None
    # match https://... OR //...
    for m in re.finditer(r'(?:https?:)?//[a-z0-9.-]+/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png)(?:\?[^\s"<>)]*)?',
                         html, re.IGNORECASE):
        url = m.group(0)
        if url.startswith("//"):
            url = "https:" + url
        if _is_allowed_image(url):
            return _normalize_preview(url)
    return None


def get_candidates_with_fallback(sub: str, limit: int) -> List[Dict[str, Any]]:
    # JSON
    try:
        data = fetch_hot_json(sub, limit)
        items = image_candidates_with_interactions(data)
        if items: return items
    except ForbiddenError:
        logging.warning("Reddit 403 on primary JSON; retrying old.reddit.com …")
    except Exception as e:
        logging.warning(f"Primary JSON failed: {e}")

    # old JSON
    try:
        data = fetch_hot_json_old(sub, limit)
        items = image_candidates_with_interactions(data)
        if items: return items
    except ForbiddenError:
        logging.warning("Reddit 403 on old JSON; falling back to RSS …")
    except Exception as e:
        logging.warning(f"Old JSON failed: {e}")

    # RSS
    try:
        items = fetch_hot_rss(sub, limit)
        if items: return items
        logging.warning("RSS returned no allowed images; trying mirror …")
    except Exception as e:
        logging.warning(f"RSS failed: {e}; trying mirror …")

    # Mirror
    items = fetch_hot_from_mirror(sub, limit)
    if items: return items

    raise RuntimeError("Could not collect any usable image links from Reddit (JSON/RSS/mirror).")

# ---------------- Main --------------------
def main():
    logging.info(f"[Step 1] Fetching top {LIMIT} hot posts from r/{SUBREDDIT} …")
    candidates = get_candidates_with_fallback(SUBREDDIT, LIMIT)
    if not candidates:
        logging.error("No suitable image posts found.")
        sys.exit(2)

    history = load_history()
    pick = next((c for c in candidates if not already_posted(c["image_url"], history)), None)
    if not pick:
        logging.info("All top candidates already posted. Nothing new today.")
        sys.exit(0)

    logging.info(
        f"[Pick] '{pick['title']}'  (ups={pick['ups']}, comments={pick['num_comments']}, "
        f"awards={pick['awards']}, ratio={pick['upvote_ratio']:.2f})"
    )
    logging.info(f"[Pick] URL: {pick['image_url']}  | Permalink: {pick['permalink']}")

    logging.info("[Step 2] Creating Instagram container …")
    creation_id = ig_create_container(pick["image_url"])
    logging.info(f"[Container] id={creation_id}")

    logging.info("[Step 3] Waiting for IG to process …")
    status = ig_check_status(creation_id, max_wait=50)
    logging.info(f"[Container] status={status}")
    if status not in ("FINISHED", "PUBLISHED"):
        logging.warning(f"Container status = {status}; attempting publish anyway.")

    logging.info("[Step 4] Publishing to Instagram …")
    media_id = ig_publish(creation_id)
    logging.info(f"[Done] Published media id: {media_id}")

    add_to_history(pick["image_url"], pick["permalink"], media_id)
    logging.info("History updated.")

if __name__ == "__main__":
    try:
        main()
        logging.info("✅ Script finished successfully.")
    except Exception:
        logging.exception("❌ Script failed with error")
        sys.exit(1)
