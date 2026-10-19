"""Download the map tiles for Rajasthan + Maharashtra once, so the dashboard map works without internet.

Run (needs internet):  .venv\\Scripts\\python -m eval.prefetch_tiles
Tiles go to data/tiles/ and the server then serves them from disk.
"""

import asyncio
import math

from app.main import fetch_tile

STATES = (68.0, 15.5, 81.0, 30.5)          # lon/lat box covering Rajasthan + Maharashtra
DISTRICTS = [(72.0, 25.8, 74.2, 27.6),      # Jodhpur talukas
             (73.4, 19.6, 74.9, 20.9)]      # Nashik talukas
PLAN = [("esri", range(4, 10), [STATES]), ("dark", range(4, 10), [STATES]), ("night", range(4, 9), [STATES]),
        ("esri", range(10, 12), DISTRICTS), ("dark", range(10, 12), DISTRICTS)]


def tiles(box, z):
    w, s, e, n = box
    def xy(lon, lat):
        k = 2 ** z
        return int((lon + 180) / 360 * k), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * k)
    x0, y0 = xy(w, n)
    x1, y1 = xy(e, s)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


async def main():
    jobs = sorted({(src, *t) for src, zooms, boxes in PLAN for z in zooms for box in boxes for t in tiles(box, z)})
    sem = asyncio.Semaphore(12)
    done = failed = 0

    async def one(src, z, x, y):
        nonlocal done, failed
        async with sem:
            ok = await fetch_tile(src, z, x, y)
        done += 1
        failed += ok is None
        if done % 200 == 0:
            print(f"{done}/{len(jobs)} tiles ({failed} failed)")

    await asyncio.gather(*(one(*j) for j in jobs))
    print(f"Done: {done - failed} tiles saved, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
