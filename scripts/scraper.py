# ============================================================
# Official Gazette scraper (one year per laptop)
#
#   python scripts/scraper.py --year 2025
#
# How pcm.gov.lb publishes an issue (checked 28-10-2026):
#
#   listingbydate.aspx?pageid=<year>   10 issues per page, next pages
#                                      = ASP.NET postbacks
#   subpg.aspx?pageid=<issue>          table of contents (فهرس), links
#                                      archive/j2025/j49/wfm/m1799.htm
#   archive/j2025/j49/wfm/m1799.htm    pagestart=3321 pageend=3321
#   archive/j2025/j49/doc45/3321.gif   Part 1 page image (laws, decrees)
#   archive/j2025/j49/doc46/7057.gif   Part 2 page image (announcements)
#
# Page numbers are continuous inside a part, so only the FIRST and
# LAST item of each part are opened to get the page range; every page
# image of the range is then downloaded directly.
#
# Phases (each one checkpointed, safe to stop at any time):
#   1. index   : list every issue of the year
#   2. ranges  : page range of Part 1 / Part 2 of every issue
#   3. download: every page image, OCR running in a background thread
#
# Resume = run the same command again: finished work is skipped.
# A CAPTCHA / access-denied page is never solved or bypassed: the
# program writes <year>/BLOCKED.txt, waits, and tries one request
# per hour; after --max-block-hours it stops for a human to check.
# ============================================================

import argparse
import re
import signal
import sys
import threading
import time
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import common
from common import (ASSIGNED_YEARS, BASE_URL, YEAR_LISTINGS, AccessBlocked, FetchError, Http, Logger,
                    YearPaths, issue_id_of, load_json, now, save_bytes, save_json)

# "عدد 44 تاريخ 8-10-2026", "عدد 55م تاريخ ...", "عدد 10 م2 تاريخ ...", "م عدد 8 تاريخ ..."
_ISSUE_RE = re.compile(r"(م\s*\d?)?\s*عدد\s*(م?)\s*(\d+)\s*(م\s*\d?)?\s*تاريخ\s*(\d{1,2})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{4})")
_PAGESTART_RE = re.compile(r"pagestart\s*=\s*(\d+)")
_PAGEEND_RE = re.compile(r"pageend\s*=\s*(\d+)")
_IMG_RE = re.compile(r"""<img[^>]+src\s*=\s*["']?([^"'\s>]+\.(?:gif|jpg|jpeg|png))""", re.I)
_PAGER_RE = re.compile(r"(ctl00\$maincont\$[A-Za-z0-9$_]+)")
_ARCHIVE_RE = re.compile(r"/archive/j\d{4}/([^/]+)/", re.I)

CATEGORIES = ("قوانين", "مراسيم", "قرارات", "تعاميم", "بلاغات", "مذكرات", "اعلانات", "إعلانات",
              "احكام", "أحكام", "الجزء الثاني", "مقررات", "اتفاقيات", "معاهدات")

MAX_PAGE_ATTEMPTS = 3       # download attempts of one page (over all runs)
MAX_PAGES_PER_PART = 2000   # sanity limit on a page range read from the site


class Stop(Exception):
    pass


# ------------------------------------------------------------
# Parsing (adapted from gazette/source.py)
# ------------------------------------------------------------

def parse_issue_label(label, year):
    m = _ISSUE_RE.search(label or "")
    if not m:
        return None
    lead, prefix, number, suffix, d, mo, y = m.groups()
    suffix = (suffix or prefix or lead or "").replace(" ", "")
    try:
        when = date(int(y), int(mo), int(d))
    except ValueError:
        return None
    if when.year != year:            # e.g. 2023 issue 1 labelled "5-1-2022" on the site
        try:
            when = when.replace(year=year)
        except ValueError:
            pass
    return {"number": int(number), "suffix": suffix, "date": when.isoformat()}


def soup_of(response):
    response.encoding = response.encoding or "utf-8"
    return BeautifulSoup(response.text, "lxml")


def content_links(soup):
    main = soup.select_one("#ctl00_maintextcol") or soup
    out = []
    for a in main.select("a[href*='subpg.aspx?pageid=']"):
        text = a.get_text(" ", strip=True)
        if text and text != "المزيد":
            out.append((urljoin(BASE_URL, a["href"]), text))
    return out


def list_year_issues(http, year, log):
    url = f"{BASE_URL}/arabic/listingbydate.aspx?pageid={YEAR_LISTINGS[year]}"
    soup = soup_of(http.get(url))
    found, seen, page = [], set(), 1
    while True:
        new = [x for x in content_links(soup) if x[0] not in seen]
        for x in new:
            seen.add(x[0])
            found.append(x)
        log(f"[index] listing page {page}: {len(new)} issues (total {len(found)})")
        pagers = []
        for a in soup.select("a[href*='lbtnPage'], a[href*='lbtnNext']"):
            m = _PAGER_RE.search(a.get("href", ""))
            if m:
                pagers.append((m.group(1), a.get_text(strip=True)))
        target = next((t for t, label in pagers if label == str(page + 1)), None) \
            or next((t for t, label in pagers if label in ("»", ">")), None)
        if not target or page >= 30 or (page > 1 and not new):
            break
        form = {i["name"]: i.get("value", "") for i in soup.select("input[type=hidden][name]")}
        form.update({"__EVENTTARGET": target, "__EVENTARGUMENT": ""})
        soup = soup_of(http.post(url, form))
        page += 1

    issues = {}
    for link, label in found:
        info = parse_issue_label(label, year)
        if info is None:
            log(f"[index] skipped (label not understood): {label}")
            continue
        iid = issue_id_of(info["number"], info["suffix"])
        if iid in issues:
            iid += f"_{len(issues)}"
        issues[iid] = {"issue_id": iid, "label": label, "url": link, **info}
    return dict(sorted(issues.items(), key=lambda kv: (kv[1]["date"], kv[0])))


def parse_toc(html):
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one("div.contentContainer")
    if content is None:
        raise FetchError("issue page without table of contents")
    category, heading = "", ""
    items = []
    for p in content.find_all(["p", "div", "h2", "h3", "h4"]):
        links = [a for a in p.find_all("a", href=True) if "/archive/" in a["href"]]
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if links:
            for a in links:
                title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip() or text
                url = urljoin(BASE_URL, a["href"])
                if any(i["url"] == url for i in items):
                    continue
                items.append({"seq": len(items) + 1, "part": 2 if "/part2/" in url.lower() else 1,
                              "section": " › ".join(x for x in (category, heading) if x),
                              "title": title, "url": url})
            continue
        if p.find(["strong", "b"]) is None or "فهرس" in text or len(text) > 120:
            continue
        if any(text.startswith(c) for c in CATEGORIES):
            category, heading = text, ""
        else:
            heading = text
    return items


def fetch_item(http, url):
    r = http.get(url, expect="any")
    if r.status_code == 404:
        raise FetchError(f"item page not found (404): {url}")
    html = r.content.decode("latin-1")          # windows-1256 page: only digits / URLs are read
    start, end, img = _PAGESTART_RE.search(html), _PAGEEND_RE.search(html), _IMG_RE.search(html)
    if not (start and img):
        raise FetchError(f"no page range / page image in {url}")
    first_image = urljoin(url, img.group(1))
    return {"start": int(start.group(1)), "end": int(end.group(1)) if end else int(start.group(1)),
            "image_dir": first_image.rsplit("/", 1)[0] + "/",
            "ext": "." + first_image.rsplit(".", 1)[-1].lower()}


# ------------------------------------------------------------
# Scraper
# ------------------------------------------------------------

class Scraper:
    def __init__(self, args):
        self.args = args
        self.year = args.year
        self.paths = YearPaths(self.year)
        self.log = Logger(self.paths.log)
        self.http = Http(args.delay, self.log)
        self.issues = {}            # issue_id -> issue record (metadata/issues/<id>.json)
        self.stop_requested = False
        self.ocr = None
        self.last_push = time.time()
        self.push_thread = None

    # --- control ---------------------------------------------

    def check_stop(self):
        if self.stop_requested or common.os.path.exists(self.paths.stop_file):
            raise Stop()

    def guarded(self, fn, *a):
        """Run one network step; on a block, pause and retry once per hour (never bypass)."""
        waited = 0.0
        outage_wait = 5.0          # "website not available" page: 5, 10, 20, 40, 60 min
        while True:
            self.check_stop()
            try:
                result = fn(*a)
                if common.os.path.exists(self.paths.blocked):
                    common.os.remove(self.paths.blocked)
                    self.log("access is working again, BLOCKED.txt removed")
                return result
            except AccessBlocked as e:
                if waited >= self.args.max_block_hours:
                    self.log(f"[BLOCKED] still blocked after {waited:.0f} h: stopping. Human check needed.")
                    raise
                with open(self.paths.blocked, "a", encoding="utf-8") as f:
                    f.write(f"{now()} {e}\n")
                if e.kind == "site_unavailable":
                    minutes = min(outage_wait, self.args.block_wait_minutes)
                    outage_wait *= 2
                    self.log(f"[SITE DOWN] {e}")
                    self.log(f"[SITE DOWN] pcm.gov.lb answered 'website not available' (server outage or "
                             f"overload): waiting {minutes:.0f} min before one new try.")
                else:
                    minutes = self.args.block_wait_minutes
                    self.log(f"[BLOCKED] {e}")
                    self.log(f"[BLOCKED] The site refused access. Nothing is bypassed: waiting "
                             f"{minutes:.0f} min before one new try (Ctrl+C to stop; progress is saved).")
                self.save_progress(status="paused_blocked", note=str(e))
                end = time.time() + minutes * 60
                while time.time() < end:
                    self.check_stop()
                    time.sleep(5)
                waited += minutes / 60
                self.http.unexpected_streak = 0

    # --- phase 1: index --------------------------------------

    def phase_index(self):
        index = load_json(self.paths.index)
        if index and index.get("complete") and not self.args.refresh_index:
            self.log(f"[index] {len(index['issues'])} issues already listed (use --refresh-index to re-read)")
        else:
            self.log(f"[index] reading the {self.year} listing on pcm.gov.lb ...")
            issues = self.guarded(list_year_issues, self.http, self.year, self.log)
            if not issues:
                raise FetchError("no issue found in the year listing (site layout changed?)")
            index = {"year": self.year, "listing_url": f"{BASE_URL}/arabic/listingbydate.aspx?pageid="
                     f"{YEAR_LISTINGS[self.year]}", "read_at": now(), "complete": True, "issues": issues}
            save_json(self.paths.index, index)
            self.log(f"[index] {len(issues)} issues found and saved")
        for iid, info in index["issues"].items():
            record = load_json(self.paths.issue_file(iid))
            if record is None:
                record = {**info, "year": self.year, "status": "new", "parts": {}, "toc": [], "pages": [],
                          "errors": []}
                save_json(self.paths.issue_file(iid), record)
            self.issues[iid] = record

    # --- phase 2: page ranges ---------------------------------

    def read_ranges(self, rec):
        r = self.http.get(rec["url"])
        items = parse_toc(r.text)
        rec["toc"] = items
        m = next((_ARCHIVE_RE.search(i["url"]) for i in items if _ARCHIVE_RE.search(i["url"])), None)
        rec["archive_folder"] = m.group(1) if m else None
        parts = {}
        for part in (1, 2):
            if part == 2 and self.args.part1_only:
                continue
            p_items = [i for i in items if i["part"] == part]
            if not p_items:
                continue
            first = fetch_item(self.http, p_items[0]["url"])
            last = first if len(p_items) == 1 else fetch_item(self.http, p_items[-1]["url"])
            start = min(first["start"], last["start"])
            end = max(first["end"], last["end"])
            if end < start or end - start + 1 > MAX_PAGES_PER_PART:
                raise FetchError(f"part {part}: strange page range {start}-{end}")
            parts[str(part)] = {"start": start, "end": end, "image_dir": first["image_dir"],
                                "ext": first["ext"], "items": len(p_items)}
        return parts

    def phase_ranges(self):
        issues = list(self.issues.values())
        if self.args.limit_issues:
            issues = issues[:self.args.limit_issues]
        todo = [r for r in issues if r["status"] in ("new", "range_error")
                or (r["status"] == "ranges_done" and not r["pages"])]
        self.log(f"[ranges] {len(todo)} issues need their page ranges")
        for n, rec in enumerate(todo, 1):
            self.check_stop()
            try:
                parts = self.guarded(self.read_ranges, rec)
            except FetchError as e:
                rec["status"] = "range_error"
                rec["errors"].append({"at": now(), "phase": "ranges", "error": str(e)})
                save_json(self.paths.issue_file(rec["issue_id"]), rec)
                self.log(f"[ranges] {rec['label']}: ERROR {e}")
                continue
            rec["parts"] = parts
            rec["pages"] = []
            for part, info in parts.items():
                for page in range(info["start"], info["end"] + 1):
                    rec["pages"].append({
                        "part": int(part), "page": page,
                        "url": f"{info['image_dir']}{page}{info['ext']}",
                        "image": self.paths.rel(self.paths.image_path(rec["issue_id"], int(part), page,
                                                                      info["ext"])),
                        "text": self.paths.rel(self.paths.text_path(rec["issue_id"], int(part), page)),
                        "status": "pending", "attempts": 0, "error": None, "bytes": None,
                        "downloaded_at": None})
            rec["status"] = "ranges_done" if rec["pages"] else "no_pages"
            save_json(self.paths.issue_file(rec["issue_id"]), rec)
            desc = ", ".join(f"part {p}: pages {i['start']}-{i['end']}" for p, i in parts.items())
            self.log(f"[ranges] {n}/{len(todo)} {rec['label']} -> {desc or 'no page'}")
            self.save_progress()

    # --- phase 3: download ------------------------------------

    def download_page(self, rec, pg):
        r = self.http.get(pg["url"], expect="image")
        if r.status_code == 404:
            return None
        return r.content

    def phase_download(self):
        issues = [r for r in self.issues.values() if r["pages"] and r["status"] != "walking"]
        if self.args.limit_issues:
            issues = issues[:self.args.limit_issues]
        budget = self.args.limit_pages
        for rec in issues:
            todo = [p for p in rec["pages"] if p["status"] == "pending"
                    or (p["status"] == "failed" and p["attempts"] < MAX_PAGE_ATTEMPTS)]
            if self.args.part1_only:
                todo = [p for p in todo if p["part"] == 1]
            # a file already on disk (e.g. copied from another run) is not downloaded again
            for p in todo[:]:
                if common.os.path.exists(self.paths.abs(p["image"])):
                    p.update(status="downloaded", error=None,
                             bytes=common.os.path.getsize(self.paths.abs(p["image"])))
                    todo.remove(p)
            if not todo:
                self.finish_issue(rec)
                continue
            self.log(f"[download] {rec['label']} ({rec['issue_id']}): {len(todo)} pages to download")
            for p in todo:
                if budget is not None and budget <= 0:
                    save_json(self.paths.issue_file(rec["issue_id"]), rec)
                    self.log(f"[download] --limit-pages reached, stopping")
                    return
                self.check_stop()
                p["attempts"] += 1
                try:
                    data = self.guarded(self.download_page, rec, p)
                except FetchError as e:
                    p.update(status="failed", error=str(e))
                    self.log(f"[download] page {p['page']} FAILED ({p['attempts']}/{MAX_PAGE_ATTEMPTS}): {e}")
                else:
                    if data is None:
                        p.update(status="missing", error="HTTP 404 on the site")
                        self.log(f"[download] page {p['page']}: not on the site (404)")
                    else:
                        save_bytes(self.paths.abs(p["image"]), data)
                        p.update(status="downloaded", error=None, bytes=len(data), downloaded_at=now())
                        if self.ocr:
                            self.ocr.notify()
                save_json(self.paths.issue_file(rec["issue_id"]), rec)
                if budget is not None:
                    budget -= 1
                pr = self.save_progress()
                if p["status"] == "downloaded":
                    print(f"  {rec['issue_id']} part{p['part']} page {p['page']}  "
                          f"[{pr['images_downloaded']}/{pr['images_total_known']} = "
                          f"{pr['download_percent']}%  OCR {pr['ocr_pages_completed']}]", flush=True)
            self.finish_issue(rec)

    # --- direct mode: walk the image folders, no table-of-contents pages ---
    #
    #   archive/j2025/j20/doc45/1163.gif, 1164.gif, ... until a page is missing
    #   Part 1 (doc45) and Part 2 (doc46) page numbers continue from the previous
    #   regular issue; a supplement (j17m1, ...) starts again at page 1.
    #   One request per page (each one is a download) + one check per part end.

    def check_access(self):
        """One normal request to the year listing: a CAPTCHA here means wait, never work around it."""
        self.http.get(f"{BASE_URL}/arabic/listingbydate.aspx?pageid={YEAR_LISTINGS[self.year]}")

    def probe(self, url, known_url):
        """Image bytes, or None when the page does not exist. The site answers a missing file
        with its 'website not available' page: a known image is fetched to tell a missing page
        from a real outage (an outage raises AccessBlocked and is waited out)."""
        try:
            r = self.http.get(url, expect="image")
            return None if r.status_code == 404 else r.content
        except AccessBlocked as e:
            if e.kind != "site_unavailable":
                raise
        except FetchError:
            pass                                  # odd non-image answer: checked like a missing page
        self.http.get(known_url or self.known_good_url(), expect="image")
        self.http.unexpected_streak = 0
        return None

    def known_good_url(self):
        for r in self.issues.values():
            for p in r["pages"]:
                if p["status"] == "downloaded":
                    return p["url"]
        return f"{BASE_URL}/archive/j{self.year}/j1/doc45/1.gif"

    def walk_part(self, rec, part, start, known_url):
        folder = rec["archive_folder"]
        doc = "doc45" if part == 1 else "doc46"
        image_dir = f"{BASE_URL}/archive/j{self.year}/{folder}/{doc}/"
        info = rec["parts"].setdefault(str(part), {"start": None, "end": None, "image_dir": image_dir,
                                                   "ext": ".gif", "found_by": "direct", "done": False})
        if info.get("done"):
            return info
        page = info["end"] + 1 if info["end"] else start
        misses_at_start = 0
        while page - (info["start"] or start) < MAX_PAGES_PER_PART:
            self.check_stop()
            url = f"{image_dir}{page}.gif"
            if info["end"]:
                known_url = f"{image_dir}{info['end']}.gif"
            data = self.guarded(self.probe, url, known_url)
            if data is None:
                if info["start"] is None and misses_at_start < 2:
                    misses_at_start += 1          # expected first page absent: try the next one
                    page += 1
                    continue
                break
            img = self.paths.image_path(rec["issue_id"], part, page, ".gif")
            save_bytes(img, data)
            rec["pages"].append({
                "part": part, "page": page, "url": url, "image": self.paths.rel(img),
                "text": self.paths.rel(self.paths.text_path(rec["issue_id"], part, page)),
                "status": "downloaded", "attempts": 1, "error": None, "bytes": len(data),
                "downloaded_at": now()})
            info["start"] = info["start"] or page
            info["end"] = page
            save_json(self.paths.issue_file(rec["issue_id"]), rec)
            if self.ocr:
                self.ocr.notify()
            pr = self.save_progress()
            print(f"  {rec['issue_id']} part{part} page {page}  [{pr['images_downloaded']} downloaded, "
                  f"OCR {pr['ocr_pages_completed']}]", flush=True)
            page += 1
        info["done"] = True
        if info["start"] is None:
            del rec["parts"][str(part)]
            rec.setdefault("no_part", []).append(part)     # checked: this issue has no such part
        save_json(self.paths.issue_file(rec["issue_id"]), rec)
        return info

    def phase_direct(self):
        self.log("[direct] checking that the site accepts this laptop again (normal listing page)...")
        self.guarded(self.check_access)
        self.log("[direct] access OK")
        self.phase_download()                     # issues whose ranges are already known
        last_end = {1: None, 2: None}
        recs = sorted(self.issues.values(), key=lambda r: (r["date"], r["number"], r["suffix"]))
        for rec in recs:
            regular = not rec["suffix"]
            from_toc = rec["pages"] and not any(i.get("found_by") == "direct" for i in rec["parts"].values())
            wanted = (1,) if not regular or self.args.part1_only else (1, 2)
            # parts still to walk (a Part-1-only run leaves Part 2 for a later run)
            todo = [] if from_toc else [
                p for p in wanted if p not in rec.get("no_part", [])
                and not rec["parts"].get(str(p), {}).get("done")]
            if not todo:
                if regular:
                    for part in (1, 2):
                        info = rec["parts"].get(str(part))
                        if info and info.get("end"):
                            last_end[part] = info["end"]
                continue
            rec.setdefault("archive_folder", None)
            rec["archive_folder"] = rec["archive_folder"] or \
                f"j{rec['number']}{rec['suffix'].replace('م', 'm')}"
            rec["status"] = "walking"
            self.log(f"[direct] {rec['label']} ({rec['archive_folder']})")
            for part in wanted:
                if part not in todo:
                    info = rec["parts"].get(str(part))
                    if regular and info and info.get("end"):
                        last_end[part] = info["end"]
                    continue
                if regular and last_end[part] is None:
                    rec["errors"].append({"at": now(), "phase": "direct",
                                          "error": f"part {part}: previous issue's last page unknown"})
                    continue
                start = last_end[part] + 1 if regular else 1
                known = None
                if regular:
                    prev = next((r for r in reversed(recs[:recs.index(rec)])
                                 if not r["suffix"] and r["parts"].get(str(part))), None)
                    if prev:
                        known = prev["parts"][str(part)]["image_dir"] + f"{last_end[part]}.gif"
                info = self.walk_part(rec, part, start, known)
                if regular and info.get("end"):
                    last_end[part] = info["end"]
                desc = f"pages {info['start']}-{info['end']}" if info.get("start") else "no page"
                self.log(f"[direct] {rec['issue_id']} part {part}: {desc}")
            rec["status"] = "downloaded" if rec["pages"] else "no_pages"
            save_json(self.paths.issue_file(rec["issue_id"]), rec)

    def finish_issue(self, rec):
        if all(p["status"] in ("downloaded", "missing") for p in rec["pages"]):
            if rec["status"] != "downloaded":
                rec["status"] = "downloaded"
                save_json(self.paths.issue_file(rec["issue_id"]), rec)
                self.log(f"[download] {rec['label']}: all pages done")

    # --- progress ---------------------------------------------

    def save_progress(self, status="running", note=None):
        import progress
        pr = progress.compute(self.paths, issues=list(self.issues.values()),
                              ocr_state=self.ocr.state_snapshot() if self.ocr else None)
        pr["status"] = status
        if note:
            pr["note"] = note
        pr["requests_this_run"] = self.http.count
        save_json(self.paths.progress, pr)
        self.maybe_push()
        return pr

    def maybe_push(self, force=False):
        """GitHub push in the background every --auto-push-minutes (a failure never stops scraping)."""
        minutes = self.args.auto_push_minutes
        if minutes <= 0 or self.args.limit_issues or self.args.limit_pages or (self.push_thread and self.push_thread.is_alive()):
            return
        if not force and time.time() - self.last_push < minutes * 60:
            return
        import git_sync
        self.last_push = time.time()
        self.push_thread = threading.Thread(target=git_sync.sync, args=(self.year, self.log), daemon=True)
        self.push_thread.start()

    # --- main -------------------------------------------------

    def run(self):
        self.log(f"===== Official Gazette {self.year}: start (delay {self.args.delay}s, "
                 f"OCR {'off' if self.args.no_ocr else 'on'}) =====")
        if common.os.path.exists(self.paths.stop_file):
            common.os.remove(self.paths.stop_file)
        if not self.args.no_ocr:
            import ocr
            self.ocr = ocr.OcrWorker(self.paths, self.log, engine=self.args.ocr_engine)
            self.ocr.start()
        code = 0
        try:
            self.phase_index()
            self.save_progress()
            if self.args.direct:
                self.phase_direct()
            else:
                self.phase_ranges()
            if not self.args.ranges_only and not self.args.direct:
                self.phase_download()
            done = self.save_progress(status="downloads_finished")
            self.log(f"[done] downloads finished for {self.year}: {done['images_downloaded']} images, "
                     f"{done['download_failures']} failures, {done['images_missing']} missing on the site")
        except Stop:
            self.log("stop requested: progress saved. Run the same command again to resume.")
            self.save_progress(status="stopped")
            code = 3                    # stopped on purpose (Ctrl+C / STOP file)
        except AccessBlocked as e:
            self.save_progress(status="blocked", note=str(e))
            self.log("Stopped because the site keeps refusing access. Open the site in a browser to check, "
                     "wait a few hours, then run the same command again.")
            code = 2
        finally:
            if self.ocr:
                if code == 0 and not self.stop_requested:
                    self.log("[ocr] waiting for OCR to finish the downloaded pages (Ctrl+C to stop)...")
                    try:
                        self.ocr.finish()
                    except KeyboardInterrupt:
                        pass
                self.ocr.stop()
                st = "finished" if code == 0 else ("blocked" if code == 2 else "stopped")
                pr = self.save_progress(status=st)
                self.log(f"[ocr] {pr['ocr_pages_completed']} pages OCR'ed, {pr['ocr_failures']} OCR failures")
            if self.args.auto_push_minutes > 0 and not (self.args.limit_issues or self.args.limit_pages):
                if self.push_thread and self.push_thread.is_alive():
                    self.push_thread.join(300)
                self.maybe_push(force=True)
                self.push_thread.join(300)
        return code


def main():
    ap = argparse.ArgumentParser(description="Download and OCR one year of the Lebanese Official Gazette.")
    ap.add_argument("--year", type=int, required=True, help="2025 (laptop 1) or 2024 (laptop 2)")
    ap.add_argument("--delay", type=float, default=10.0, help="seconds between two requests (default 10)")
    ap.add_argument("--no-ocr", action="store_true", help="download only (run scripts/ocr.py later)")
    ap.add_argument("--ocr-engine", default="easyocr", choices=["easyocr", "tesseract"])
    ap.add_argument("--part1-only", action="store_true", help="skip Part 2 (announcements)")
    ap.add_argument("--direct", action="store_true",
                    help="walk the image folders page by page (no table-of-contents pages)")
    ap.add_argument("--ranges-only", action="store_true", help="phases 1-2 only (no image download)")
    ap.add_argument("--limit-issues", type=int, help="test: only the first N issues")
    ap.add_argument("--limit-pages", type=int, help="test: stop after N page downloads")
    ap.add_argument("--refresh-index", action="store_true", help="re-read the year listing (new issues)")
    ap.add_argument("--block-wait-minutes", type=float, default=60)
    ap.add_argument("--max-block-hours", type=float, default=24)
    ap.add_argument("--auto-push-minutes", type=float, default=60,
                    help="push <year> results to GitHub every N minutes (0 = never)")
    ap.add_argument("--force-year", action="store_true", help="allow a year other than 2024 / 2025")
    args = ap.parse_args()
    if args.year not in YEAR_LISTINGS:
        sys.exit(f"Unknown year {args.year}. Known: {sorted(YEAR_LISTINGS)}")
    if args.year not in ASSIGNED_YEARS and not args.force_year:
        sys.exit(f"Year {args.year} is not assigned to a laptop (2025 = laptop 1, 2024 = laptop 2).")
    args.delay = max(3.0, args.delay)

    scraper = Scraper(args)

    def on_ctrl_c(signum, frame):
        if scraper.stop_requested:
            raise KeyboardInterrupt
        scraper.stop_requested = True
        print("\nStopping after the current request... (press Ctrl+C again to force)", flush=True)
    signal.signal(signal.SIGINT, on_ctrl_c)
    sys.exit(scraper.run())


if __name__ == "__main__":
    main()
