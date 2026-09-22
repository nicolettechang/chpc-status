# Lengau access-status monitor

Logs Lengau's access status from <https://users.chpc.ac.za/reports/> with real
timestamps, so you can answer "when exactly was it down, and for how long".

## What the page actually gives you

The status window is a row of equal-width coloured `<div>`s with no timestamps in
the markup, labelled `4h ago` on the left and `now` on the right:

| colour    | meaning            |
|-----------|--------------------|
| `#52d273` | reachable          |
| `#dc3545` | service disruption |

There is no JSON API behind it — the blocks are rendered straight into the HTML by
Django, and `/reports/status`, `/reports/api/status`, `/reports/heartbeat` and
friends all return 404. Scraping the markup is the only route.

### The "48 blocks ÷ 4 hours = 5 minutes each" reading is wrong

That is the natural assumption, and it does not survive measurement. Sampling the
live bar and cross-correlating consecutive reads to see how far the pattern
scrolled left (all fits were 100% exact, so the shifts are not in doubt):

| interval (UTC)      | elapsed | blocks advanced | implied s/block |
|---------------------|---------|-----------------|-----------------|
| 19:21:10 → 19:27:29 | 379 s   | 1               | 379             |
| 19:27:29 → 19:33:31 | 362 s   | 3               | 121             |
| 19:33:31 → 19:36:14 | 163 s   | 1               | 163             |
| **19:21:10 → 19:36:14** | **904 s** | **5**       | **181**         |

Two conclusions:

1. The mean rate is ~3 min per block, so 48 blocks span roughly **2.4 hours, not
   the 4 hours the label claims**.
2. Far more importantly, the spacing is *not uniform* — 379 s for one block, then
   121 s each for three. The bar behaves like "the last 48 checks" with the checks
   running at an irregular cadence, not like a fixed time axis.

So you cannot put reliable absolute time periods on the individual strokes. Any
tool that claims to is quietly making the 5-minute assumption and will be wrong by
tens of minutes at the left-hand end of the bar.

You can re-check this yourself at any time:

```bash
python3 lengau_status.py --calibrate
```

It samples the bar repeatedly, reports seconds-per-block per interval and over the
long baseline, and tells you when the measurement is meaningless (a bar of one flat
colour matches itself at every shift, so nothing can be inferred).

## What this script does instead

It keeps two records and never conflates them.

**`data/lengau_observations.csv` — authoritative.** One row per poll: this
machine's clock time and the status the page reported at that instant. Its
resolution is the polling interval and nothing else. This is what the report and
the dashboard are built from.

**`data/lengau_buckets.csv` — approximate, gap-filling only.** The bar decoded onto
its nominal axis (`window label ÷ block count`). Written *only* when the exact log
has a real hole in it — the Mac was asleep, say — so that you have a rough idea of
what happened rather than nothing. Every row carries `quality =
approximate-nominal-axis` and the block width it assumed, so it can never be
mistaken for a measurement.

Because polling is the resolution, the agent runs **every 10 minutes** by default
rather than every few hours. One poll is a single 18 KB GET, so this is about 2.5 MB
a day — cheaper than loading the page once in a browser with its CSS and fonts.

## Layout

```
chpc-status/
├── lengau_status.py                poller, reporter, calibrator (stdlib only)
├── index.html                      dashboard; loads data/ automatically on Pages
├── .github/workflows/poll.yml      GitHub Actions poller (the one that runs)
├── install.sh                      optional local LaunchAgent, see below
└── data/
    ├── lengau_observations.csv     one row per poll        <- the real record
    ├── lengau_buckets.csv          bar-derived, approximate, gap-fill only
    ├── agent.out.log / .err.log    launchd output (local agent only, untracked)
    └── raw/                        gzipped page snapshot per poll, 30-day prune
                                    (local agent only, untracked)
```

`lengau_observations.csv`: `observed_at_utc`, `observed_at_sast`, `status`
(`up` / `down` / `unreachable` / `unknown`), `headline`, `n_blocks`,
`window_label_hours`, `n_up`, `n_down`, `bar` (the 48-block pattern as `G`/`R`
characters, kept for later re-analysis), `note`.

## Where it runs: GitHub Actions

The poller runs on GitHub's infrastructure, not on a laptop. A laptop sleeps,
logs out, and joins networks from which the portal is filtered (from the CSIR
LAN, `users.chpc.ac.za` times out while the rest of the web loads) - every one
of those showed up as a hole in the record. A hosted runner has none of those
problems and always sees the portal from the same place.

`.github/workflows/poll.yml` runs `lengau_status.py` every 10 minutes and
commits `data/lengau_observations.csv` (and `lengau_buckets.csv` if a gap was
backfilled) straight back to `main`. The repo *is* the record; `git log data/`
is the audit trail.

It does **not** rely on GitHub's cron for the cadence. In practice a `*/10`
schedule fired late by an hour or not at all, so instead one job loops
poll → commit → sleep for 5.5 hours (the 6-hour job limit), then dispatches
its successor. `watchdog.yml` checks hourly that the newest row is less than
30 minutes old and restarts the chain if not - it watches the record rather
than the workflow's status, because a run that is alive but not committing is
just as broken as one that has died.

Each iteration resets to `origin/main` before polling, and rows whose push
loses a race are carried forward and merged back in by `tools/merge_rows.py`.
The first version rebased instead, and a failed rebase left the run polling
for its full 5.5 hours with every commit stuck locally before dying with the
lot - which is why **19 and 20 September each have a 5 h 40 min hole** in the
record. Nothing was silently filled in: the report and dashboard show those
as `no data`.

The dashboard is served by GitHub Pages from the same branch, so it reads the
CSV next to it and refreshes every 5 minutes:
<https://nicolettechang.github.io/chpc-status/>

Things to know:

- **If polling stops**, Actions → poll → *Run workflow* restarts the chain by
  hand. The cron backstop should do this on its own within an hour or so.
- **The dashboard can lag a few minutes** behind the repo: GitHub Pages
  rebuilds after each commit (~30 s) and its CDN caches the CSV for up to
  10 min.
- **Scheduled workflows are disabled after 60 days without repository
  activity.** The poller's own commits count as activity, so this should not
  trigger, but if polling stops, check Actions → poll for a "workflow disabled"
  banner and re-enable it.
- **Do not also run the local LaunchAgent** against a clone of this repo: both
  would append to the same CSV and the next `git pull` would conflict. The
  LaunchAgent is kept only as a fallback for running somewhere without GitHub.
- **Manual poll:** Actions → poll → *Run workflow*.

### One-time setup on a fresh fork

1. Settings → Actions → General → *Workflow permissions* → **Read and write**.
2. Settings → Pages → *Deploy from a branch* → `main` / `/ (root)`.
3. Actions → poll → *Run workflow* once to confirm it commits.

## Use

```bash
python3 lengau_status.py --report               # up/down periods from data/
python3 lengau_status.py --report --days 7
python3 lengau_status.py                        # poll once by hand (appends to data/)
python3 lengau_status.py --calibrate            # re-measure block cadence
open index.html                                 # dashboard offline: use the file picker
```

Optional local LaunchAgent, for a machine that is always on and *not* a clone
being pushed to GitHub:

```bash
bash install.sh                                 # polls every 10 min
INTERVAL=300 bash install.sh                    # every 5 min
bash install.sh --uninstall
```

In pandas:

```python
import pandas as pd
df = pd.read_csv("~/src/chpc-status/data/lengau_observations.csv",
                 parse_dates=["observed_at_utc"])
df.set_index("observed_at_utc")["status"].eq("up").resample("1D").mean()  # daily uptime
```

## Honest failure modes

- **This machine can't reach the portal** → logged as `status=unreachable`, never as
  `down`. An unreachable portal is not evidence about Lengau.
- **Mac asleep** → a visible `no data` period in the report, not a silent assumption
  that nothing changed. `--report` refuses to bridge gaps longer than 3× the polling
  interval.
- **CHPC changes the page** → `note=parse failed`, and the gzipped snapshot in
  `data/raw/` shows exactly what changed.
- **A new colour appears** (an amber "degraded" state, say) → recorded as
  `status=unknown` with the hex kept, rather than being forced into up or down.
- **Status flip between polls** → the report attributes the change to the midpoint of
  the two polls and says so; edges are good to about half the polling interval.
