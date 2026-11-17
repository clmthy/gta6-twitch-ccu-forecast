"""GTA VI launch-day Twitch viewers forecast (gta6-twitch-ccu-forecast-01).

Reads one case file per run (MAKEFUN_INPUT_DIR, default /input) and writes one
forecast per case to MAKEFUN_OUTPUT_DIR (default /output):
  {"hours": [{"hour": 2, "q": [q05, q25, q50, q75, q95]}, ..., {"hour": 24, ...}]}

Method (see NOTES.md for the reasoning and the leave-one-out evidence):
  A big game's launch-day shape is strongly patterned in UTC time: the
  category is already live in Asia-Pacific/Europe at 00:00 UTC, dips
  overnight, then climbs to a peak in the late UTC day (US daytime + Europe
  evening). We model each hour's viewers as a multiple of hour 1's print and
  learn the distribution of that multiple (in log space) from the case's
  comparables (other games' launch days).

  Each comparable is weighted by similarity to this case on two axes:
    1. scale: how close its own hour-1 print is (Gaussian kernel on log
       viewers, bandwidth 1.0) -- a mega-launch behaves like other
       mega-launches, not like a small category that spiked 200x from a
       near-zero hour 1;
    2. launch state: how large hour 1 is against the category's own
       pre-release baseline (Gaussian kernel on the log spike, bandwidth 1.0)
       -- this separates launches that were already live at 00:00 UTC
       (rolling midnight unlocks) from ones still waiting for a single global
       unlock later in the day.
  Per hour we take the weighted mean and standard deviation of the log-ratios
  and emit normal quantiles at the five scored levels. The pinball loss is a
  proper scoring rule, so honest quantiles are optimal.

  Fully deterministic: fixed sort orders, no randomness, no wall clock, no
  network. Standard library only. Runs in well under a second per case.
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.6448536269514722, -0.6744897501960817, 0.0,
     0.6744897501960817, 1.6448536269514722]

# Gaussian kernel bandwidths (log units), tuned by leave-one-out on the kit's
# 37 comparables: full-set mean pinball 0.372 vs 0.930 for the house baseline.
BANDWIDTH_SCALE = 1.0
BANDWIDTH_SPIKE = 1.0
# A comparable must carry at least this weight to enter a hour's distribution;
# if fewer than 5 do, the 5 heaviest are kept, so the spread never collapses.
MIN_WEIGHT = 0.02
MIN_COMPARABLES = 5


def parse_ts(t):
    """ISO-8601 UTC timestamp -> aware datetime."""
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def round2(x):
    """x rounded to 2 decimals, half up (as JavaScript's Math.round(x*100)/100)."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def pre_baseline(points, day_start, days=3):
    """Mean viewers over the `days` days before the release (0.0 if none)."""
    vals = [p["viewers"] for p in points or []
            if p.get("viewers") and p["viewers"] > 0
            and day_start - timedelta(days=days) <= parse_ts(p["at"] if "at" in p else p["from"]) < day_start]
    return sum(vals) / len(vals) if vals else 0.0


def comparable_curves(case):
    """Each comparable as (hour1_viewers, {hour: viewers}, pre_mean).

    Hour 1 is the mean of the points in [day_start, day_start + 1h); the
    launch-day curve is the mean per hour for hours 2..24. A comparable with
    no hour-1 point (e.g. the GTA VI pre-release entry, which is not a
    launch) is skipped.
    """
    out = []
    for c in case.get("comparables") or []:
        ds = parse_ts(c["day_start"])
        hours = {}
        for p in c.get("hourly") or []:
            v = p.get("viewers")
            if v is None or v <= 0:
                continue
            h = int((parse_ts(p["from"]) - ds).total_seconds() // 3600) + 1
            if 1 <= h <= 24:
                hours.setdefault(h, []).append(v)
        if 1 not in hours:
            continue
        curve = {h: sum(v) / len(v) for h, v in hours.items()}
        pre = [p["viewers"] for p in c.get("hourly") or []
               if p.get("viewers") and p["viewers"] > 0
               and ds - timedelta(days=3) <= parse_ts(p["from"]) < ds]
        pre_mean = sum(pre) / len(pre) if pre else 0.0
        out.append((curve[1], curve, pre_mean))
    return out


def forecast_case(case):
    if case["levels"] != LEVELS:
        raise ValueError("levels are not %s" % (LEVELS,))
    first = next((k for k in case["known"] if k["hour"] == 1), None)
    if first is None:
        raise ValueError("no hour-1 print")
    p1 = max(1.0, float(first["viewers"]))
    log_p1 = math.log(p1)

    ds = parse_ts(case["day_start"])
    target_pre = pre_baseline(case.get("history") or [], ds)
    spike_t = math.log(p1 / target_pre) if target_pre > 0 else None

    comps = comparable_curves(case)
    if not comps:
        raise ValueError("no usable comparables")

    scored = []
    for c_p1, curve, pre_mean in comps:
        d = math.log(max(1.0, c_p1)) - log_p1
        w = math.exp(-0.5 * (d / BANDWIDTH_SCALE) ** 2)
        if spike_t is not None and pre_mean > 0:
            spike_c = math.log(max(1.0, c_p1) / pre_mean)
            d2 = spike_c - spike_t
            w *= math.exp(-0.5 * (d2 / BANDWIDTH_SPIKE) ** 2)
        scored.append((w, curve))
    scored.sort(key=lambda t: -t[0])
    weighted = [(w, cv) for w, cv in scored if w >= MIN_WEIGHT]
    if len(weighted) < MIN_COMPARABLES:
        weighted = scored[:MIN_COMPARABLES]

    rows = []
    for row in case["forecast"]:
        h = row["hour"]
        pairs = [(math.log(cv[h] / cv[1]), w) for w, cv in weighted
                 if h in cv and 1 in cv]
        if not pairs:
            pairs = [(0.0, 1.0)]
        tot = sum(w for _, w in pairs)
        mu = sum(v * w for v, w in pairs) / tot
        var = sum(w * (v - mu) ** 2 for v, w in pairs) / tot
        sd = math.sqrt(max(var, 1e-9))
        qs = [round2(max(1.0, p1 * math.exp(mu + z * sd))) for z in Z]
        for i in range(1, 5):
            if qs[i] < qs[i - 1]:
                qs[i] = qs[i - 1]
        rows.append({"hour": h, "q": qs})
    rows.sort(key=lambda r: r["hour"])
    return {"hours": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
