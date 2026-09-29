#!/usr/bin/env python3
"""SoiKeno collector: keeps data/keno.jsonl up to date with Vietlott Keno draws.

Output format is identical to vietvudanh/vietlott-data (one JSON object per line,
ascending by id):
  {"date":"2026-09-28","id":"#0297327","result":[...20 numbers...],
   "big_small":"Chẵn (12)","odd_even":"Hòa"}

Sources, in order:
  1. ketquaday.vn  - list of latest ~50 draws + one page per draw (backfill)
  2. baomoi.com    - latest 18 draws (__NEXT_DATA__ JSON)
  3. vietvudanh/vietlott-data - public archive, merged when it has draws we lack

Usage:
  collect.py once            # one pass, exit
  collect.py loop MINUTES    # poll every ~75 s for MINUTES, commit+push on change
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "keno.jsonl")
LATEST = os.path.join(ROOT, "data", "keno-latest.jsonl")
STATUS = os.path.join(ROOT, "data", "status.json")
LATEST_N = 300
MAX_BACKFILL = 400  # max per-draw pages fetched in one pass
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36 SoiKenoCollector")
ICT = timezone(timedelta(hours=7))

KQD = "https://ketquaday.vn"
BAOMOI = "https://baomoi.com/tien-ich-ket-qua-vietlott-keno.epi"
ARCHIVE = "https://raw.githubusercontent.com/vietvudanh/vietlott-data/master/data/keno.jsonl"


def log(*a):
    print(datetime.now(ICT).strftime("%H:%M:%S"), *a, flush=True)


def get(url, timeout=25, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "vi,en;q=0.8"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- helpers
def make_draw(num, date_iso, nums):
    nums = sorted(int(x) for x in nums)
    if len(nums) != 20 or len(set(nums)) != 20 or not all(1 <= n <= 80 for n in nums):
        return None
    even = sum(1 for n in nums if n % 2 == 0)
    big = sum(1 for n in nums if n >= 41)
    # same (swapped) labels as vietvudanh: big_small holds even/odd, odd_even holds big/small
    bs = "Hòa" if even == 10 else (f"Chẵn ({even})" if even > 10 else f"Lẻ ({20 - even})")
    oe = "Hòa" if big == 10 else (f"Lớn ({big})" if big > 10 else f"Nhỏ ({20 - big})")
    return {"date": date_iso, "id": f"#{int(num):07d}", "result": nums,
            "big_small": bs, "odd_even": oe}


def dmy_to_iso(s):
    d, m, y = (int(x) for x in re.split(r"[/-]", s.strip())[:3])
    return f"{y:04d}-{m:02d}-{d:02d}"


def num_of(draw):
    return int(draw["id"].lstrip("#"))


# ---------------------------------------------------------------- ketquaday
_KQD_ID = re.compile(r"#(\d{6,7})(?:</a>)?\s*</strong>")
_KQD_DATE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s+\d{1,2}:\d{2}")
_KQD_NUM = re.compile(r'class="btn-number(?:-live)?"[^>]*>\s*(\d{1,2})\s*<')


def parse_ketquaday(html):
    out = []
    marks = list(_KQD_ID.finditer(html))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        chunk = html[m.end():end]
        dm = _KQD_DATE.search(chunk)
        nums = _KQD_NUM.findall(chunk)[:20]
        if not dm or len(nums) < 20:
            continue
        d = make_draw(m.group(1), dmy_to_iso(dm.group(1)), nums)
        if d:
            out.append(d)
    return out


def src_ketquaday_latest():
    return parse_ketquaday(get(f"{KQD}/ket-qua-keno"))


def src_ketquaday_one(num):
    ds = [d for d in parse_ketquaday(get(f"{KQD}/ket-qua-keno-ky-{num}")) if num_of(d) == num]
    return ds[0] if ds else None


# ---------------------------------------------------------------- baomoi
def src_baomoi():
    html = get(BAOMOI)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    nd = json.loads(m.group(1))
    items = nd["props"]["pageProps"]["resp"]["data"]["content"]["items"]
    out = []
    for it in items:
        try:
            nums = it["awards"][0]["value"].split("-")
            d = make_draw(it["draw"], dmy_to_iso(it["date"]), nums)
            if d:
                out.append(d)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- archive
def src_archive_tail(after_num):
    # read the last ~200 KB of the public archive
    txt = get(ARCHIVE, headers={"Range": "bytes=-200000"}, timeout=40)
    out = []
    for line in txt.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if num_of(d) > after_num:
            dd = make_draw(num_of(d), d["date"], d["result"])
            if dd:
                out.append(dd)
    return out


# ---------------------------------------------------------------- store
def load():
    draws = {}
    if os.path.exists(DATA):
        with open(DATA, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    draws[num_of(d)] = d
    return draws


def dump_line(d):
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def save(draws, sources):
    keys = sorted(draws)
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA + ".tmp", "w", encoding="utf-8") as f:
        for k in keys:
            f.write(dump_line(draws[k]) + "\n")
    os.replace(DATA + ".tmp", DATA)
    with open(LATEST + ".tmp", "w", encoding="utf-8") as f:
        for k in keys[-LATEST_N:]:
            f.write(dump_line(draws[k]) + "\n")
    os.replace(LATEST + ".tmp", LATEST)
    last = draws[keys[-1]]
    missing = [n for n in range(keys[-LATEST_N], keys[-1]) if n not in draws]
    status = {"last_id": last["id"], "last_date": last["date"], "count": len(keys),
              "missing_in_latest": len(missing),
              "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "sources": sources}
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
        f.write("\n")


def one_pass(draws, deep=True):
    """Returns (number of new draws, list of sources that worked)."""
    before = len(draws)
    top_before = max(draws) if draws else 0
    fresh, ok = [], []
    for name, fn in (("ketquaday", src_ketquaday_latest), ("baomoi", src_baomoi)):
        try:
            got = fn()
            fresh += got
            ok.append(name)
            log(f"{name}: {len(got)} draws, newest #{max(map(num_of, got)) if got else '-'}")
        except Exception as e:
            log(f"{name}: FAIL {e!r}")
    if deep:
        try:
            got = src_archive_tail(top_before - 1000)
            fresh += got
            ok.append("archive")
            log(f"archive: newest #{max(map(num_of, got)) if got else '-'}")
        except Exception as e:
            log(f"archive: FAIL {e!r}")
    for d in fresh:
        n = num_of(d)
        if n not in draws:
            draws[n] = d
        elif draws[n]["result"] != d["result"]:
            log(f"WARN mismatch #{n}: keeping stored {draws[n]['result']} vs {d['result']}")
    # backfill holes between the old top and the new top, one page per draw
    if draws:
        top = max(draws)
        lo = max(min(draws), top - LATEST_N * 3)
        holes = [n for n in range(lo, top) if n not in draws][-MAX_BACKFILL:]
        if holes:
            log(f"backfill: {len(holes)} missing draws ({holes[0]}..{holes[-1]})")
        fails = 0
        for n in holes:
            try:
                d = src_ketquaday_one(n)
                if d:
                    draws[n] = d
                    fails = 0
                else:
                    fails += 1
            except Exception as e:
                fails += 1
                log(f"backfill #{n}: {e!r}")
            if fails >= 8:
                log("backfill: too many failures, stopping for now")
                break
            time.sleep(0.7)
        if holes:
            ok.append("ketquaday-backfill")
    return len(draws) - before, ok


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], check=True,
                          capture_output=True, text=True).stdout


def commit_push(msg):
    git("add", "data")
    if not git("status", "--porcelain", "data").strip():
        return False
    git("commit", "-q", "-m", msg)
    for _ in range(3):
        try:
            git("push", "-q")
            return True
        except subprocess.CalledProcessError:
            git("pull", "-q", "--rebase", "-X", "theirs")
    return False


def in_draw_hours():
    now = datetime.now(ICT)
    return 5 * 60 + 50 <= now.hour * 60 + now.minute <= 22 * 60 + 15


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    draws = load()
    if mode == "once":
        added, ok = one_pass(draws)
        if draws:
            save(draws, ok)
        log(f"added {added}; total {len(draws)}; last #{max(draws) if draws else '-'}")
        return
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 20
    deadline = time.time() + minutes * 60
    first = True
    while True:
        added, ok = one_pass(draws, deep=first)
        first = False
        if added or not os.path.exists(STATUS):
            save(draws, ok)
            last = max(draws)
            if commit_push(f"keno: +{added} (last #{last:07d})"):
                log(f"pushed +{added}, last #{last:07d}")
        if time.time() + 75 > deadline or not in_draw_hours():
            break
        time.sleep(75)


if __name__ == "__main__":
    main()
