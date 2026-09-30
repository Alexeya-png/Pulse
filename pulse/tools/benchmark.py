"""Reproducible synthetic benchmark. No Instagram account or network needed."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pulse.model import Member, Sample, Snapshot
from pulse.store import Store

parser = argparse.ArgumentParser()
parser.add_argument("--members", type=int, default=100_000)
args = parser.parse_args()
if args.members < 100:
    parser.error("--members must be at least 100")
with tempfile.TemporaryDirectory() as directory:
    store = Store(Path(directory) / "benchmark.sqlite3")
    start = time.perf_counter()
    people = tuple(Member(f"user_{i}", str(i)) for i in range(args.members))
    following = people[:7000] + tuple(Member(f"outside_{i}", str(args.members + i)) for i in range(500))
    store.ingest(Snapshot("benchmark", "2026-09-01T00:00:00Z", (Sample("followers", "", people, "id"), Sample("following", "", following, "id"))))
    baseline = time.perf_counter() - start
    start = time.perf_counter()
    result = store.ingest(Snapshot("benchmark", "2026-09-02T00:00:00Z", (Sample("followers", "", people[100:] + (Member("new_user", str(args.members + 501)),), "id"), Sample("following", "", following, "id"))))
    compare = time.perf_counter() - start
    start = time.perf_counter()
    page = store.events("benchmark", "followers", "removed", limit=80)
    page_ms = (time.perf_counter() - start) * 1000
    assert (result.removed, result.added, len(page)) == (100, 1, 80)
    start = time.perf_counter()
    reciprocal = store.nonreciprocal("benchmark", limit=80)
    reciprocal_ms = (time.perf_counter() - start) * 1000
    assert reciprocal["total"] == 600 and len(reciprocal["users"]) == 80
    print(json.dumps({"followers": args.members, "following": len(following), "baseline_seconds": round(baseline, 3), "comparison_seconds": round(compare, 3), "first_history_page_ms": round(page_ms, 2), "nonreciprocal_count_and_page_ms": round(reciprocal_ms, 2), "database_MiB": round(store.path.stat().st_size / 1024**2, 2), "platform": sys.platform, "python": sys.version.split()[0]}, indent=2))
