#!/usr/bin/env python3
"""Union an append-only CSV with rows that an earlier push could not land.

Both files are the poller's own output, keyed on their first column (an ISO
UTC timestamp, never quoted, so a plain split is safe). Rows already present
win, the rest are added, and the result is sorted back into time order.

This exists so a poll whose push loses a race is never thrown away and never
force-pushed over the top of someone else's rows.

Line endings are preserved byte for byte: csv.DictWriter writes CRLF, and
rewriting the record as LF would show up as all 900-odd lines changing in
every commit.
"""
import sys

EOL = "\r\n"


def read(path):
    try:
        # newline="" keeps the file's own terminators out of the way of
        # universal-newline translation.
        with open(path, newline="", encoding="utf-8") as fh:
            return [line.rstrip("\r\n") for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def main(target, extra):
    base, add = read(target), read(extra)
    if not base and not add:
        return 0
    header = base[0] if base else add[0]
    seen, out = set(), []
    for line in base[1:] + add[1:]:
        key = line.split(",", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    out.sort(key=lambda line: line.split(",", 1)[0])
    with open(target, "w", newline="", encoding="utf-8") as fh:
        fh.write(EOL.join([header] + out) + EOL)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
