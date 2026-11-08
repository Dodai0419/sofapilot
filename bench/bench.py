"""Time the list work, best of 5. Run: python bench/bench.py"""
import pathlib, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sofapilot as s


def best(fn, n=5):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return min(out) * 1000


data = s.load_lists()
tv = {"manufacturer": "Nope", "model": "ZZ9000", "device": "y"}
print(f"bundle: {(ROOT / 'data' / 'tvlists.json.gz').stat().st_size / 1024:.0f} KB, {len(data['devices'])} lists")
print(f"load all lists: {best(s.load_lists):.0f} ms")
print(f"match a TV (worst case, no match): {best(lambda: s.match_list(data, tv)):.1f} ms")
