# Manga Tracker — cloud setup (GitHub Actions + GitHub Pages)

Everything runs on GitHub's free tier: a scheduled Action checks your
tracked pages with a real headless browser, commits the results, and a
GitHub Pages site (accessible from any device, no computer of yours needs
to be on) reads those results and alerts you.

**One thing worth knowing up front:** on GitHub's free plan, Pages sites
must live in a *public* repository. That means the repo's code — including
`config.json`, i.e. the list of things you're tracking — is technically
visible to anyone who looks. For tracking public secondhand-manga listings
this is low-stakes, but if that bothers you, GitHub Pro (~$4/month) allows
private-repo Pages instead.

## 1. Create the repo

1. On GitHub, click **New repository**. Name it something like
   `manga-tracker`. Keep it **Public**. Don't initialize with a README
   (you already have one).
2. Upload all the files in this folder to the repo — either drag-and-drop
   them in the GitHub web UI, or if you're comfortable with git:

   ```
   cd manga-tracker
   git init
   git add .
   git commit -m "Initial setup"
   git branch -M main
   git remote add origin https://github.com/<your-username>/manga-tracker.git
   git push -u origin main
   ```

   Make sure the `.github/workflows/scrape.yml` file actually lands inside
   a `.github/workflows/` folder — some drag-and-drop uploads flatten
   folders, so double check this in the repo after uploading.

## 2. Turn on GitHub Pages

1. In the repo, go to **Settings → Pages**.
2. Under "Build and deployment," set **Source** to **Deploy from a
   branch**.
3. Branch: **main**, folder: **/ (root)**. Save.
4. GitHub will give you a URL like
   `https://<your-username>.github.io/manga-tracker/`. It takes a minute
   or two to go live the first time.

## 3. Check workflow permissions

1. Go to **Settings → Actions → General**.
2. Scroll to "Workflow permissions" and make sure **Read and write
   permissions** is selected, then Save.
   (This lets the scheduled job commit `listings.json` back to the repo.
   If it's missing, the workflow will fail on the "Commit updated
   listings" step with a permissions error.)

## 4. Run it for the first time

1. Go to the **Actions** tab → **Scrape listings** (in the left sidebar)
   → **Run workflow** → Run workflow.
2. Watch it run — should take a minute or two. The first run just sets a
   baseline; it won't flag existing listings as "new," only ones that show
   up after that.
3. If it finds 0 listings for a tracked page, that likely means the
   scraper's selectors need adjusting for that site — see "If it's not
   finding listings" below.

After this first run, it'll run automatically every 15 minutes on its own.

## 5. Visit your tracker

Open `https://<your-username>.github.io/manga-tracker/` on any device.
Click **Enable notifications** once. Leave the tab open (or pinned) and
you'll get alerted as new listings are found, refreshed from the live repo
every 20 seconds.

## 6. Add or remove tracked series

Two ways:

- **Quick:** in GitHub, open `config.json` in the repo, click the pencil
  (edit) icon, add or remove an entry, commit directly to `main`.
- **From the page:** add a link in the tracker UI, then click "Download
  config.json" in the banner that appears, and re-upload that file to the
  repo (GitHub will let you overwrite the existing file — drag it into the
  repo's file list, it'll show as a replacement, then commit).

Either way, the next scheduled run (within 15 minutes) picks up the change.

## If it's not finding listings

Mandarake's markup isn't publicly documented, so `scraper.py`'s selectors
are a best-effort guess. To check what's actually happening:

1. In `.github/workflows/scrape.yml`, temporarily change the run command
   to `python scraper.py --debug`.
2. After the run, download the workflow's artifact/logs, or add a step to
   upload `debug_*.html` as a workflow artifact — happy to help set that
   up if this comes up.
3. Send me what's in the debug HTML and I'll adjust the selectors in
   `extract_mandarake_items`.

## How "new" detection works

An item stays flagged "new" for 24 hours after it's first seen — not just
on the one scraper run that found it — so the page has a wide window to
notice it even if it wasn't open at the exact moment the Action ran. Once
your browser has shown you a notification for a specific item, it won't
notify you again for that same one.
