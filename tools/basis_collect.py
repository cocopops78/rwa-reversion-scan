#!/usr/bin/env python3
"""basis_collect.py — echantillonneur de basis inter-instruments correles (meme venue).

Deux instruments proches cotes sur la MEME venue : on enregistre leur ecart (basis, bps)
dans le temps pour voir s'il OSCILLE (reversion, exploitable) ou reste un discount
PERSISTANT (non exploitable). Sortie CSV au schema de reversion_analyze.py.

Sources KEYLESS (market-data publique, aucun secret) :
  * /api/v1/info/markets            (marketStats.markPrice, bid/ask)
  * /metadata/stats -> listings[]   (mark_price, base_spread_bps, volume_24h)

Paires par defaut :
  * XAUT vs XAU   (venue A)
  * PAXG vs XAU   (venue B)

CSV : iso_time,epoch,base,hedge,symbol,base_mid,hedge_mid,basis_bps,gross_bps,crossing_bps,spread,vol
  base=hedge=venue ; symbol=<long>-<short> ; base_mid=long mark ; hedge_mid=short mark ;
  basis_bps=(long-short)/short*1e4 (signe) ; gross_bps=|basis| ; spread/vol = jambe la plus
  mince (juger mark perime vs reel). Colonnes surnumeraires ignorees par reversion_analyze.

Usage :
  python tools/basis_collect.py --minutes 330 --interval 60 --csv part.csv --checkpoint-min 30
"""
import sys, os, csv, time, json, argparse, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0"}
SRC_A = "https://api.starknet.extended.exchange/api/v1/info/markets"
SRC_B = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"

# (venue_tag, long_ticker, short_ticker) — venue_tag doit matcher TAKER_BPS de reversion_analyze
PAIRS = [
    ("variational", "XAUT", "XAU"),
    ("extended", "PAXG-USD", "XAU-USD"),
]


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _marks():
    """{venue: {ticker: (mark, spread_bps, vol)}} — keyless, tolerant aux pannes reseau."""
    out = {"extended": {}, "variational": {}}
    try:
        for m in _get(SRC_A).get("data", []):
            ms = m.get("marketStats", {})
            mk = ms.get("markPrice") or ms.get("lastPrice")
            if not mk:
                continue
            mk = float(mk)
            bid = float(ms.get("bidPrice", 0) or 0)
            ask = float(ms.get("askPrice", 0) or 0)
            spr = ((ask - bid) / mk * 1e4) if (bid > 0 and ask > 0 and mk > 0) else 0.0
            vol = float(ms.get("dailyVolume", 0) or 0)
            out["extended"][m["name"]] = (mk, spr, vol)
    except Exception as e:
        print(f"[basis] source A KO: {type(e).__name__}: {e}", flush=True)
    try:
        for it in _get(SRC_B).get("listings", []):
            tk = str(it.get("ticker", "")).upper()
            mk = it.get("mark_price")
            if not mk:
                continue
            out["variational"][tk] = (float(mk), float(it.get("base_spread_bps", 0) or 0),
                                      float(it.get("volume_24h", 0) or 0))
    except Exception as e:
        print(f"[basis] source B KO: {type(e).__name__}: {e}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=330.0)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--csv", default="basis.csv")
    ap.add_argument("--checkpoint-min", type=float, default=30.0)
    args = ap.parse_args()

    cols = ["iso_time", "epoch", "base", "hedge", "symbol", "base_mid", "hedge_mid",
            "basis_bps", "gross_bps", "crossing_bps", "spread", "vol"]
    new = (not os.path.exists(args.csv)) or os.path.getsize(args.csv) == 0
    f = open(args.csv, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new:
        w.writerow(cols)
        f.flush()

    t0 = time.time()
    end = t0 + args.minutes * 60
    n = 0
    last_ck = t0
    print(f"[basis] start — {args.minutes:.0f}min @ {args.interval:.0f}s -> {args.csv}", flush=True)
    while time.time() < end:
        mk = _marks()
        ts = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
        for venue, lg, sh in PAIRS:
            v = mk.get(venue, {})
            if lg in v and sh in v:
                lm, lsp, lvol = v[lg]
                sm, ssp, svol = v[sh]
                if lm > 0 and sm > 0:
                    basis = (lm - sm) / sm * 1e4
                    sym = f"{lg.split('-')[0]}-{sh.split('-')[0]}"
                    w.writerow([iso, f"{ts:.0f}", venue, venue, sym, f"{lm:.6f}", f"{sm:.6f}",
                                f"{basis:.2f}", f"{abs(basis):.2f}", f"{abs(basis):.2f}",
                                f"{max(lsp, ssp):.2f}", f"{min(lvol, svol):.0f}"])
                    n += 1
        f.flush()
        if time.time() - last_ck >= args.checkpoint_min * 60:
            print(f"[basis] checkpoint — {n} lignes, {(time.time() - t0) / 60:.0f}min", flush=True)
            last_ck = time.time()
        # sortie propre si le reste de temps < interval
        if time.time() + args.interval >= end:
            break
        time.sleep(args.interval)
    f.close()
    print(f"[basis] done — {n} lignes -> {args.csv}", flush=True)


if __name__ == "__main__":
    main()
