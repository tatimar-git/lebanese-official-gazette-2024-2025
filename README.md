# Lebanese Official Gazette (الجريدة الرسمية): OCR dataset, 2024 and 2025

This repository holds Arabic OCR text and metadata for the Lebanese Official Gazette. The source is the
scanned page images published on [pcm.gov.lb](http://www.pcm.gov.lb). It was collected for a
Deep Learning course project, a RAG chatbot over Lebanese legislation.

## Contents

| Year | Issues with OCR | OCR pages | Issues in metadata |
|---|---|---|---|
| 2024 | 13 | 1,508 | 56 |
| 2025 | 53 | 6,101 | 72 |

The 2024 OCR is still in progress, so not every issue in the metadata has OCR text yet.
`progress.json` in each year folder shows the current state.

```
2024/ and 2025/
  ocr_text/issueNNN/<year>_issueNNN_partP_pageNNNNN.txt   UTF-8 Arabic text, one file per scanned page
  metadata/issues/issueNNN.json                           issue number, date, source URLs, table of contents
  metadata/issues_index.json                              list of all issues for the year
  metadata/ocr_status.json                                OCR status per issue
  progress.json                                           download and OCR progress summary
scripts/                                                  scraper and OCR scripts that produced the data
```

The page images (about 560 MB) are not included. Each issue's metadata JSON has the original
`image_dir` URL for every part, so the images can be downloaded again with `scripts/scraper.py`.

## Quick start (Python)

```python
import json, pathlib

root = pathlib.Path("2025")
meta = json.loads((root / "metadata/issues/issue001.json").read_text(encoding="utf-8"))
print(meta["label"], meta["date"])

pages = sorted((root / "ocr_text/issue001").glob("*.txt"))
text = "\n".join(p.read_text(encoding="utf-8") for p in pages)
```

## Notes

- The text comes from automatic OCR, so expect recognition errors, especially in tables and
  low-quality scans.
- The Official Gazette is a public government publication. This repository is for educational use.
