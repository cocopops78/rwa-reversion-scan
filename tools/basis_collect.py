#!/usr/bin/env python3
"""basis_collect.py — echantillonneur de basis CROSS-VENUE (tous DEX connus, RFQ + CLOB).

Successeur du collecteur or (run-1..4 : pas d'edge). Scanne un univers de tokens sur TOUTES
les venues connues du bot. Pour chaque token present sur >=2 venues, enregistre le basis (bps)
de CHAQUE paire de venues dans le temps -> reversion_analyze.py mesure si ca OSCILLE assez
(2*grossσ) pour battre les frais aller-retour.

FILTRE CLE (lecon de l'or) : ne retenir qu'un couloir dont edge2σ = 2·grossσ − feesRT est
positif AVEC MARGE (>= +3 bps), demi-vie < 240 min, CONV reelle, ET spread carnet serre. Les
venues RFQ (variational ; extended sur RWA) = MIRAGES de marks (σ gonfle) -> a discounter.

Deux UNIVERS (--universe) :
  * crypto : 15 bluechips x extended/lighter/paradex/hyperliquid/txflow/variational/vest — 24/7,
             OK le WEEKEND.
  * rwa    : XAU/equities x extended/lighter/hl-xyz/variational/vest. Les PERPS RWA (xyz:TSLA,
             xyz:GOLD...) tradent 24/7 (moins liquides le WE, mais data reelle) -> OK le weekend
             aussi ; l'action US sous-jacente est fermee mais le perp continue. hl-xyz = 'xyz:GOLD'.

Sources KEYLESS (aucun secret) :
  * extended    : GET /api/v1/info/markets              (marketStats ; RWA extended = RFQ)
  * lighter     : GET /api/v1/orderBooks (map auto) + /orderBookOrders   (CLOB)
  * paradex     : GET /v1/orderbook/{TOKEN-USD-PERP}    (CLOB)
  * hyperliquid : POST /info {"type":"allMids"}         (CLOB, mid crypto)
  * hl-xyz      : POST /info {"type":"allMids","dex":"xyz"}  (CLOB builder, cles 'xyz:TICKER')
  * txflow      : POST /info {"type":"l2Book","coin":"<idx>"}  (CLOB HL-fork ; index connus BTC/ETH)
  * variational : GET /metadata/stats -> listings[]     (RFQ : mark_price/base_spread_bps)
  * vest        : GET /v2/exchangeInfo (map auto) + /v2/depth?symbol=  (CLOB)
  * rise        : GET /api/v1/markets (map auto) + /api/v1/orderbook?market_id=  (CLOB, risechain)

CSV (schema reversion_analyze) : iso_time,epoch,base,hedge,symbol,base_mid,hedge_mid,
  basis_bps,gross_bps,crossing_bps,spread,vol. base/hedge = VENUE ; spread = jambe la plus
  mince (bps, garde-fou executabilite) ; vol = profondeur min ($).

[!] WEEKEND : vol plus basse (crypto ET perps RWA) -> grossσ SOUS-estime (test conservateur).
Les perps RWA tradent quand meme le WE (sous-jacent US ferme mais perp actif). Acqui sur
PLUSIEURS JOURS recommandee avant tout verdict.

Usage : python tools/basis_collect.py --universe crypto --minutes 330 --interval 60 --csv part.csv
"""
import sys, os, csv, time, json, argparse, urllib.request, itertools

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0"}

UNIVERSES = {
    "crypto": {
        "tokens": ["BTC", "ETH", "SOL", "XRP", "BNB", "TRX", "DOGE", "ADA",
                   "AVAX", "LINK", "LTC", "DOT", "SUI", "TON", "BCH"],
        "venues": ["extended", "lighter", "paradex", "hyperliquid", "txflow", "variational", "vest", "rise"],
        "hlxyz": {},
    },
    "rwa": {   # [!] equities FERMEES le WE -> lancer un jour de semaine (session US)
        "tokens": ["XAU", "SPCX", "MRVL", "NVDA", "TSLA", "MU"],
        "venues": ["extended", "lighter", "hl-xyz", "variational", "vest", "txflow", "rise"],
        "hlxyz": {"XAU": "GOLD"},              # token -> ticker xyz ; gold = xyz:GOLD
    },
}

EXT_URL   = "https://api.starknet.extended.exchange/api/v1/info/markets"
LIT_BOOKS = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
LIT_ORD   = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookOrders"
PAR_URL   = "https://api.prod.paradex.trade/v1/orderbook"
HL_URL    = "https://api.hyperliquid.xyz/info"
TXF_URL   = "https://api.txflow.com/info"
VAR_URL   = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
VEST_URL  = "https://server-prod.hz.vestmarkets.com"
RISE_MKTS = "https://api.rise.trade/api/v1/markets"          # id/nom/mark (rise.trade CLOB, risechain)
RISE_OB   = "https://api.rise.trade/api/v1/orderbook"        # ?market_id=N -> bids/asks

_LIT_IDS = {}    # token -> lighter market_id (auto)
_VEST_SYM = {}   # token -> vest symbol (auto, ex BTC -> BTC-PERP)
_TXF_IDX = {}    # token -> txflow l2Book index (auto, price-match ; meta/allMids 403)
_RISE_ID = {}    # token -> rise market_id (auto, via /markets)


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url, body, origin=None):
    hdr = {**UA, "Content-Type": "application/json"}
    if origin:
        hdr["Origin"] = origin; hdr["Referer"] = origin + "/"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdr)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _discover_txflow(ref_prices, max_idx=90):
    """txflow : meta/allMids en 403 -> impossible d'ENUMERER. On scanne l2Book par INDEX et on
    matche chaque mid aux prix de REFERENCE (HL main pour crypto ; hl-xyz pour RWA) a ±1.5% ->
    token->index. Robuste au re-indexing (BNB=3, SOL=13 en crypto ; XAU=51, TSLA=59, MU=79 en RWA)."""
    refp = ref_prices
    for idx in range(0, max_idx):
        try:
            j = _post(TXF_URL, {"type": "l2Book", "coin": str(idx)}, origin="https://app.txflow.com")
            lv = j.get("levels")
            if not lv or not lv[0] or not lv[1]:
                continue
            mid = (float(lv[0][0]["px"]) + float(lv[1][0]["px"])) / 2
        except Exception:
            continue
        if mid <= 0:
            continue
        best_t, best_d = None, 0.015   # matche au token le plus proche, pas encore mappe
        for t, p in refp.items():
            if t in _TXF_IDX or p <= 0:
                continue
            d = abs(mid - p) / p
            if d < best_d:
                best_t, best_d = t, d
        if best_t:
            _TXF_IDX[best_t] = idx


def _discover(tokens, venues, hlxyz):
    """Auto-decouverte des ids/symboles par venue (jamais devine). lighter + vest + rise + txflow."""
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
    try:
        d = _get(f"{VEST_URL}/v2/exchangeInfo")
        syms = d.get("symbols", d) if isinstance(d, dict) else d
        for m in syms:
            s = str(m.get("symbol", ""))
            base = s.split("-")[0].upper()
            if base in tokset and m.get("tradingStatus", "TRADING") == "TRADING":
                _VEST_SYM[base] = s
    except Exception as e:
        print(f"[basis] vest syms KO: {type(e).__name__}: {e}", flush=True)
    if "rise" in venues:
        try:
            for mk in _get(RISE_MKTS).get("data", {}).get("markets", []):
                nm = str(mk.get("base_asset_symbol") or mk.get("config", {}).get("name", ""))
                base = nm.split("/")[0].upper()
                if base in tokset and mk.get("market_id") is not None:
                    _RISE_ID[base] = str(mk["market_id"])
        except Exception as e:
            print(f"[basis] rise ids KO: {type(e).__name__}: {e}", flush=True)
    if "txflow" in venues:
        refp = {}
        try:                                   # crypto : reference HL main
            main = _post(HL_URL, {"type": "allMids"})
            for t in tokens:
                if main.get(t):
                    refp[t] = float(main[t])
        except Exception:
            pass
        if any(t not in refp for t in tokens):  # RWA / tokens absents de HL main -> reference hl-xyz
            try:
                xyz = _post(HL_URL, {"type": "allMids", "dex": "xyz"})
                for t in tokens:
                    if t in refp:
                        continue
                    v = xyz.get("xyz:" + hlxyz.get(t, t))
                    if v:
                        refp[t] = float(v)
            except Exception:
                pass
        _discover_txflow(refp)
    print(f"[basis] lighter ids: {_LIT_IDS}", flush=True)
    print(f"[basis] vest syms: {_VEST_SYM}", flush=True)
    print(f"[basis] rise ids: {_RISE_ID}", flush=True)
    print(f"[basis] txflow idx (auto price-match): {_TXF_IDX}", flush=True)


def _marks(tokens, venues, hlxyz_map):
    """{venue: {token: (mid, spread_bps, vol$)}} — keyless, tolerant aux pannes par venue."""
    out = {v: {} for v in venues}
    tokset = set(tokens)
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
    if "hyperliquid" in venues:
        try:
            mids = _post(HL_URL, {"type": "allMids"})
            for t in tokens:
                v = mids.get(t)
                if v:
                    out["hyperliquid"][t] = (float(v), 0.0, 0.0)
        except Exception as e:
            print(f"[basis] hyperliquid KO: {type(e).__name__}: {e}", flush=True)
    if "hl-xyz" in venues:
        try:
            mids = _post(HL_URL, {"type": "allMids", "dex": "xyz"})
            for t in tokens:
                v = mids.get("xyz:" + hlxyz_map.get(t, t))
                if v:
                    out["hl-xyz"][t] = (float(v), 0.0, 0.0)
        except Exception as e:
            print(f"[basis] hl-xyz KO: {type(e).__name__}: {e}", flush=True)
    if "txflow" in venues:
        for t, idx in _TXF_IDX.items():
            if t not in tokset:
                continue
            try:
                j = _post(TXF_URL, {"type": "l2Book", "coin": str(idx)}, origin="https://app.txflow.com")
                lv = j.get("levels")
                if not lv or not lv[0] or not lv[1]:
                    continue
                bid = float(lv[0][0]["px"]); ask = float(lv[1][0]["px"]); m = (bid + ask) / 2
                spr = (ask - bid) / m * 1e4 if m > 0 else 0.0
                vol = float(lv[0][0].get("sz", 0) or 0) * m
                out["txflow"][t] = (m, spr, vol)
            except Exception:
                pass
    if "variational" in venues:
        try:
            for it in _get(VAR_URL).get("listings", []):
                tk = str(it.get("ticker", "")).upper()
                if tk not in tokset:
                    continue
                mk = it.get("mark_price")
                if not mk:
                    continue
                out["variational"][tk] = (float(mk), float(it.get("base_spread_bps", 0) or 0),
                                          float(it.get("volume_24h", 0) or 0))
        except Exception as e:
            print(f"[basis] variational KO: {type(e).__name__}: {e}", flush=True)
    if "vest" in venues:
        for t, vsym in _VEST_SYM.items():
            if t not in tokset:
                continue
            try:
                d = _get(f"{VEST_URL}/v2/depth?symbol={vsym}")
                bids = d.get("bids") or []; asks = d.get("asks") or []
                if not bids or not asks:
                    continue
                bb = max(float(p) for p, _ in bids); ba = min(float(p) for p, _ in asks)
                m = (bb + ba) / 2
                spr = (ba - bb) / m * 1e4 if m > 0 else 0.0
                out["vest"][t] = (m, spr, 0.0)
            except Exception:
                pass
    if "rise" in venues:
        for t, mid_id in _RISE_ID.items():
            if t not in tokset:
                continue
            try:
                d = _get(f"{RISE_OB}?market_id={mid_id}").get("data", {})
                bids = d.get("bids") or []; asks = d.get("asks") or []
                if not bids or not asks:
                    continue
                bid = float(bids[0]["price"]); ask = float(asks[0]["price"]); m = (bid + ask) / 2
                spr = (ask - bid) / m * 1e4 if m > 0 else 0.0
                sz = float(asks[0].get("quantity", 0) or 0)
                out["rise"][t] = (m, spr, sz * m)
            except Exception:
                pass
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
    _discover(tokens, venues, hlxyz)
    # Sonde de couverture (1 cycle) : voir TOUT DE SUITE si une venue est muette.
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
          f"{len(tokens)} tokens · {len(venues)} venues -> {args.csv}", flush=True)
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
