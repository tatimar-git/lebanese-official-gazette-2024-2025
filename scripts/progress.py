# ============================================================
# Progress of one year (works while the scraper is running)
#
#   python scripts/progress.py --year 2025
#   python scripts/progress.py --year 2025 --failures
#
# Everything is recomputed from the files on disk:
#   <year>/metadata/issues/*.json   (written by scraper.py)
#   <year>/metadata/ocr_status.json (written by the OCR worker)
# and saved to <year>/progress.json.
# ============================================================

import argparse
import glob
import os

from common import YearPaths, load_json, now, save_json


def load_issues(paths):
    return [load_json(f) for f in sorted(glob.glob(os.path.join(paths.issues_dir, "*.json")))]


def compute(paths, issues=None, ocr_state=None):
    if issues is None:
        issues = load_issues(paths)
    if ocr_state is None:
        ocr_state = load_json(os.path.join(paths.metadata, "ocr_status.json"), {}) or {}
    index = load_json(paths.index, {}) or {}
    pages = [p for r in issues for p in r.get("pages", [])]
    count = lambda s: sum(1 for p in pages if p["status"] == s)
    downloaded = count("downloaded")
    missing = count("missing")
    failed = count("failed")
    ocr_done = sum(1 for v in ocr_state.values() if v.get("status") == "done")
    ocr_failed = sum(1 for v in ocr_state.values() if v.get("status") == "failed")
    with_ranges = [r for r in issues if r.get("status") not in ("new", "range_error")]
    finished = sorted((r for r in issues if r.get("status") == "downloaded"), key=lambda r: r["date"])
    total = len(pages)
    total_issues = len(index.get("issues", {})) or len(issues)
    return {
        "year": paths.year,
        "updated_at": now(),
        "issues_discovered": total_issues,
        "issues_with_page_ranges": len(with_ranges),
        "issues_range_errors": sum(1 for r in issues if r.get("status") == "range_error"),
        "issues_fully_downloaded": len(finished),
        "last_processed_issue": finished[-1]["label"] if finished else None,
        "images_total_known": total,
        "images_downloaded": downloaded,
        "images_missing_on_site": missing,
        "images_missing": missing,
        "images_remaining": total - downloaded - missing,
        "download_failures": failed,
        "ocr_pages_completed": ocr_done,
        "ocr_failures": ocr_failed,
        "ocr_remaining": downloaded - ocr_done - ocr_failed,
        "download_percent": round(100 * (downloaded + missing) / total, 1) if total else 0.0,
        "ocr_percent": round(100 * ocr_done / total, 1) if total else 0.0,
        "ranges_known_for_all_issues": len(with_ranges) == total_issues and total_issues > 0,
        "megabytes_downloaded": round(sum(p.get("bytes") or 0 for p in pages) / 1e6, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--failures", action="store_true", help="list failed downloads / OCR pages")
    args = ap.parse_args()
    paths = YearPaths(args.year)
    issues = load_issues(paths)
    pr = compute(paths, issues)
    old = load_json(paths.progress, {}) or {}
    for k in ("status", "note"):
        if k in old:
            pr[k] = old[k]
    save_json(paths.progress, pr)

    print(f"\n=== Official Gazette {pr['year']} — {pr['updated_at']} ===")
    print(f"status (last run)       : {pr.get('status', '-')}")
    print(f"issues discovered       : {pr['issues_discovered']}")
    print(f"issues with page ranges : {pr['issues_with_page_ranges']}  (errors: {pr['issues_range_errors']})")
    print(f"issues fully downloaded : {pr['issues_fully_downloaded']}")
    print(f"last processed issue    : {pr['last_processed_issue']}")
    print(f"images known            : {pr['images_total_known']}"
          + ("" if pr["ranges_known_for_all_issues"] else "  (more once all page ranges are read)"))
    print(f"images downloaded       : {pr['images_downloaded']}  ({pr['megabytes_downloaded']} MB)")
    print(f"images remaining        : {pr['images_remaining']}")
    print(f"missing on the site     : {pr['images_missing_on_site']}")
    print(f"download failures       : {pr['download_failures']}")
    print(f"OCR pages completed     : {pr['ocr_pages_completed']}  (remaining {pr['ocr_remaining']})")
    print(f"OCR failures            : {pr['ocr_failures']}")
    print(f"PROGRESS                : download {pr['download_percent']}%   OCR {pr['ocr_percent']}%")
    if os.path.exists(paths.blocked):
        print(f"\n!! The site blocked access at some point, see {paths.blocked}")
    if args.failures:
        print("\nFailed downloads:")
        for r in issues:
            for p in r.get("pages", []):
                if p["status"] == "failed":
                    print(f"  {r['issue_id']} part{p['part']} page {p['page']}: {p['error']}")
            for e in r.get("errors", []):
                print(f"  {r['issue_id']} {e['phase']}: {e['error']}")
        print("Failed OCR:")
        ocr_state = load_json(os.path.join(paths.metadata, "ocr_status.json"), {}) or {}
        for key, v in ocr_state.items():
            if v.get("status") == "failed":
                print(f"  {key}: {v.get('error')}")


if __name__ == "__main__":
    main()
