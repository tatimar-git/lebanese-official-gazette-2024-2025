# ============================================================
# Push the results of ONE year to GitHub
#
#   python scripts/git_sync.py --year 2024
#
# Only Official_Gazette_Project/<year>/ is staged, so laptop 1
# (2025) and laptop 2 (2024) never touch the same files and never
# conflict. Images are not pushed (.gitignore: too big for GitHub,
# see README); OCR text, metadata and progress are.
#
# Called automatically by scraper.py every --auto-push-minutes.
# A failure (no internet, no credentials) is only logged: the
# scraping continues and the next push sends everything.
# ============================================================

import argparse
import os
import subprocess
import sys

from common import PROJECT_DIR


def run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout + r.stderr).strip()


def sync(year, log=print):
    repo_rc, repo = run(["git", "rev-parse", "--show-toplevel"], PROJECT_DIR)
    if repo_rc != 0:
        log(f"[git] not a git repository: {repo}")
        return False
    folder = os.path.relpath(os.path.join(PROJECT_DIR, str(year)), repo).replace("\\", "/")
    run(["git", "add", "--", folder], repo)
    rc, staged = run(["git", "diff", "--cached", "--name-only", "--", folder], repo)
    if staged:
        n = len(staged.splitlines())
        rc, out = run(["git", "commit", "-m", f"Gazette {year}: OCR results and progress ({n} files)",
                       "--", folder], repo)
        if rc != 0:
            log(f"[git] commit failed: {out[-300:]}")
            return False
    rc, out = run(["git", "pull", "--rebase", "--autostash"], repo)
    if rc != 0:
        log(f"[git] pull failed (will retry next time): {out[-300:]}")
        run(["git", "rebase", "--abort"], repo)
        return False
    rc, out = run(["git", "push"], repo)
    if rc != 0:
        log(f"[git] push failed (will retry next time): {out[-300:]}")
        return False
    log(f"[git] {year} results pushed to GitHub")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    sys.exit(0 if sync(ap.parse_args().year) else 1)
