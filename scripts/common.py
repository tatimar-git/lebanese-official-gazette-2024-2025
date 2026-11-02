# ============================================================
# Shared code for the Official Gazette OCR project
#
#   paths of one year folder, atomic JSON files, the polite
#   HTTP client (one request at a time, fixed delay, every
#   response classified; a CAPTCHA / block page is NEVER solved
#   or bypassed: the program pauses and logs it).
#
# Adapted from gazette/source.py of the main project.
# ============================================================

import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime

import requests

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_URL = "http://www.pcm.gov.lb"

# Year listing pages ("اعداد سنة YYYY") on pcm.gov.lb, checked 28-10-2026
YEAR_LISTINGS = {
    2022: 21198,
    2023: 22469,
    2024: 23685,
    2025: 25182,
}

# Which laptop processes which year (the scripts refuse other years
# unless --force-year is given, so the two laptops never overlap)
ASSIGNED_YEARS = (2024, 2025)

USER_AGENT = ("LebanonGazette-RAG/1.0 (USEK Deep Learning student project; "
              "public pages of pcm.gov.lb only)")

# Arabic output on the Windows console
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ------------------------------------------------------------
# Paths
# ------------------------------------------------------------

class YearPaths:
    def __init__(self, year):
        self.year = int(year)
        self.root = os.path.join(PROJECT_DIR, str(self.year))
        self.images = os.path.join(self.root, "images")
        self.ocr_text = os.path.join(self.root, "ocr_text")
        self.metadata = os.path.join(self.root, "metadata")
        self.issues_dir = os.path.join(self.metadata, "issues")
        self.index = os.path.join(self.metadata, "issues_index.json")
        self.progress = os.path.join(self.root, "progress.json")
        self.log = os.path.join(self.root, "logs", "scraper.log")
        self.ocr_log = os.path.join(self.root, "logs", "ocr.log")
        self.blocked = os.path.join(self.root, "BLOCKED.txt")
        self.stop_file = os.path.join(self.root, "STOP")
        for d in (self.images, self.ocr_text, self.issues_dir, os.path.dirname(self.log)):
            os.makedirs(d, exist_ok=True)

    def issue_file(self, issue_id):
        return os.path.join(self.issues_dir, f"{issue_id}.json")

    def image_path(self, issue_id, part, page, ext):
        name = f"{self.year}_{issue_id}_part{part}_page{page:05d}{ext}"
        return os.path.join(self.images, issue_id, name)

    def text_path(self, issue_id, part, page):
        name = f"{self.year}_{issue_id}_part{part}_page{page:05d}.txt"
        return os.path.join(self.ocr_text, issue_id, name)

    def rel(self, path):
        return os.path.relpath(path, PROJECT_DIR).replace("\\", "/")

    def abs(self, rel):
        return os.path.join(PROJECT_DIR, rel)


def issue_id_of(number, suffix):
    """Issue 49 -> 'issue049'; supplement 49م -> 'issue049m'; 10 م2 -> 'issue010m2'."""
    tail = (suffix or "").replace("م", "m").replace(" ", "")
    return f"issue{int(number):03d}{tail}"


# ------------------------------------------------------------
# Files
# ------------------------------------------------------------

def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    for attempt in range(5):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except PermissionError:          # Windows: being replaced by the other thread
            if attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))


def save_json(path, data):
    """Atomic write: a crash or power cut never leaves a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"   # own temp file per thread
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: file open in an editor / antivirus
            time.sleep(0.5 * (attempt + 1))
    os.replace(tmp, path)


def save_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Logger:
    def __init__(self, path):
        self.path = path

    def __call__(self, message):
        line = f"{now()} {message}"
        print(line, flush=True)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

class AccessBlocked(Exception):
    """CAPTCHA / access denied / rate limit: needs a pause or a human, never bypassed."""

    def __init__(self, message, kind="blocked"):
        super().__init__(message)
        self.kind = kind


class FetchError(Exception):
    pass


_CAPTCHA_MARKERS = ("captcha_resp", "validation request", "user validation required")
_OMSAR_MARKERS = ("you have reached omsar", "website requested is not available")


def classify(response, expect):
    status = response.status_code
    ctype = response.headers.get("Content-Type", "").lower()
    head = response.content[:6000].decode("latin-1", errors="replace").lower()
    if any(m in head for m in _CAPTCHA_MARKERS):
        return "captcha"
    if any(m in head for m in _OMSAR_MARKERS):
        return "site_unavailable"
    if status in (401, 403):
        return "access_denied"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    if status == 404:
        return "not_found"
    if status >= 400:
        return "unexpected"
    if expect == "image" and not ctype.startswith("image/"):
        return "unexpected"
    if expect == "html" and "html" not in ctype:
        return "unexpected"
    return "ok"


BLOCK_KINDS = ("captcha", "site_unavailable", "access_denied", "rate_limited")


class Http:
    def __init__(self, delay, log, timeout=(15, 30), max_retries=3):   # (connect, read) seconds; a page is < 200 KB
        self.delay = delay
        self.log = log
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        # Connection: close = a fresh connection per request. pcm.gov.lb sometimes leaves part of an
        # image on a kept-alive connection ("BadStatusLine" with image bytes, then long timeouts).
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ar,en;q=0.8",
                                     "Connection": "close"})
        self._last = 0.0
        self.count = 0
        self.unexpected_streak = 0

    def _wait(self):
        pause = self.delay - (time.time() - self._last)
        if pause > 0:
            time.sleep(pause)
        self._last = time.time()

    @staticmethod
    def reachable(host="www.pcm.gov.lb", port=80):
        try:
            socket.create_connection((host, port), timeout=10).close()
            return True
        except OSError:
            return False

    def wait_for_network(self):
        if self.reachable():
            return False
        self.log("network / pcm.gov.lb unreachable: waiting (checking once a minute)...")
        while not self.reachable():
            time.sleep(60)
        self.log("connection is back, continuing")
        return True

    def request(self, method, url, expect="html", **kwargs):
        """Returns the response (ok or 404). Raises AccessBlocked or FetchError."""
        attempt = 0
        error = None
        while attempt <= self.max_retries:
            self._wait()
            self.count += 1
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                error = e
                self.session.close()                 # drop any half-read connection
                if self.wait_for_network():
                    continue                     # outage: does not count as an attempt
                attempt += 1
                time.sleep(min(120, 10 * 2 ** attempt))
                continue
            except requests.RequestException as e:
                error = e
                attempt += 1
                time.sleep(min(120, 10 * 2 ** attempt))
                continue
            kind = classify(r, expect)
            if kind in BLOCK_KINDS:
                raise AccessBlocked(f"{kind} (HTTP {r.status_code}) for {url}", kind)
            if kind == "server_error":
                error = FetchError(f"HTTP {r.status_code}")
                attempt += 1
                time.sleep(min(120, 10 * 2 ** attempt))
                continue
            if kind == "unexpected":
                self.unexpected_streak += 1
                if self.unexpected_streak >= 3:
                    raise AccessBlocked(f"3 unexpected responses in a row (last HTTP {r.status_code}, {url})")
                raise FetchError(f"unexpected response HTTP {r.status_code} "
                                 f"{r.headers.get('Content-Type', '')} for {url}")
            self.unexpected_streak = 0
            return r
        raise FetchError(f"{url}: gave up after {self.max_retries + 1} attempts: {error}")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, data, **kw):
        return self.request("POST", url, data=data, **kw)
