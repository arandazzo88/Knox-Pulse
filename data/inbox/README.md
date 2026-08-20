# data/inbox/

Drop folder for events that **can't** be scraped from CI — Instagram accounts
(need a logged-in browser) and the Honky Tonk Events Google Calendar (needs an
OAuth connection). The weekly Cowork task on Daz's Mac writes files here; the
GitHub Actions crawler picks them up on its next run.

## How it works

1. A file named like `2026-08-27-instagram.json` is committed into this folder.
   It is a **JSON array** of listing records in the exact same shape as the
   objects in `data/listings.json` (see any entry there for the field set).
2. On the next run, `crawler/crawl.py` (`process_inbox()`, before any site
   crawling) reads every `*.json` file here, merges records whose `id` isn't
   already in the catalog, and **deletes the file**.
3. The weekly workflow commits both the updated `data/listings.json` and the
   removed inbox files.

## Rules

- Each file must be a top-level JSON **array**. A malformed or non-array file is
  logged and **left in place** (it won't crash the crawl) so it can be fixed.
- Every record needs a unique `id`; duplicates against the catalog are skipped.
- `.gitkeep` and this `README.md` are ignored by the processor (only `*.json`).
