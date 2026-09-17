#!/usr/bin/env python3
"""
Log CHPC Lengau access status from https://users.chpc.ac.za/reports/

The page renders recent Lengau access status as a row of equal-width coloured
blocks (a "heartbeat bar") labelled "4h ago" on the left and "now" on the right:

    <div style="flex:1; height:24px; background:#52d273; border-radius:4px;"></div>

Green (#52d273) = reachable, red (#dc3545) = service disruption. There are no
timestamps in the markup.

WHY THIS SCRIPT DOES NOT SIMPLY DIVIDE THE BAR BY TIME
------------------------------------------------------
The obvious reading is "48 blocks / 4 hours = 5 minutes per block". Measured
against the live page on 2026-09-14, that is wrong: sampling the bar repeatedly
and counting how far the pattern shifted left gave

    19:21:10 -> 19:27:29 UTC  (379 s)  bar advanced 1 block
    19:27:29 -> 19:33:31 UTC  (362 s)  bar advanced 3 blocks
    19:33:31 -> 19:36:14 UTC  (170 s)  bar advanced 1 block

i.e. a mean of ~3 min per block, not 5, and wildly uneven block-to-block. The
bar is best understood as "the last 48 checks", with the checks running at an
irregular cadence, and the "4h" label as nominal rather than exact.

So the bar cannot carry a trustworthy absolute time axis. This script therefore
keeps two separate records and never mixes them up:

  observations.csv  AUTHORITATIVE. One row per poll, with this machine's clock
                    time and the status the page reported at that instant. Its
                    resolution is the polling interval and nothing else.

  buckets.csv       APPROXIMATE. The bar decoded onto its nominal time axis
                    (window label / block count), used only to fill gaps when
                    this machine was asleep. Every row is flagged approximate
                    and carries the nominal block width it assumed.

Run --calibrate to re-measure the block cadence yourself; it is only meaningful
while the bar has some structure (mixed green and red), because a uniform bar
looks identical at every shift.

Usage:
    lengau_status.py                    # poll once, append to the log
    lengau_status.py --report           # up/down periods from the exact log
    lengau_status.py --report --days 7
    lengau_status.py --calibrate        # measure seconds-per-block (~10 min)
"""

import argparse
import csv
import gzip
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

URL = "https://users.chpc.ac.za/reports/"
SAST = ZoneInfo("Africa/Johannesburg")

COLOUR_STATUS = {"#52d273": "up", "#dc3545": "down"}

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OBS_CSV = os.path.join(DATA, "lengau_observations.csv")
BUCKETS_CSV = os.path.join(DATA, "lengau_buckets.csv")
RAW_DIR = os.path.join(DATA, "raw")
RAW_KEEP_DAYS = 30

OBS_FIELDS = [
    "observed_at_utc",
    "observed_at_sast",
    "status",
    "headline",
    "n_blocks",
    "window_label_hours",
    "n_up",
    "n_down",
    "bar",
    "note",
]
BUCKET_FIELDS = [
    "bucket_start_utc",
    "bucket_start_sast",
    "bucket_end_sast",
    "status",
    "colour",
    "observed_at_utc",
    "nominal_block_seconds",
    "quality",
]

BAR_RE = re.compile(
    r"flex:1;\s*height:24px;\s*background:\s*(#[0-9a-fA-F]{6})", re.IGNORECASE
)
BAR_RE_LOOSE = re.compile(
    r"height:24px;[^\"']*?background:\s*(#[0-9a-fA-F]{6})", re.IGNORECASE
)
WINDOW_RE = re.compile(r"<span>\s*(\d+)\s*h\s*ago\s*</span>", re.IGNORECASE)
HEADLINE_RE = re.compile(r"●\s*([^<]+?)\s*<")


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


# --------------------------------------------------------------------- fetch


def fetch(url=URL, timeout=45):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lengau-status-monitor/2.0 (personal availability logging)",
            "Accept": "text/html",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# --------------------------------------------------------------------- parse


def parse(html):
    colours = [c.lower() for c in BAR_RE.findall(html)] or [
        c.lower() for c in BAR_RE_LOOSE.findall(html)
    ]
    if not colours:
        raise ValueError("no heartbeat blocks found - page layout probably changed")

    m = WINDOW_RE.search(html)
    window_hours = int(m.group(1)) if m else 4
    hm = HEADLINE_RE.search(html)
    headline = hm.group(1).strip() if hm else ""

    statuses = [COLOUR_STATUS.get(c, "unknown") for c in colours]
    return {
        "colours": colours,
        "statuses": statuses,
        "window_hours": window_hours,
        "headline": headline,
        # The rightmost block is "now"; trust it over the headline wording,
        # but fall back to the headline if the bar ends on an unknown colour.
        "status": statuses[-1]
        if statuses[-1] != "unknown"
        else headline_status(headline),
        "n_up": statuses.count("up"),
        "n_down": statuses.count("down"),
        "bar": "".join(
            {"up": "G", "down": "R"}.get(s, "?") for s in statuses
        ),
    }


def headline_status(headline):
    h = headline.lower()
    if "disruption" in h or "down" in h or "outage" in h:
        return "down"
    if "operational" in h or "available" in h or "up" in h:
        return "up"
    return "unknown"


# ------------------------------------------------------------------- storage


def append_row(path, fields, row):
    os.makedirs(DATA, exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def save_buckets(store):
    os.makedirs(DATA, exist_ok=True)
    tmp = BUCKETS_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=BUCKET_FIELDS)
        w.writeheader()
        for key in sorted(store):
            w.writerow({k: store[key].get(k, "") for k in BUCKET_FIELDS})
    os.replace(tmp, BUCKETS_CSV)


def archive_raw(html, when):
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, f"reports-{when.strftime('%Y%m%dT%H%M%SZ')}.html.gz")
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(html)
    cutoff = when - timedelta(days=RAW_KEEP_DAYS)
    for name in os.listdir(RAW_DIR):
        full = os.path.join(RAW_DIR, name)
        try:
            if datetime.fromtimestamp(os.path.getmtime(full), tz=timezone.utc) < cutoff:
                os.remove(full)
        except OSError:
            pass


def last_observation_time():
    rows = load_csv(OBS_CSV)
    for row in reversed(rows):
        if row.get("status") in ("up", "down", "unknown"):
            try:
                return datetime.fromisoformat(row["observed_at_utc"])
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------- poll


def poll(gap_factor=2.5, interval=600):
    now = datetime.now(timezone.utc)
    try:
        html = fetch()
    except (urllib.error.URLError, OSError) as exc:
        # This machine could not reach the portal. That says nothing about
        # Lengau, so it is logged as 'unreachable', never as 'down'.
        append_row(
            OBS_CSV,
            OBS_FIELDS,
            {
                "observed_at_utc": iso(now),
                "observed_at_sast": iso(now.astimezone(SAST)),
                "status": "unreachable",
                "note": f"fetch failed: {exc}",
            },
        )
        print(f"[{iso(now.astimezone(SAST))}] portal unreachable: {exc}", file=sys.stderr)
        return 1

    archive_raw(html, now)
    try:
        p = parse(html)
    except ValueError as exc:
        append_row(
            OBS_CSV,
            OBS_FIELDS,
            {
                "observed_at_utc": iso(now),
                "observed_at_sast": iso(now.astimezone(SAST)),
                "status": "unknown",
                "note": f"parse failed: {exc}",
            },
        )
        print(f"[{iso(now.astimezone(SAST))}] parse failed: {exc}", file=sys.stderr)
        return 2

    prev = last_observation_time()
    append_row(
        OBS_CSV,
        OBS_FIELDS,
        {
            "observed_at_utc": iso(now),
            "observed_at_sast": iso(now.astimezone(SAST)),
            "status": p["status"],
            "headline": p["headline"],
            "n_blocks": len(p["statuses"]),
            "window_label_hours": p["window_hours"],
            "n_up": p["n_up"],
            "n_down": p["n_down"],
            "bar": p["bar"],
            "note": "",
        },
    )

    # Backfill from the bar only when the exact log has a real hole in it,
    # e.g. the Mac was asleep. Otherwise the bar adds nothing but noise.
    added = 0
    gap = (now - prev).total_seconds() if prev else None
    if gap is None or gap > gap_factor * interval:
        added = backfill(p, now)

    print(
        f"[{iso(now.astimezone(SAST))}] {p['status'].upper()}"
        f" ({p['headline'] or 'no headline'})"
        f" | bar {p['n_up']}/{len(p['statuses'])} green"
        + (f" | backfilled {added} approximate buckets" if added else "")
    )
    return 0


def backfill(p, now):
    n = len(p["statuses"])
    block = p["window_hours"] * 3600.0 / n
    epoch = now.timestamp()
    anchor = datetime.fromtimestamp(epoch - (epoch % block), tz=timezone.utc)
    start0 = anchor - timedelta(hours=p["window_hours"])

    store = {r["bucket_start_utc"]: r for r in load_csv(BUCKETS_CSV)}
    added = 0
    for i, status in enumerate(p["statuses"]):
        start = start0 + timedelta(seconds=i * block)
        end = start + timedelta(seconds=block)
        key = iso(start)
        if key not in store:
            added += 1
        store[key] = {
            "bucket_start_utc": key,
            "bucket_start_sast": iso(start.astimezone(SAST)),
            "bucket_end_sast": iso(end.astimezone(SAST)),
            "status": status,
            "colour": p["colours"][i],
            "observed_at_utc": iso(now),
            "nominal_block_seconds": int(block),
            "quality": "approximate-nominal-axis",
        }
    save_buckets(store)
    return added


# ------------------------------------------------------------------- report


def spans_from(rows, gap_seconds):
    """Contiguous up/down periods from timestamped observations.

    A status change is only known to have happened somewhere between two polls,
    so it is attributed to the midpoint. A gap longer than `gap_seconds` is not
    bridged - it becomes an explicit 'no data' period rather than a silent
    assumption that nothing changed while the machine was asleep.
    """
    spans = []
    prev_t = None
    for r in rows:
        t = datetime.fromisoformat(r["observed_at_utc"])
        st = r["status"]
        if prev_t is not None and (t - prev_t).total_seconds() > gap_seconds:
            spans[-1]["end"] = prev_t
            spans.append({"status": "no data", "start": prev_t, "end": t})
            spans.append({"status": st, "start": t, "end": t})
        elif spans and spans[-1]["status"] == st:
            spans[-1]["end"] = t
        elif spans:
            mid = spans[-1]["end"] + (t - spans[-1]["end"]) / 2
            spans[-1]["end"] = mid
            spans.append({"status": st, "start": mid, "end": t})
        else:
            spans.append({"status": st, "start": t, "end": t})
        prev_t = t
    return spans


def report(days, gap_seconds):
    rows = [r for r in load_csv(OBS_CSV) if r.get("status") in ("up", "down")]
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = [r for r in rows if datetime.fromisoformat(r["observed_at_utc"]) >= cutoff]
    if len(rows) < 2:
        print("not enough observations yet - the poller needs to run a few times")
        return 1

    spans = spans_from(rows, gap_seconds)
    known = [s for s in spans if s["status"] in ("up", "down")]
    total = sum((s["end"] - s["start"]).total_seconds() for s in known)
    up = sum((s["end"] - s["start"]).total_seconds() for s in known if s["status"] == "up")

    print(
        f"Lengau access, {iso(spans[0]['start'].astimezone(SAST))} to "
        f"{iso(spans[-1]['end'].astimezone(SAST))} (SAST)"
    )
    print(
        f"{len(rows)} observations, {total / 3600:.1f} h covered, "
        f"availability {100 * up / total:.1f}%\n"
        if total
        else ""
    )
    print(f"{'status':10}{'from (SAST)':28}{'to (SAST)':28}duration")
    for s in spans:
        h, m = divmod(int((s["end"] - s["start"]).total_seconds() // 60), 60)
        print(
            f"{s['status']:10}{iso(s['start'].astimezone(SAST)):28}"
            f"{iso(s['end'].astimezone(SAST)):28}{h:>4}h {m:02d}m"
        )
    print("\nPeriod edges are accurate to about half the polling interval.")
    return 0


# ---------------------------------------------------------------- calibrate


def best_shift(old, new):
    """How far did the bar scroll left between `old` and `new`?

    Returns (shift, score, unique). `unique` is False when several shifts fit
    equally well, which is what happens on a bar of one flat colour.
    """
    n = len(old)
    # Only consider shifts that still leave at least half the bar overlapping:
    # a one-block overlap matches perfectly by accident and would win every time.
    scored = []
    for k in range(0, n // 2 + 1):
        a, b = old[k:], new[: n - k]
        scored.append((sum(x == y for x, y in zip(a, b)) / len(a), k))
    scored.sort(key=lambda t: (-t[0], t[1]))
    top = scored[0]
    unique = len(scored) < 2 or scored[1][0] < top[0]
    return top[1], top[0], unique


def calibrate(samples=6, spacing=100):
    print(
        f"sampling the bar {samples} times, {spacing}s apart "
        f"(~{samples * spacing / 60:.0f} min)\n"
    )
    obs = []
    for i in range(samples):
        t = datetime.now(timezone.utc)
        try:
            p = parse(fetch())
        except Exception as exc:  # noqa: BLE001 - diagnostic tool, report anything
            print(f"  sample {i + 1}: failed ({exc})")
            continue
        obs.append((t, p["statuses"]))
        print(f"  sample {i + 1}: {iso(t)}  {p['bar']}")
        if i < samples - 1:
            time.sleep(spacing)

    if len(obs) < 2:
        print("\nnot enough samples")
        return 1
    if len(set(obs[0][1])) < 2:
        print(
            "\nThe bar is a single flat colour, so every shift fits equally well "
            "and the cadence cannot be measured right now. Try again while the "
            "status is flapping."
        )
        return 1

    print(f"\n{'interval':>10}{'elapsed s':>12}{'blocks':>9}{'s/block':>10}  unique")
    rates = []
    for (t0, s0), (t1, s1) in zip(obs, obs[1:]):
        dt = (t1 - t0).total_seconds()
        k, score, unique = best_shift(s0, s1)
        rate = dt / k if k else float("inf")
        if k and unique:
            rates.append(rate)
        print(
            f"{iso(t0)[11:19]:>10}{dt:>12.0f}{k:>9}"
            f"{(f'{rate:.0f}' if k else '-'):>10}  {unique} (fit {score:.0%})"
        )
    if rates:
        total_dt = (obs[-1][0] - obs[0][0]).total_seconds()
        k_total, _, _ = best_shift(obs[0][1], obs[-1][1])
        print(
            f"\nlong baseline: {k_total} blocks over {total_dt:.0f}s "
            f"= {total_dt / k_total:.0f} s/block"
            if k_total
            else "\nlong baseline: bar did not move"
        )
        if k_total:
            span = len(obs[0][1]) * total_dt / k_total
            print(
                f"implied bar span: {len(obs[0][1])} blocks x "
                f"{total_dt / k_total:.0f}s = {span / 3600:.1f} h "
                f"(the page label claims 4 h)"
            )
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true", help="print up/down periods")
    ap.add_argument("--days", type=float, default=0, help="limit report to last N days")
    ap.add_argument("--calibrate", action="store_true", help="measure seconds per block")
    ap.add_argument("--samples", type=int, default=6, help="calibration samples")
    ap.add_argument("--spacing", type=int, default=100, help="calibration spacing (s)")
    ap.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("LENGAU_INTERVAL", 600)),
        help="the scheduled polling interval, used to decide what counts as a gap",
    )
    args = ap.parse_args()
    if args.calibrate:
        return calibrate(args.samples, args.spacing)
    if args.report:
        return report(args.days, gap_seconds=3 * args.interval)
    return poll(interval=args.interval)


if __name__ == "__main__":
    sys.exit(main())
