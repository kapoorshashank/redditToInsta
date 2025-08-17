#!/usr/bin/env python3
"""
Reddit → Instagram (Top 8, public JSON) + IG Graph (no captions)
Task-Scheduler safe: logs & history live next to the script
---------------------------------------------------------------------------
- Fetches the top 8 hot posts from r/ProgrammerHumor via Reddit's public JSON.
- Filters to direct i.redd.it images only (no NSFW, no stickies).
- Ranks by interaction score and selects the best candidate not posted before.
- Publishes to Instagram via Graph API with NO caption.
- Logs Meta usage headers and retries transient Graph errors.
- Writes a daily log file to logs/run_YYYY-MM-DD.log (next to this file).
- Stores posted history in posted_history.json (next to this file).
"""

import os
import sys
import time
import json
import logging
from typing import List, Dict
from datetime import datetime

import requests
import re
from html import unescape
from xml.etree import ElementTree as ET


# ── Base directory (folder where this .py lives) ──────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# ---- Reddit UA (more "real" to avoid CI blocks) ----
REDDIT_UA = os.getenv(
    "REDDIT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RedditToInstaBot/1.0 (+https://github.com/<youruser>/<yourrepo>)"
)

# Paths pinned to script folder (Task Scheduler-safe)
LOG_DIR = os.path.join(BASE_DIR, "logs")
HISTORY_FILE = os.path.join(BASE_DIR, "posted_history.json")
os.makedirs(LOG_DIR, exist_ok=True)

# ── Logging setup ─────────────────────────────────────────────────────────────
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

# ── optional dotenv (loads .env from the script folder) ───────────────────────
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))
    logging.info("Loaded .env from script folder.")
except Exception:
    logging.debug("python-dotenv not installed; skipping .env load")

# ── Config (edit via .env) ────────────────────────────────────────────────────
SUBREDDIT = os.getenv("SUBREDDIT", "ProgrammerHumor")
LIMIT = int(os.getenv("REDDIT_LIMIT", "8"))  # only 8 hot posts

IG_USER_ID = os.getenv("IG_USER_ID")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
GRAPH_BASE = "https://graph.facebook.com/v21.0"


def require_env(var: str):
    val = os.getenv(var)
    if not val:
        logging.error(f"Missing required environment variable: {var}")
        sys.exit(1)


for var in ["IG_USER_ID", "IG_ACCESS_TOKEN"]:
    require_env(var)

# ── Local history helpers ─────────────────────────────────────────────────────
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
        logging.error(f"Failed to save history file: {e}")


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

# ── Usage headers + backoff ───────────────────────────────────────────────────
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
            time.sleep(wait)
            continue
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
            time.sleep(wait)
            continue
        log_usage_headers(r, "GET ")
        raise RuntimeError(f"Graph error {r.status_code}: {r.text}")
    raise RuntimeError("Graph: max retries reached")
def _http_get(url: str, headers: dict, timeout: int = 20):
    r = requests.get(url, headers=headers, timeout=timeout)
    # Treat 403 specially so we can fall back
    if r.status_code == 403:
        raise requests.HTTPError("403", response=r)
    r.raise_for_status()
    return r

def fetch_hot_json_primary(sub: str, limit: int) -> dict:
    url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
    headers = {"User-Agent": REDDIT_UA, "Accept": "application/json"}
    return _http_get(url, headers).json()

def fetch_hot_json_old(sub: str, limit: int) -> dict:
    url = f"https://old.reddit.com/r/{sub}/hot.json?limit={limit}"
    headers = {"User-Agent": REDDIT_UA, "Accept": "application/json"}
    return _http_get(url, headers).json()

def fetch_hot_rss(sub: str, limit: int) -> list[dict]:
    """
    Last-resort: parse the RSS feed and extract i.redd.it images.
    We won't have scores; we’ll keep the original feed order.
    """
    url = f"https://www.reddit.com/r/{sub}/hot/.rss"
    headers = {"User-Agent": REDDIT_UA, "Accept": "application/rss+xml"}
    text = _http_get(url, headers).text
    root = ET.fromstring(text)
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        permalink = (item.findtext("link") or "").strip()
        html = item.findtext("content:encoded", default="", namespaces=ns) or ""
        html = unescape(html)
        m = re.search(r"https://i\.redd\.it/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png)", html, re.IGNORECASE)
        if not m:
            continue
        img = m.group(0)
        items.append({
            "title": title,
            "image_url": img,
            "permalink": permalink,
            "author": "u/unknown",
            "ups": 0, "num_comments": 0, "awards": 0, "upvote_ratio": 0.0,
            "score_interaction": 0  # no scoring data via RSS
        })
        if len(items) >= max(1, limit):
            break
    return items

def get_candidates_with_fallback(sub: str, limit: int) -> list[dict]:
    """
    Try JSON → old JSON → RSS; return ranked candidates if JSON works,
    else RSS-ordered items.
    """
    try:
        data = fetch_hot_json_primary(sub, limit)
        return image_candidates_with_interactions(data)
    except requests.HTTPError as e:
        if getattr(e, "response", None) and e.response is not None and e.response.status_code == 403:
            logging.warning("Reddit 403 on primary JSON; retrying old.reddit.com …")
            try:
                data = fetch_hot_json_old(sub, limit)
                return image_candidates_with_interactions(data)
            except requests.HTTPError as e2:
                if getattr(e2, "response", None) and e2.response is not None and e2.response.status_code == 403:
                    logging.warning("Reddit 403 on old JSON; falling back to RSS …")
                    return fetch_hot_rss(sub, limit)
                raise
        raise

# ── Reddit fetch ──────────────────────────────────────────────────────────────
def fetch_hot_json(sub: str, limit: int) -> dict:
    url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
    headers = {"User-Agent": "anonymous:reddit-to-instagram:0.8"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def image_candidates_with_interactions(data: dict) -> List[Dict]:
    items: List[Dict] = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {}) or {}
        url = p.get("url_overridden_by_dest") or p.get("url") or ""
        is_img = (
            isinstance(url, str)
            and url.startswith("https://i.redd.it/")
            and url.lower().endswith((".jpg", ".jpeg", ".png"))
        )
        if not is_img or p.get("over_18") or p.get("stickied"):
            continue

        ups = int(p.get("ups") or p.get("score") or 0)
        comments = int(p.get("num_comments") or 0)
        awards = int(p.get("total_awards_received") or 0)
        ratio = float(p.get("upvote_ratio") or 0.0)

        interaction_score = (
            ups
            + 2 * comments
            + 10 * awards
            + int(ups * max(0.0, (ratio - 0.85)) * 2)
        )

        items.append({
            "title": p.get("title", ""),
            "image_url": url,
            "permalink": f"https://reddit.com{p.get('permalink', '')}",
            "author": f"u/{p.get('author')}" if p.get("author") else "u/unknown",
            "ups": ups,
            "num_comments": comments,
            "awards": awards,
            "upvote_ratio": ratio,
            "score_interaction": interaction_score,
        })

    items.sort(key=lambda x: x["score_interaction"], reverse=True)
    return items

# ── Instagram API (no caption) ────────────────────────────────────────────────
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
    """Poll every 5s up to 10 times (fewer calls)."""
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
        time.sleep(5)
        waited += 5
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

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    logging.info(f"[Step 1] Fetching top {LIMIT} hot posts from r/{SUBREDDIT} …")
    candidates = get_candidates_with_fallback(SUBREDDIT, LIMIT)


    if not candidates:
        logging.error("No suitable image posts found.")
        sys.exit(2)

    history = load_history()
    pick = None
    for c in candidates:
        if not already_posted(c["image_url"], history):
            pick = c
            break

    if not pick:
        logging.info("All top candidates already posted. Nothing new to post today.")
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
