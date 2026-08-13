#!/usr/bin/env python3
"""basis_collect.py — echantillonneur de basis inter-instruments correles (meme venue).

Deux instruments proches cotes sur la MEME venue : on enregistre leur ecart (basis, bps)
dans le temps pour voir s'il OSCILLE (reversion, exploitable) ou reste un discount
PERSISTANT (non exploitable). Sortie CSV au schema de reversion_analyze.py.

Sources KEYLESS (market-data publique, aucun secret) :
  * /api/v1/info/markets            (marketStats.markPrice, bid/ask)
  * /metadata/stats -> listings[]   (mark_price, base_spread_bps, volume_24h)

Paires par defaut :
  * XAUT vs XAU   (intra-venue)
  * PAXG vs XAU   (intra-venue)
  * XAU vs XAU    (cross-venue : meme actif sur 2 venues)

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
# Source C = perps L1 keyless, POST /info {"type":"l2Book","coin":"<index>"} (schema HL-fork).
# `coin` = INDEX NUMERIQUE (pas le nom). Resolus par prix (mid dans la plage or) : XAUT=46, XAU=51.
SRC_C = "https://api.txflow.com/info"
TXFLOW_IDX = {"XAUT": 46, "XAU": 51}
# Source D = lighter (carnet keyless). orderBookOrders best bid/ask -> mid. market_id par symbole.
# NB: lighter a XAU et PAXG mais PAS XAUT. Depuis un runner GHA (IP != bot) -> pas de conflit WAF.
SRC_D = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookOrders"
LIGHTER_IDS = {"XAU": 92, "PAXG": 48}
# Source E = paradex (carnet keyless). /v1/orderbook/{market}. Or : SEULEMENT PAXG (0-fee taker).
SRC_E = "https://api.prod.paradex.trade/v1/orderbook"
PARADEX_MARKETS = {"PAXG-USD-PERP": "PAXG-USD-PERP"}

# (label, long_venue, long_ticker, short_venue, short_ticker) — venues doivent matcher
# TAKER_BPS de reversion_analyze (base=long_venue, hedge=short_venue). Intra-venue si
# long_venue == short_venue ; cross-venue (meme actif, 2 venues) sinon.
# Actifs par venue : txflow=XAU/XAUT · variational=XAU/XAUT/PAXG · extended=XAU/PAXG ·
# lighter=XAU/PAXG · paradex=PAXG (0-fee).
PAIRS = [
    # --- CROSS-VENUE meme actif (tradeable) ---
    ("XAU-txf-lit",  "txflow", "XAU",  "lighter", "XAU"),            # LE combo maker (hedge lighter)
    ("XAU-txf-var",  "txflow", "XAU",  "variational", "XAU"),
    ("XAU-var-lit",  "variational", "XAU", "lighter", "XAU"),
    ("XAU-var-ext",  "variational", "XAU", "extended", "XAU-USD"),
    ("XAU-ext-lit",  "extended", "XAU-USD", "lighter", "XAU"),
    ("XAUT-txf-var", "txflow", "XAUT", "variational", "XAUT"),       # XAUT : que txf+var
    ("PAXG-par-lit", "paradex", "PAXG-USD-PERP", "lighter", "PAXG"), # paradex 0-fee vs lighter
    ("PAXG-par-var", "paradex", "PAXG-USD-PERP", "variational", "PAXG"),
    ("PAXG-par-ext", "paradex", "PAXG-USD-PERP", "extended", "PAXG-USD"),
    ("PAXG-var-lit", "variational", "PAXG", "lighter", "PAXG"),
    ("PAXG-ext-lit", "extended", "PAXG-USD", "lighter", "PAXG"),
    # --- INTRA-VENUE (observation) ---
    ("XAUT-XAU-var", "variational", "XAUT", "variational", "XAU"),
    ("XAUT-XAU-txf", "txflow", "XAUT", "txflow", "XAU"),
    ("PAXG-XAU-ext", "extended", "PAXG-USD", "extended", "XAU-USD"),
]


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url, body):
    hdr = {**UA, "Content-Type": "application/json",
           "Origin": "https://app.txflow.com", "Referer": "https://app.txflow.com/"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdr)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _marks():
    """{venue: {ticker: (mark, spread_bps, vol)}} — keyless, tolerant aux pannes reseau."""
    out = {"extended": {}, "variational": {}, "txflow": {}, "lighter": {}, "paradex": {}}
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
    try:
        for name, idx in TXFLOW_IDX.items():
            j = _post(SRC_C, {"type": "l2Book", "coin": str(idx)})
            lv = j.get("levels")
            if not lv or not lv[0] or not lv[1]:
                continue
            bid = float(lv[0][0]["px"]); ask = float(lv[1][0]["px"])
            mid = (bid + ask) / 2
            if not (4000 < mid < 4600):   # garde-fou : l'index a change de marche -> skip
                print(f"[basis] txflow {name} idx {idx} hors plage or (mid={mid:.1f}) — verifier index", flush=True)
                continue
            spr = (ask - bid) / mid * 1e4 if mid > 0 else 0.0
            vol = float(lv[0][0].get("sz", 0) or 0) * mid   # proxy = profondeur best bid en $
            out["txflow"][name] = (mid, spr, vol)
    except Exception as e:
        print(f"[basis] source C (txflow) KO: {type(e).__name__}: {e}", flush=True)
    try:
        for name, mid_id in LIGHTER_IDS.items():
            j = _get(f"{SRC_D}?market_id={mid_id}&limit=1")
            asks = j.get("asks") or []; bids = j.get("bids") or []
            if not asks or not bids:
                continue
            ask = float(asks[0]["price"]); bid = float(bids[0]["price"])
            mid = (bid + ask) / 2
            spr = (ask - bid) / mid * 1e4 if mid > 0 else 0.0
            sz = float(asks[0].get("remaining_base_amount") or 0)
            out["lighter"][name] = (mid, spr, sz * mid)
    except Exception as e:
        print(f"[basis] source D (lighter) KO: {type(e).__name__}: {e}", flush=True)
    try:
        for name in PARADEX_MARKETS:
            j = _get(f"{SRC_E}/{name}?depth=1")
            a = j.get("asks") or []; b = j.get("bids") or []
            if not a or not b:
                continue
            ask = float(a[0][0]); bid = float(b[0][0]); mid = (bid + ask) / 2
            spr = (ask - bid) / mid * 1e4 if mid > 0 else 0.0
            sz = float(a[0][1]) if len(a[0]) > 1 else 0.0
            out["paradex"][name] = (mid, spr, sz * mid)
    except Exception as e:
        print(f"[basis] source E (paradex) KO: {type(e).__name__}: {e}", flush=True)
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
        for label, lv, lg, sv, sh in PAIRS:
            ld = mk.get(lv, {})
            sd = mk.get(sv, {})
            if lg in ld and sh in sd:
                lm, lsp, lvol = ld[lg]
                sm, ssp, svol = sd[sh]
                if lm > 0 and sm > 0:
                    basis = (lm - sm) / sm * 1e4
                    w.writerow([iso, f"{ts:.0f}", lv, sv, label, f"{lm:.6f}", f"{sm:.6f}",
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
