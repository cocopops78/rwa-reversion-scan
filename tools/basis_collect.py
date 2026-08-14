#!/usr/bin/env python3
"""basis_collect.py — echantillonneur de basis CROSS-VENUE sur venues CARNET.

Successeur du collecteur or (run-1..4 : verdict = pas d'edge exploitable). Scanne un univers
de tokens sur les venues A CARNET (les RFQ = mirages de marks, cf run-4). Pour chaque token
present sur >=2 venues, enregistre le basis (bps) de CHAQUE paire de venues dans le temps ->
reversion_analyze.py mesure si ca OSCILLE assez (2*grossσ) pour battre les frais aller-retour.

FILTRE CLE (lecon de l'or) : ne retenir qu'un couloir dont edge2σ = 2·grossσ − feesRT est
positif AVEC MARGE (>= +3 bps, pas +0.1), demi-vie < 240 min, CONV reelle, ET spread carnet
serre (sinon = mirage de mid, cf paradex 193bps).

Deux UNIVERS (--universe) :
  * crypto : BTC..DOGE sur extended/lighter/paradex/hyperliquid — marches 24/7, OK le WEEKEND.
  * rwa    : XAU/equities sur extended/lighter/hl-xyz (builder HIP-3). [!] Les EQUITIES sont
             FERMEES le week-end (marks geles -> data inexploitable) -> lancer un JOUR DE
             SEMAINE en session US. hl-xyz cote le gold sous 'xyz:GOLD'.

Sources KEYLESS (aucun secret) :
  * extended    : GET /api/v1/info/markets            (marketStats mark/bid/ask/dailyVolume)
  * lighter     : GET /api/v1/orderBooks (map symbol->market_id auto) + /orderBookOrders best bid/ask
  * paradex     : GET /v1/orderbook/{TOKEN-USD-PERP}  (best bid/ask)
  * hyperliquid : POST /info {"type":"allMids"}            (mid principal, crypto)
  * hl-xyz      : POST /info {"type":"allMids","dex":"xyz"} (mid builder RWA ; cles 'xyz:TICKER')

CSV (schema reversion_analyze) : iso_time,epoch,base,hedge,symbol,base_mid,hedge_mid,
  basis_bps,gross_bps,crossing_bps,spread,vol. base/hedge = VENUE ; spread = jambe la plus
  mince (bps, garde-fou executabilite) ; vol = profondeur min ($).

[!] WEEKEND : la vol crypto est plus basse le WE -> grossσ possiblement SOUS-estime (test
conservateur). Les EQUITIES sont carrement fermees -> universe=rwa a lancer en semaine.

Usage :
  python tools/basis_collect.py --universe crypto --minutes 330 --interval 60 --csv part.csv
"""
import sys, os, csv, time, json, argparse, urllib.request, itertools

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0"}

UNIVERSES = {
    "crypto": {
        "tokens": ["BTC", "ETH", "SOL", "XRP", "AVAX", "LINK", "DOGE"],
        "venues": ["extended", "lighter", "paradex", "hyperliquid"],
        "hlxyz": {},
    },
    "rwa": {   # [!] equities FERMEES le WE -> lancer un jour de semaine (session US)
        "tokens": ["XAU", "SPCX", "MRVL", "NVDA", "TSLA", "MU"],
        "venues": ["extended", "lighter", "hl-xyz"],
        "hlxyz": {"XAU": "GOLD"},   # token -> ticker xyz (defaut = token) ; gold = xyz:GOLD
    },
}

EXT_URL   = "https://api.starknet.extended.exchange/api/v1/info/markets"
LIT_BOOKS = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
LIT_ORD   = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookOrders"
PAR_URL   = "https://api.prod.paradex.trade/v1/orderbook"
HL_URL    = "https://api.hyperliquid.xyz/info"

_LIT_IDS = {}   # symbol -> market_id (auto au boot)


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url, body):
    hdr = {**UA, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdr)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _discover_lighter_ids(tokens):
    """symbol -> market_id via /orderBooks (jamais devine : cf piege sz_decimals lighter)."""
    tokset = set(tokens)
    try:
        j = _get(LIT_BOOKS)
        rows = j.get("order_books") or j.get("orderBooks") or (j if isinstance(j, list) else [])
        for ob in rows:
            sym = str(ob.get("symbol", "")).upper().split("-")[0].split("/")[0]
            mid = ob.get("market_id", ob.get("id"))
            if sym in tokset and mid is not None:
                _LIT_IDS[sym] = int(mid)
    except Exception as e:
        print(f"[basis] lighter ids KO: {type(e).__name__}: {e}", flush=True)
    print(f"[basis] lighter market_ids: {_LIT_IDS}", flush=True)


def _marks(tokens, venues, hlxyz_map):
    """{venue: {token: (mid, spread_bps, vol$)}} — keyless, tolerant aux pannes par venue."""
    out = {v: {} for v in venues}
    tokset = set(tokens)
    # --- extended (carnet ; RWA sur extended = RFQ -> mark dispo mais executabilite caveat) ---
    if "extended" in venues:
        try:
            want = {f"{t}-USD": t for t in tokens}
            for m in _get(EXT_URL).get("data", []):
                nm = m.get("name")
                if nm not in want:
                    continue
                ms = m.get("marketStats", {})
                mk = ms.get("markPrice") or ms.get("lastPrice")
                if not mk:
                    continue
                mk = float(mk)
                bid = float(ms.get("bidPrice", 0) or 0); ask = float(ms.get("askPrice", 0) or 0)
                spr = ((ask - bid) / mk * 1e4) if (bid > 0 and ask > 0 and mk > 0) else 0.0
                out["extended"][want[nm]] = (mk, spr, float(ms.get("dailyVolume", 0) or 0))
        except Exception as e:
            print(f"[basis] extended KO: {type(e).__name__}: {e}", flush=True)
    # --- lighter (carnet) ---
    if "lighter" in venues:
        for t, mid_id in _LIT_IDS.items():
            if t not in tokset:
                continue
            try:
                j = _get(f"{LIT_ORD}?market_id={mid_id}&limit=1")
                asks = j.get("asks") or []; bids = j.get("bids") or []
                if not asks or not bids:
                    continue
                ask = float(asks[0]["price"]); bid = float(bids[0]["price"]); m = (bid + ask) / 2
                spr = (ask - bid) / m * 1e4 if m > 0 else 0.0
                sz = float(asks[0].get("remaining_base_amount") or 0)
                out["lighter"][t] = (m, spr, sz * m)
            except Exception:
                pass
    # --- paradex (carnet ; crypto + PAXG) ---
    if "paradex" in venues:
        for t in tokens:
            try:
                j = _get(f"{PAR_URL}/{t}-USD-PERP?depth=1")
                a = j.get("asks") or []; b = j.get("bids") or []
                if not a or not b:
                    continue
                ask = float(a[0][0]); bid = float(b[0][0]); m = (bid + ask) / 2
                spr = (ask - bid) / m * 1e4 if m > 0 else 0.0
                sz = float(a[0][1]) if len(a[0]) > 1 else 0.0
                out["paradex"][t] = (m, spr, sz * m)
            except Exception:
                pass
    # --- hyperliquid MAIN (crypto ; mid, carnet tres liquide -> spread~0) ---
    if "hyperliquid" in venues:
        try:
            mids = _post(HL_URL, {"type": "allMids"})
            for t in tokens:
                v = mids.get(t)
                if v:
                    out["hyperliquid"][t] = (float(v), 0.0, 0.0)
        except Exception as e:
            print(f"[basis] hyperliquid KO: {type(e).__name__}: {e}", flush=True)
    # --- hl-xyz (builder HIP-3 RWA ; mid via allMids dex=xyz, cles 'xyz:TICKER') ---
    if "hl-xyz" in venues:
        try:
            mids = _post(HL_URL, {"type": "allMids", "dex": "xyz"})
            for t in tokens:
                key = "xyz:" + hlxyz_map.get(t, t)
                v = mids.get(key)
                if v:
                    out["hl-xyz"][t] = (float(v), 0.0, 0.0)
        except Exception as e:
            print(f"[basis] hl-xyz KO: {type(e).__name__}: {e}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="crypto", choices=list(UNIVERSES),
                    help="crypto (24/7, OK weekend) ou rwa (equities -> JOUR DE SEMAINE)")
    ap.add_argument("--minutes", type=float, default=330.0)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--csv", default="basis.csv")
    ap.add_argument("--checkpoint-min", type=float, default=30.0)
    args = ap.parse_args()

    uni = UNIVERSES[args.universe]
    tokens, venues, hlxyz = uni["tokens"], uni["venues"], uni["hlxyz"]
    _discover_lighter_ids(tokens)
    # Sonde de couverture (1 cycle) : voir TOUT DE SUITE si une venue est muette (un run non
    # surveille ne doit pas decouvrir a J+1 qu'un token/venue etait vide).
    _probe = _marks(tokens, venues, hlxyz)
    print("[basis] couverture initiale: " +
          " · ".join(f"{v}={len(_probe.get(v, {}))}/{len(tokens)}" for v in venues), flush=True)
    venue_pairs = list(itertools.combinations(venues, 2))

    cols = ["iso_time", "epoch", "base", "hedge", "symbol", "base_mid", "hedge_mid",
            "basis_bps", "gross_bps", "crossing_bps", "spread", "vol"]
    new = (not os.path.exists(args.csv)) or os.path.getsize(args.csv) == 0
    f = open(args.csv, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new:
        w.writerow(cols); f.flush()

    t0 = time.time(); end = t0 + args.minutes * 60; n = 0; last_ck = t0
    print(f"[basis] start [{args.universe}] — {args.minutes:.0f}min @ {args.interval:.0f}s · "
          f"tokens={tokens} · venues={venues} -> {args.csv}", flush=True)
    while time.time() < end:
        mk = _marks(tokens, venues, hlxyz)
        ts = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
        for va, vb in venue_pairs:
            da = mk.get(va, {}); db = mk.get(vb, {})
            for t in tokens:
                if t in da and t in db:
                    am, asp, avol = da[t]; bm, bsp, bvol = db[t]
                    if am > 0 and bm > 0:
                        basis = (am - bm) / bm * 1e4
                        lab = f"{t}-{va.replace('-', '')[:3]}-{vb.replace('-', '')[:3]}"
                        w.writerow([iso, f"{ts:.0f}", va, vb, lab,
                                    f"{am:.6f}", f"{bm:.6f}", f"{basis:.2f}", f"{abs(basis):.2f}",
                                    f"{abs(basis):.2f}", f"{max(asp, bsp):.2f}", f"{min(avol, bvol):.0f}"])
                        n += 1
        f.flush()
        if time.time() - last_ck >= args.checkpoint_min * 60:
            print(f"[basis] checkpoint — {n} lignes, {(time.time() - t0) / 60:.0f}min", flush=True)
            last_ck = time.time()
        if time.time() + args.interval >= end:
            break
        time.sleep(args.interval)
    f.close()
    print(f"[basis] done — {n} lignes -> {args.csv}", flush=True)


if __name__ == "__main__":
    main()
