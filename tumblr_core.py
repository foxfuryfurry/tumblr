# -*- coding: utf-8 -*-
"""
Core of the Tumblr archiver (used by tumblr_gui.py).

Layout on disk:
    <out_dir>/<blog>/<YYYY-MM-DD_HHMMSS_postid>/0.jpg, 1.mov, ..., post.json
post.json is written last: its presence means the post is complete.
"""

import io
import os
import re
import json
import shutil
import html as html_lib
from datetime import datetime, timezone
from queue import Queue
from threading import Thread, Lock, Event
from urllib.parse import quote, unquote, urlparse

import requests
import xmltodict


TIMEOUT = 10      # seconds
RETRY = 5         # download attempts per file
MEDIA_NUM = 50    # posts per page (old endpoint)
THREADS = 10      # posts downloaded concurrently


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def clean_blog_name(raw):
    """Accepts 'name', 'name.tumblr.com', 'www.tumblr.com/name', with or without https://"""
    s = re.sub(r'^https?://', '', raw.strip().lstrip('@'), flags=re.IGNORECASE)
    host, _, path = s.partition('/')
    host = host.lower()
    parts = [p for p in path.split('/') if p]
    if host in ('tumblr.com', 'www.tumblr.com'):
        if parts[:2] == ['blog', 'view'] and len(parts) > 2:
            return parts[2]
        if parts[:1] == ['blog'] and len(parts) > 1:
            return parts[1]
        return parts[0] if parts else ''
    if host.endswith('.tumblr.com'):
        return host[:-len('.tumblr.com')]
    return host


def load_proxies(path):
    """Returns the proxies dict from proxies.json, or None. Raises ValueError if invalid."""
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        data = json.load(f)
    return data or None


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value):
    if isinstance(value, dict):
        value = value.get("#text", "")
    return value or ""


def _strip_html(text):
    text = re.sub(r'<br\s*/?>|</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    return html_lib.unescape(text).strip()


def _iso(ts):
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def folder_name(ts, post_id):
    """2026-09-02_092541_<post id>  (UTC; sorts chronologically, never collides)"""
    try:
        stamp = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    except (TypeError, ValueError):
        stamp = "unknown-time"
    return "%s_%s" % (stamp, post_id)


def _host(site):
    return site if "." in site else site + ".tumblr.com"


def parse_post_url(raw_url):
    """Return the blog identifier and post ID from a Tumblr post URL."""
    value = raw_url.strip()
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [unquote(part) for part in parsed.path.split("/") if part]

    if host in ("tumblr.com", "www.tumblr.com"):
        if parts[:2] == ["blog", "view"] and len(parts) >= 4:
            site, post_id = parts[2], parts[3]
        elif len(parts) >= 2:
            site, post_id = parts[0], parts[1]
        else:
            site, post_id = "", ""
    elif host.endswith(".tumblr.com"):
        site = host[:-len(".tumblr.com")]
        post_id = parts[1] if len(parts) >= 2 and parts[0] == "post" else ""
    elif len(parts) >= 2 and parts[0] == "post":
        site, post_id = host, parts[1]
    else:
        site, post_id = "", ""

    if (not site or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", site)
            or ".." in site):
        raise ValueError("The URL does not contain a supported Tumblr blog name.")
    if not re.fullmatch(r"\d+", post_id):
        raise ValueError("The URL does not contain a numeric Tumblr post ID.")
    return site, post_id


def _dedupe(items):
    seen = set()
    out = []
    for kind, url in items:
        if url not in seen:
            seen.add(url)
            out.append((kind, url))
    return out


def file_extension(url, kind):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if re.match(r"^\.[a-z0-9]{2,5}$", ext):
        return ext
    return ".jpg" if kind == "photo" else ".mp4"


# Per-blog record, owned by the caller (the GUI keeps it in its config file):
#   last_downloaded : UTC time the last (completed) run for this blog finished
#   newest_post_ts  : timestamp of the newest post saved; "only new posts" starts here
def _ts(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def describe_state(state):
    downloaded = (state.get("last_downloaded") or "")[:10] or "never"
    ts = state.get("newest_post_ts")
    newest = (_iso(ts) or "")[:10] if isinstance(ts, int) else ""
    return "Last downloaded: %s  \u00b7  Newest post: %s" % (downloaded, newest or "\u2014")


def _has_post_folders(folder):
    try:
        return any(e.is_dir() and not e.name.startswith("_") for e in os.scandir(folder))
    except OSError:
        return False


# ----------------------------------------------------------------------------
# Media extraction: old /api/read endpoint (XML)
# ----------------------------------------------------------------------------

NPF_RE = re.compile(r"data-npf='(\{.*?\})'", re.DOTALL)
IMG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
SRCSET_RE = re.compile(r'srcset="([^"]+)"')
SRC_RE = re.compile(r'\ssrc="([^"]+)"')
SOURCE_RE = re.compile(r'<source\s+src="([^"]+)"', re.IGNORECASE)
HD_RE = re.compile(r'"hdUrl":"([^"]+)"')


def _best_from_srcset(srcset):
    best = None
    for entry in srcset.split(","):
        m = re.search(r"(https?://\S+)\s+(\d+)w", entry.strip())
        if m:
            width = int(m.group(2))
            if best is None or width > best[0]:
                best = (width, m.group(1))
    return best[1] if best else None


def media_from_html(body):
    """Videos (from NPF data) and images embedded in a regular-body, in page order."""
    found = []
    video_found = False

    for m in NPF_RE.finditer(body):
        try:
            npf = json.loads(m.group(1))
        except ValueError:
            continue
        if npf.get("type") != "video":
            continue
        media = npf.get("media")
        if isinstance(media, list):
            media = max(media, key=lambda x: x.get("width", 0)) if media else {}
        # the "media" url is the full-size rendition; the top-level url is smaller
        url = (media or {}).get("url") or npf.get("url")
        if url and "tumblr.com" in url:
            found.append((m.start(), "video", url))
            video_found = True

    if not video_found:
        for m in SOURCE_RE.finditer(body):
            if "tumblr.com" in m.group(1):
                found.append((m.start(), "video", m.group(1)))

    for m in IMG_RE.finditer(body):
        tag = m.group(0)
        url = None
        srcset = SRCSET_RE.search(tag)
        if srcset:
            url = _best_from_srcset(srcset.group(1))
        if not url:
            src = SRC_RE.search(tag)
            url = src.group(1) if src else None
        if url and ".media.tumblr.com" in url:
            found.append((m.start(), "photo", url))

    found.sort(key=lambda t: t[0])
    return [(kind, url) for _, kind, url in found]


def _best_photo_url(photo):
    urls = _as_list(photo.get("photo-url"))
    if not urls:
        return None

    def width(u):
        try:
            return int(u.get("@max-width", 0)) if isinstance(u, dict) else 0
        except ValueError:
            return 0

    return _text(max(urls, key=width)) or None


def _legacy_video(post):
    for player in _as_list(post.get("video-player")):
        text = _text(player)
        m = SOURCE_RE.search(text)
        if m:
            return m.group(1)
        m = HD_RE.search(text)
        if m:
            return m.group(1).replace("\\", "")
    return None


def extract_media_old(post):
    items = []

    photoset = post.get("photoset")
    if isinstance(photoset, dict):
        for photo in _as_list(photoset.get("photo")):
            url = _best_photo_url(photo)
            if url:
                items.append(("photo", url))
    else:
        url = _best_photo_url(post)
        if url:
            items.append(("photo", url))

    body = _text(post.get("regular-body"))
    if body:
        items.extend(media_from_html(body))

    url = _legacy_video(post)
    if url:
        items.append(("video", url))

    return _dedupe(items)


def build_metadata(post, site):
    body = (_text(post.get("regular-body"))
            or _text(post.get("photo-caption"))
            or _text(post.get("video-caption")))
    tumblelog = post.get("tumblelog") or {}
    ts = post.get("@unix-timestamp")
    return {
        "artist": tumblelog.get("@name") or site,
        "title": _strip_html(_text(post.get("regular-title"))),
        "description": _strip_html(body),
        "date": _iso(ts) or post.get("@date-gmt"),
        "tags": [_text(t) for t in _as_list(post.get("tag"))],
        "id": post.get("@id"),
        "url": post.get("@url"),
    }


# ----------------------------------------------------------------------------
# Media extraction: v2 API (JSON, NPF)
# ----------------------------------------------------------------------------

def extract_media_v2(post):
    blocks = list(post.get("content", []))
    for t in post.get("trail", []):
        blocks += t.get("content", [])

    items = []
    for b in blocks:
        kind = b.get("type")
        if kind == "image":
            media = sorted(b.get("media", []),
                           key=lambda m: m.get("width", 0), reverse=True)
            if media:
                items.append(("photo", media[0]["url"]))
        elif kind == "video":
            media = b.get("media")
            if isinstance(media, list):
                media = media[0] if media else {}
            url = (media or {}).get("url")
            if url and "tumblr.com" in url:
                items.append(("video", url))
    return _dedupe(items)


def build_metadata_v2(post, site):
    texts = [b.get("text", "") for b in post.get("content", [])
             if b.get("type") == "text"]
    return {
        "artist": post.get("blog_name") or (post.get("blog") or {}).get("name") or site,
        "title": "",
        "description": "\n".join(texts).strip(),
        "date": _iso(post.get("timestamp")),
        "tags": post.get("tags", []),
        "id": post.get("id_string") or str(post.get("id", "")),
        "url": post.get("post_url"),
    }


# ----------------------------------------------------------------------------
# The crawler
# ----------------------------------------------------------------------------

class SiteStats(object):
    KEYS = ("queued", "done", "skipped", "failed", "stopped")

    def __init__(self):
        self.lock = Lock()
        self.counts = dict((k, 0) for k in self.KEYS)

    def add(self, key):
        with self.lock:
            self.counts[key] += 1

    def summary(self):
        with self.lock:
            c = dict(self.counts)
        text = "%d queued \u00b7 %d new \u00b7 %d existing \u00b7 %d failed" % (
            c["queued"], c["done"], c["skipped"], c["failed"])
        if c["stopped"]:
            text += " \u00b7 %d cancelled" % c["stopped"]
        return text


class Crawler(object):
    """
    blogs:   list of (blog_name, skip_reblogs, only_new) tuples
    done:    callable(blog_name), called after a blog finishes and its dates are saved
    get_state:  callable(blog_name) -> dict with the stored dates (or {})
    save_state: callable(blog_name, dict) to persist them after a completed run
    creds:   dict with consumer_key, consumer_secret, token, token_secret (all optional)
    log:     callable(str)
    status:  callable(blog_name, str)
    Both callbacks may be called from worker threads.
    """

    def __init__(self, blogs, out_dir, creds=None, proxies=None, log=print, status=None,
                 done=None, get_state=None, save_state=None):
        self.blogs = blogs
        self.out_dir = out_dir
        self.proxies = proxies
        self.log = log
        self.status = status or (lambda site, text: None)
        self.done = done or (lambda site: None)
        self.get_state = get_state or (lambda site: {})
        self.save_state = save_state or (lambda site, state: None)
        self.newest = {}     # blog -> newest post timestamp queued this run
        self.stop_event = Event()
        self.queue = Queue()
        self.stats = {}

        creds = creds or {}
        self.consumer_key = (creds.get("consumer_key") or "").strip()
        consumer_secret = (creds.get("consumer_secret") or "").strip()
        token = (creds.get("token") or "").strip()
        token_secret = (creds.get("token_secret") or "").strip()

        self.oauth = None
        if self.consumer_key and consumer_secret and token and token_secret:
            try:
                from requests_oauthlib import OAuth1
                self.oauth = OAuth1(self.consumer_key, consumer_secret, token, token_secret)
            except ImportError:
                self.log("requests_oauthlib is not installed (pip install requests_oauthlib); "
                         "logged-in API access is disabled.")

    def stop(self):
        self.stop_event.set()

    def run(self):
        workers = [Thread(target=self._worker, daemon=True) for _ in range(THREADS)]
        for w in workers:
            w.start()
        try:
            for entry in self.blogs:
                if self.stop_event.is_set():
                    break
                site, skip_reblogs = entry[0], entry[1]
                only_new = entry[2] if len(entry) > 2 else False
                self._do_site(site, skip_reblogs, only_new)
        finally:
            for _ in workers:
                self.queue.put(None)
            for w in workers:
                w.join()
        self.log("Stopped." if self.stop_event.is_set() else "All done.")

    def download_single_post(self, raw_url):
        try:
            site, post_id = parse_post_url(raw_url)
        except (AttributeError, ValueError) as e:
            self.log("Invalid Tumblr post URL: %s" % e)
            return

        self.stats[site] = SiteStats()
        self.newest[site] = None
        self.status(site, "downloading post...")
        workers = [Thread(target=self._worker, daemon=True) for _ in range(THREADS)]
        for worker in workers:
            worker.start()
        try:
            post = self._single_post_old(site, post_id)
            queued = bool(post and self._queue_post(
                site, os.path.join(self.out_dir, site), post.get("@id"),
                post.get("@unix-timestamp"), build_metadata(post, site),
                extract_media_old(post), replace=True))

            if not queued and (self.oauth or self.consumer_key):
                post = self._single_post_v2(site, post_id)
                if post:
                    post_key = post.get("id_string") or str(post.get("id", post_id))
                    queued = self._queue_post(
                        site, os.path.join(self.out_dir, site), post_key,
                        post.get("timestamp"), build_metadata_v2(post, site),
                        extract_media_v2(post), replace=True)

            if not queued:
                self.log("[%s] post %s was not found or contains no downloadable media." %
                         (site, post_id))
            self.queue.join()
        finally:
            for _ in workers:
                self.queue.put(None)
            for worker in workers:
                worker.join()

        outcome = "stopped" if self.stop_event.is_set() else "finished"
        self.status(site, "%s \u00b7 %s" % (outcome, self.stats[site].summary()))
        self.log("[%s] single post %s: %s" %
                 (site, outcome, self.stats[site].summary()))

    # --- per blog ------------------------------------------------------------

    def _do_site(self, site, skip_reblogs, only_new=False):
        self.stats[site] = SiteStats()
        self.newest[site] = None
        folder = os.path.join(self.out_dir, site)
        os.makedirs(folder, exist_ok=True)
        state = self.get_state(site) or {}

        since = None
        if only_new:
            stored = state.get("newest_post_ts")
            if isinstance(stored, int) and _has_post_folders(folder):
                since = stored
                self.log("[%s] only new posts, from %s onwards" % (site, (_iso(stored) or "")[:10]))
            else:
                self.log("[%s] no stored date (or no saved posts) \u2014 doing a full download" % site)
        self.status(site, "crawling\u2026")

        result = self._crawl_old(site, folder, skip_reblogs, since)
        if result == "notfound":
            if not self._crawl_v2(site, folder, skip_reblogs, since):
                self.log("[%s] not found on the normal endpoint. If the blog is private, "
                         "mature or dashboard-only, add API credentials." % site)
                self.status(site, "not found (API credentials needed?)")
                try:
                    os.rmdir(folder)
                except OSError:
                    pass
                return
        elif result == "error":
            self.status(site, "error \u2014 see log")
            return

        self.queue.join()
        stopped = self.stop_event.is_set()
        if not stopped:
            self._save_state(site, folder, state)
        prefix = "stopped" if stopped else "finished"
        self.status(site, "%s \u00b7 %s" % (prefix, self.stats[site].summary()))
        self.log("[%s] %s: %s" % (site, prefix, self.stats[site].summary()))
        if not stopped:
            self.done(site)

    def _save_state(self, site, folder, state):
        new_state = dict(state)
        new_state["last_downloaded"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Only move the "newest post" mark forward when nothing failed, so a failed
        # post is still inside the range of the next "only new posts" run.
        if self.stats[site].counts["failed"] == 0:
            candidates = [t for t in (state.get("newest_post_ts"), self.newest.get(site))
                          if isinstance(t, int)]
            if candidates:
                new_state["newest_post_ts"] = max(candidates)
        self.save_state(site, new_state)

    def _queue_post(self, site, folder, post_id, ts, meta, media, replace=False):
        if not media:
            return False    # text-only post: nothing to archive
        self.stats[site].add("queued")
        t = _ts(ts)
        if t is not None and (self.newest.get(site) is None or t > self.newest[site]):
            self.newest[site] = t
        self.queue.put({
            "site": site,
            "dir": os.path.join(folder, folder_name(ts, post_id)),
            "meta": meta,
            "media": media,
            "replace": replace,
        })
        return True

    # --- old /api/read endpoint ---------------------------------------------

    def _single_post_old(self, site, post_id):
        url = "https://{0}/api/read".format(_host(site))
        try:
            response = requests.get(url, params={"id": post_id, "num": 1},
                                    proxies=self.proxies, timeout=TIMEOUT)
        except requests.RequestException as e:
            self.log("[%s] post request failed: %r" % (site, e))
            return None
        if response.status_code != 200:
            self.log("[%s] post endpoint returned HTTP %s" % (site, response.status_code))
            return None

        text = response.content.decode("utf-8", errors="replace")
        cleaned = re.sub(
            u"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]+",
            u"", text)
        try:
            data = xmltodict.parse(cleaned)
        except Exception as e:
            self.log("[%s] could not parse post response: %r" % (site, e))
            return None
        posts_node = (data.get("tumblr") or {}).get("posts") or {}
        for post in _as_list(posts_node.get("post")):
            if str(post.get("@id", "")) == post_id:
                return post
        return None

    def _single_post_v2(self, site, post_id):
        api = "https://api.tumblr.com/v2/blog/%s/posts" % quote(_host(site), safe=".")
        params = {"npf": "true", "id": post_id}
        if not self.oauth:
            params["api_key"] = self.consumer_key
        try:
            response = requests.get(api, params=params, auth=self.oauth,
                                    proxies=self.proxies, timeout=TIMEOUT)
        except requests.RequestException as e:
            self.log("[%s] v2 post request failed: %r" % (site, e))
            return None
        if response.status_code != 200:
            self.log("[%s] v2 post API returned %s: %s" %
                     (site, response.status_code, response.text[:300]))
            return None
        try:
            posts = response.json().get("response", {}).get("posts", [])
        except (ValueError, AttributeError) as e:
            self.log("[%s] could not parse v2 post response: %r" % (site, e))
            return None
        for post in posts:
            returned_id = post.get("id_string") or str(post.get("id", ""))
            if returned_id == post_id:
                return post
        return None

    def _crawl_old(self, site, folder, skip_reblogs, since=None):
        base_url = "https://{0}/api/read?num={1}&start={2}"
        start = 0
        while not self.stop_event.is_set():
            media_url = base_url.format(_host(site), MEDIA_NUM, start)
            try:
                response = requests.get(media_url, proxies=self.proxies, timeout=TIMEOUT)
            except requests.RequestException as e:
                self.log("[%s] request failed: %r" % (site, e))
                return "error"

            if response.status_code == 404:
                if start == 0:
                    return "notfound"
                break
            if response.status_code != 200:
                self.log("[%s] HTTP %s from %s" % (site, response.status_code, media_url))
                return "error"

            text = response.content.decode("utf-8", errors="replace")
            cleaned = re.sub(
                u"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]+",
                u"", text)

            try:
                data = xmltodict.parse(cleaned)
            except Exception as e:
                self.log("[%s] could not parse response: %r" % (site, e))
                return "error"

            posts_node = (data.get("tumblr") or {}).get("posts") or {}
            posts = _as_list(posts_node.get("post"))
            if not posts:
                break

            page_ts = []
            for post in posts:
                ts = _ts(post.get("@unix-timestamp"))
                page_ts.append(ts)
                if since is not None and ts is not None and ts < since:
                    continue
                if skip_reblogs and any(k.startswith("@reblogged-") for k in post):
                    continue
                self._queue_post(site, folder, post.get("@id"),
                                 post.get("@unix-timestamp"),
                                 build_metadata(post, site), extract_media_old(post))
            if since is not None and all(t is not None and t < since for t in page_ts):
                break    # a whole page older than the stored date: caught up
            start += MEDIA_NUM
            self.status(site, "crawling\u2026 %s" % self.stats[site].summary())
        return "ok"

    # --- v2 API fallback -------------------------------------------------------

    def _crawl_v2(self, site, folder, skip_reblogs, since=None):
        if not (self.oauth or self.consumer_key):
            return False

        api = "https://api.tumblr.com/v2/blog/%s/posts" % _host(site)
        offset = 0
        while not self.stop_event.is_set():
            params = {"npf": "true", "limit": 20, "offset": offset}
            if not self.oauth:
                params["api_key"] = self.consumer_key
            try:
                resp = requests.get(api, params=params, auth=self.oauth,
                                    proxies=self.proxies, timeout=TIMEOUT)
            except requests.RequestException as e:
                self.log("[%s] v2 request failed: %r" % (site, e))
                break
            if resp.status_code != 200:
                self.log("[%s] v2 API returned %s: %s" % (site, resp.status_code, resp.text[:300]))
                self.status(site, "API error %s \u2014 see log" % resp.status_code)
                break

            posts = resp.json().get("response", {}).get("posts", [])
            if not posts:
                break

            page_ts = []
            for post in posts:
                ts = _ts(post.get("timestamp"))
                page_ts.append(ts)
                if since is not None and ts is not None and ts < since:
                    continue
                if skip_reblogs and (post.get("trail") or post.get("reblogged_from_id")):
                    continue
                pid = post.get("id_string") or str(post.get("id", ""))
                self._queue_post(site, folder, pid, post.get("timestamp"),
                                 build_metadata_v2(post, site), extract_media_v2(post))
            if since is not None and all(t is not None and t < since for t in page_ts):
                break    # a whole page older than the stored date: caught up
            offset += len(posts)
            self.status(site, "crawling\u2026 %s" % self.stats[site].summary())
        return True

    # --- workers ---------------------------------------------------------------

    def _worker(self):
        while True:
            job = self.queue.get()
            if job is None:
                self.queue.task_done()
                return
            site = job["site"]
            result = "failed"
            try:
                result = "stopped" if self.stop_event.is_set() else self._process(job)
            except Exception as e:
                self.log("[%s] error in %s: %r" % (site, os.path.basename(job["dir"]), e))
            finally:
                self.stats[site].add(result)
                self.status(site, self.stats[site].summary())
                self.queue.task_done()

    def _process(self, job):
        post_dir = job["dir"]
        label = os.path.basename(post_dir)
        json_path = os.path.join(post_dir, "post.json")
        if job.get("replace"):
            if os.path.islink(post_dir) or os.path.isfile(post_dir):
                os.remove(post_dir)
            elif os.path.isdir(post_dir):
                shutil.rmtree(post_dir)
        elif os.path.isfile(json_path):
            return "skipped"          # finished on a previous run

        os.makedirs(post_dir, exist_ok=True)

        files = []
        complete = True
        for i, (kind, url) in enumerate(job["media"]):
            if self.stop_event.is_set():
                return "stopped"
            name = "%d%s" % (i, file_extension(url, kind))
            path = os.path.join(post_dir, name)
            if os.path.isfile(path):
                ok = True
            else:
                self.log("[%s] downloading %s/%s" % (job["site"], label, name))
                ok = self._fetch(url, path)
            if ok:
                files.append({"file": name, "source": url})
            else:
                if self.stop_event.is_set():
                    return "stopped"
                complete = False

        if not complete:
            self.log("[%s] incomplete post %s (will be retried next run)" % (job["site"], label))
            return "failed"

        meta = dict(job["meta"])
        meta["files"] = files
        tmp = json_path + ".part"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta, indent=4, ensure_ascii=False))
        os.replace(tmp, json_path)
        return "done"

    def _fetch(self, url, path):
        tmp = path + ".part"
        try:
            for _ in range(RETRY):
                if self.stop_event.is_set():
                    return False
                try:
                    resp = requests.get(url, stream=True, proxies=self.proxies, timeout=TIMEOUT)
                    if resp.status_code in (403, 404):
                        self.log("HTTP %s for %s" % (resp.status_code, url))
                        return False
                    if resp.status_code != 200:
                        continue
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if self.stop_event.is_set():
                                return False
                            fh.write(chunk)
                    os.replace(tmp, path)
                    return True
                except (requests.RequestException, OSError):
                    continue
            self.log("Failed to retrieve %s" % url)
            return False
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
