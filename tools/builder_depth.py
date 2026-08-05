#!/usr/bin/env python3
"""
builder_depth.py — échantillonne la microstructure de perps multi-venues par symbole,
à partir d'endpoints publics keyless.

Deux modes :
  depth    : slippage à la taille (VWAP fill vs mid) par snapshots de l2Book, sur
             Hyperliquid (builder HIP-3 `xyz:*` et vanilla).
  crossing : distribution basis / gross / crossing sur une fenêtre, avec --csv (série
             temporelle) + --checkpoint-min, pour l'analyse de mean-reversion
             (tools/reversion_analyze.py).

Métriques (mode depth) :
  half_spread_bps = (ask1 - bid1)/mid * 1e4 / 2
  slip@$X (bps)   = (VWAP_fill_$X - mid)/mid * 1e4   (buy=walk asks / sell=walk bids)
  depth L1 $      = min(size1*px1 des 2 côtés)

CAVEAT : mesure INSTANTANÉE = borne inférieure (n'inclut pas la latence d'exécution ni le
mouvement pendant). À lancer en séance US (carnets les plus profonds). Voir --help.

Usage : python tools/builder_depth.py [--mode depth|crossing] --help
"""
import json, time, argparse, statistics, urllib.request, sys, os, csv

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

INFO_URL = "https://api.hyperliquid.xyz/info"
LIGHTER = "https://mainnet.zklighter.elliot.ai/api/v1"

# Univers RWA builder (préfixe xyz:) + crypto vanilla (référence, pas de préfixe)
RWA = ["MU", "MRVL", "SPCX", "NVDA", "TSLA", "AAPL", "GOOGL", "MSFT",
       "ORCL", "MSTR", "TSM", "CRWV", "NOW", "PLTR"]
CRYPTO = ["BTC", "ETH", "SOL", "HYPE"]
SIZES = [20, 100, 1000]   # $ notionnels testés


def l2book(coin):
    body = json.dumps({"type": "l2Book", "coin": coin}).encode()
    req = urllib.request.Request(INFO_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _slip(book_side, mid, notional, is_buy):
    """VWAP-slip en bps pour remplir $notional en croisant book_side ([{px,sz}]). >0 = coût."""
    filled = cost = szt = 0.0
    for lvl in book_side:
        px, sz = float(lvl["px"]), float(lvl["sz"])
        take_usd = min(px * sz, notional - filled)
        if take_usd <= 0:
            continue
        tsz = take_usd / px
        cost += tsz * px
        szt += tsz
        filled += take_usd
        if filled >= notional * 0.999:
            break
    if filled < notional * 0.999 or szt == 0:
        return None   # carnet trop mince pour remplir $notional
    vwap = cost / szt
    return (vwap - mid) / mid * 1e4 if is_buy else (mid - vwap) / mid * 1e4


def measure(coin):
    d = l2book(coin)
    lv = d.get("levels", [[], []])
    if len(lv) < 2 or not lv[0] or not lv[1]:
        return None
    bids, asks = lv[0], lv[1]
    bid1, ask1 = float(bids[0]["px"]), float(asks[0]["px"])
    mid = (bid1 + ask1) / 2
    if mid <= 0:
        return None
    half = (ask1 - bid1) / mid * 1e4 / 2
    depth1 = min(float(asks[0]["sz"]) * ask1, float(bids[0]["sz"]) * bid1)
    slips = {}
    for n in SIZES:
        sb = _slip(asks, mid, n, True)    # buy = croise les asks
        ss = _slip(bids, mid, n, False)   # sell = croise les bids
        vals = [x for x in (sb, ss) if x is not None]
        slips[n] = statistics.mean(vals) if vals else None
    return {"half": half, "depth1": depth1, "slips": slips,
            "nlev": min(len(bids), len(asks))}


def agg(snaps, key_fn):
    vals = [key_fn(s) for s in snaps if s and key_fn(s) is not None]
    if not vals:
        return None, None
    return statistics.median(vals), max(vals)


# ────────────────────────────────────────────────────────────────────────────
# Mode "crossing" : distribution du crossing (hl-xyz + lighter) et du gross détecté
# sur une fenêtre de séance, pour trouver le % du temps où un couple (gross≥X, crossing≤Y)
# tradeable existe. Calcul (bps) :
#   mid      = (h_bid+h_ask+l_bid+l_ask)/4
#   crossing = (h_ask-h_bid)/mid + (l_ask-l_bid)/mid    (bps, somme des 2 fourchettes)
#   gross    = max( l_bid-h_ask , h_bid-l_ask )/mid      (bps, meilleure direction)
# ────────────────────────────────────────────────────────────────────────────

def _hl_bbo(coin):
    d = l2book(coin)
    lv = d.get("levels", [[], []])
    if len(lv) < 2 or not lv[0] or not lv[1]:
        return None
    return float(lv[0][0]["px"]), float(lv[1][0]["px"])   # bid, ask


def _lighter_ids():
    """map SYM -> market_id pour les RWA actifs sur lighter. Renvoie {} si l'API échoue
    (ex 405/429 WAF par-IP) au lieu de crasher tout le run : la jambe lighter sera juste
    absente ce run (dégradation gracieuse — critique pour un run de séance complète)."""
    out = {}
    for attempt in range(3):
        try:
            d = json.load(urllib.request.urlopen(LIGHTER + "/orderBookDetails", timeout=10))
            for o in d.get("order_book_details", []):
                s = str(o.get("symbol", "")).upper()
                if s in RWA and str(o.get("status", "")) == "active":
                    out[s] = o.get("market_id")
            return out
        except Exception as e:
            if attempt < 2:
                time.sleep(2.0)
                continue
            print(f"  [warn] _lighter_ids KO ({type(e).__name__}: {e}) — jambe lighter "
                  f"désactivée ce run (WAF/429 probable ; relancer quand lighter répond)")
    return out


def _lighter_bbo(market_id):
    u = f"{LIGHTER}/orderBookOrders?market_id={market_id}&limit=2"
    d = json.load(urllib.request.urlopen(u, timeout=10))
    bids, asks = d.get("bids", []), d.get("asks", [])
    if not bids or not asks:
        return None
    return float(bids[0]["price"]), float(asks[0]["price"])   # bid, ask


VEST_DEPTH = "https://server-prod.hz.vestmarkets.com/v2/depth?symbol="
EXT_MARKETS = "https://api.starknet.extended.exchange/api/v1/info/markets"
VAR_CATALOG = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# taker/jambe (bps). extended RFQ 2.25 ; variational 0. RT = 2×(base+hedge).
TAKER_BPS = {"hl-xyz": 0.9, "lighter": 0.0, "vest": 1.0, "extended": 2.25, "variational": 0.0}
BOOK_VENUES = {"hl-xyz", "lighter", "vest"}        # vrai carnet (bid/ask réels)
MARK_VENUES = {"extended", "variational"}          # RFQ : bid=ask=mark (pas de fourchette publique)


def _get(url, hdr=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr or {}), timeout=12) as r:
        return json.load(r)


def _vest_bbo(sym):
    d = _get(VEST_DEPTH + sym + "-USD-PERP")
    bids, asks = d.get("bids", []), d.get("asks", [])
    if not bids or not asks:
        return None
    return float(bids[0][0]), float(asks[0][0])   # bid, ask


def _catalogs(venues):
    """Catalogues fetchés 1×/round (venues RFQ à mark). extended = /info/markets (RWA suffixe
    _24_5-USD, marketStats.bid/askPrice=mark) ; variational = /metadata/stats (mark_price cached)."""
    ctx = {}
    if "extended" in venues:
        try:
            ctx["extended"] = {m["name"]: m for m in _get(EXT_MARKETS, _UA).get("data", [])}
        except Exception:
            ctx["extended"] = {}
    if "variational" in venues:
        try:
            ctx["variational"] = {str(it.get("ticker", "")).upper(): it
                                  for it in _get(VAR_CATALOG, _UA).get("listings", [])}
        except Exception:
            ctx["variational"] = {}
    return ctx


def _venue_bbo(venue, sym, ids, ctx):
    """(bid, ask) pour un symbole sur une venue. RFQ (extended/variational) → bid=ask=mark."""
    if venue == "hl-xyz":
        return _hl_bbo("xyz:" + sym)
    if venue == "lighter":
        return _lighter_bbo(ids[sym]) if sym in ids else None
    if venue == "vest":
        return _vest_bbo(sym)
    if venue == "extended":
        cat = ctx.get("extended", {})
        for nm in (f"{sym}_24_5-USD", f"{sym}-USD"):   # RWA 24/5 puis nom plain
            m = cat.get(nm)
            if m:
                st = m.get("marketStats", {})
                b = float(st.get("bidPrice", 0) or 0)
                a = float(st.get("askPrice", 0) or 0)
                if b > 0 and a > 0:
                    return b, a
        return None
    if venue == "variational":
        it = ctx.get("variational", {}).get(sym)
        if it:
            mk = float(it.get("mark_price", 0) or 0)
            sp = float(it.get("base_spread_bps", 0) or 0) / 1e4
            if mk > 0:
                return mk * (1 - sp / 2), mk * (1 + sp / 2)
        return None
    return None


def _crossing_report(samples, base, hedges, partial=False):
    """Agrège + imprime le tableau basisμ/σ, grossμ/σ, crossing, edge2σ.
    partial=True → dump de CHECKPOINT (pas la légende de fin)."""
    hdr = (f"{'SYM':6} {'hedge':10} {'n':>5} {'basisμ':>7} {'basisσ':>7} {'POS%':>5} "
           f"{'grossμ':>7} {'grossσ':>7} {'cross':>6} {'feesRT':>6} {'edge2σ':>7}  verdict")
    tag = f"   [CHECKPOINT {time.strftime('%H:%M:%S')}]" if partial else ""
    print("\n" + hdr + tag)
    print("-" * len(hdr))
    rows = []
    for (s, hedge), sm in samples.items():
        if len(sm) < 3:
            rows.append((s, hedge, None))
            continue
        basis = [x[0] for x in sm]
        gross = [x[1] for x in sm]
        cross = sorted(x[2] for x in sm)
        bmu, bsig = statistics.mean(basis), statistics.pstdev(basis)
        gmu, gsig = statistics.mean(gross), statistics.pstdev(gross)
        crmed = cross[len(cross) // 2]
        fees = 2 * (TAKER_BPS.get(base, 0.0) + TAKER_BPS.get(hedge, 0.0))
        pos = 100.0 * sum(1 for b in basis if b > 0) / len(basis)
        edge = 2 * gsig - fees   # capturable via mean-reversion (~2σ) après frais (crossing déjà dans gross)
        rows.append((s, hedge, {"n": len(sm), "bmu": bmu, "bsig": bsig, "pos": pos,
                                "gmu": gmu, "gsig": gsig, "cr": crmed, "fees": fees, "edge": edge}))
    rows.sort(key=lambda r: -(r[2]["edge"] if r[2] else -1e9))
    for s, hedge, m in rows:
        if not m:
            print(f"{s:6} {hedge:10}  (échantillon insuffisant)")
            continue
        v = ("OK edge>0" if m["edge"] > 0 else "marginal" if m["edge"] > -1.0 else "non (2σ<frais)")
        pf = " [basis persist→offset]" if (m["pos"] > 90 or m["pos"] < 10) else ""
        print(f"{s:6} {hedge:10} {m['n']:>5} {m['bmu']:>+7.1f} {m['bsig']:>7.1f} {m['pos']:>4.0f}% "
              f"{m['gmu']:>+7.1f} {m['gsig']:>7.1f} {m['cr']:>6.1f} {m['fees']:>6.1f} {m['edge']:>+7.1f}  {v}{pf}")
    if not partial:
        print("\nLecture : basisμ/σ = dislocation (hedge−base). μ=offset persistant (géré par "
              "un offset externe), σ=oscillation. POS%~100/0 = basis persistant (offset requis) ; "
              "~50 = oscille autour 0. grossσ = σ du spread d'entrée réel (bid/ask, crossing inclus). "
              "edge2σ = 2·grossσ − feesRT = capturable par mean-reversion après frais (>0 = piste réelle).")
        print("après-séance = non représentatif (pas d'arbitrage NASDAQ). Lancer EN SÉANCE US.")
        print("Réversion (demi-vie / franchissements) : lancer avec --csv puis tools/reversion_analyze.py.")


def crossing_mode(args):
    # Mesure basis μ/σ par binôme BASE × hedge. BASE configurable (--base, défaut hl-xyz).
    # Venues carnet (bid/ask réels) : hl-xyz, lighter, vest. Venues RFQ (bid=ask=mark) :
    # extended (/info/markets, RWA suffixe _24_5-USD), variational (/metadata/stats, mark cached).
    # → pour ext:var : --base extended --hedges variational.
    # Run LONG (séance complète) : --csv <f> (série brute → reversion_analyze.py),
    # --checkpoint-min N (dump agrégat partiel, anti-perte sur crash), --lighter-gap S
    # (throttle lighter, anti-429 : l'API lighter rate-limite par-IP sous charge).
    base = (args.base or "hl-xyz").strip()
    hedges = [h.strip() for h in (args.hedges or "lighter,vest").split(",") if h.strip()]
    venues = {base} | set(hedges)
    ids = _lighter_ids() if "lighter" in venues else {}
    syms = [s for s in (args.symbols.split(",") if args.symbols else RWA) if s in RWA]
    print(f"[crossing] {base} × {hedges} | {len(syms)} symboles × ~{args.minutes}min @ {args.interval}s "
          f"— {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  symboles : {','.join(syms)}   (EN SÉANCE US ; marks RFQ cached → σ sous-estimé)")
    if args.lighter_gap > 0 and "lighter" in venues:
        print(f"  throttle lighter : 1 refresh / {args.lighter_gap:.0f}s / symbole (anti-429)")
    if args.csv:
        print(f"  CSV série temporelle (append) → {args.csv}")
    if args.checkpoint_min > 0:
        print(f"  checkpoint agrégat toutes les {args.checkpoint_min:.0f} min")

    samples = {(s, h): [] for s in syms for h in hedges}   # (basis, gross, crossing)
    ctx = {}                                               # catalogues RFQ, rafraîchis /round
    _lit_cache = {}                                        # anti-429 : sym -> (bbo|None, ts)

    def _bbo(venue, sym):
        """BBO d'une venue, avec cache lighter throttlé (--lighter-gap) pour éviter le 429."""
        if venue == "lighter" and args.lighter_gap > 0:
            c = _lit_cache.get(sym)
            if c is not None and (time.time() - c[1]) < args.lighter_gap:
                return c[0]
            try:
                v = _venue_bbo("lighter", sym, ids, ctx)
            except Exception:
                v = None
            _lit_cache[sym] = (v, time.time())
            return v
        try:
            return _venue_bbo(venue, sym, ids, ctx)
        except Exception:
            return None

    csv_w = csv_f = None
    if args.csv:
        _new = (not os.path.exists(args.csv)) or os.path.getsize(args.csv) == 0
        csv_f = open(args.csv, "a", newline="", encoding="utf-8")
        csv_w = csv.writer(csv_f)
        if _new:
            csv_w.writerow(["iso_time", "epoch", "base", "hedge", "symbol",
                            "base_mid", "hedge_mid", "basis_bps", "gross_bps", "crossing_bps"])

    t_end = time.time() + args.minutes * 60
    last_ckpt = time.time()
    try:
        while time.time() < t_end:
            ctx = _catalogs(venues)   # catalogues RFQ (extended/variational) frais 1×/round
            for s in syms:
                bb = _bbo(base, s)
                if not bb:
                    continue
                hb, ha = bb
                hmid = (hb + ha) / 2
                for hedge in hedges:
                    hg = _bbo(hedge, s)
                    if not hg:
                        continue
                    gb, ga = hg
                    gmid = (gb + ga) / 2
                    mid = (hb + ha + gb + ga) / 4
                    if mid <= 0:
                        continue
                    crossing = ((ha - hb) + (ga - gb)) / mid * 1e4     # somme fourchettes (0 si RFQ)
                    basis = (gmid - hmid) / mid * 1e4                  # dislocation (hedge − base) signée
                    gross = max(gb - ha, hb - ga) / mid * 1e4          # meilleure dir
                    samples[(s, hedge)].append((basis, gross, crossing))
                    if csv_w is not None:
                        _t = time.time()
                        csv_w.writerow([time.strftime('%Y-%m-%d %H:%M:%S'), f"{_t:.1f}",
                                        base, hedge, s, f"{hmid:.6f}", f"{gmid:.6f}",
                                        f"{basis:.2f}", f"{gross:.2f}", f"{crossing:.2f}"])
                    time.sleep(0.04)
            if csv_f is not None:
                csv_f.flush()
            if args.checkpoint_min > 0 and (time.time() - last_ckpt) >= args.checkpoint_min * 60:
                _crossing_report(samples, base, hedges, partial=True)
                last_ckpt = time.time()
            time.sleep(args.interval)
    finally:
        if csv_f is not None:
            csv_f.close()

    _crossing_report(samples, base, hedges, partial=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["depth", "crossing"], default="depth")
    ap.add_argument("--snaps", type=int, default=5)
    ap.add_argument("--gap", type=float, default=5.0)
    ap.add_argument("--cap", type=float, default=6.0)
    # mode crossing
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--interval", type=float, default=4.0)
    ap.add_argument("--cross-max", dest="cross_max", type=float, default=4.0)
    ap.add_argument("--spread-min", dest="spread_min", type=float, default=8.0)
    ap.add_argument("--symbols", type=str, default="")
    ap.add_argument("--hedges", type=str, default="", help="mode crossing : ex 'lighter,vest,extended,variational'")
    ap.add_argument("--base", type=str, default="", help="mode crossing : jambe A (défaut hl-xyz ; ex 'extended' pour ext:var)")
    ap.add_argument("--csv", type=str, default="", help="mode crossing : CSV série temporelle (append ; pour reversion_analyze.py)")
    ap.add_argument("--checkpoint-min", dest="checkpoint_min", type=float, default=0.0, help="mode crossing : dump agrégat partiel toutes les N min (0=off)")
    ap.add_argument("--lighter-gap", dest="lighter_gap", type=float, default=0.0, help="mode crossing : throttle lighter, 1 refresh/symbole/N s (anti-429 ; 0=off)")
    args = ap.parse_args()
    if args.mode == "crossing":
        crossing_mode(args)
        return

    universe = [("xyz:" + s, s, "RWA") for s in RWA] + [(s, s, "CRYPTO") for s in CRYPTO]
    data = {coin: [] for coin, _, _ in universe}

    print(f"[builder_depth] {args.snaps} snapshots × {len(universe)} coins, gap {args.gap}s "
          f"— {time.strftime('%Y-%m-%d %H:%M:%S')} (lance en séance US pour la vraie profondeur)")
    for i in range(args.snaps):
        for coin, _, _ in universe:
            try:
                data[coin].append(measure(coin))
            except Exception:
                data[coin].append(None)
            time.sleep(0.08)
        if i < args.snaps - 1:
            time.sleep(args.gap)

    rows = []
    for coin, sym, grp in universe:
        snaps = [s for s in data[coin] if s]
        if not snaps:
            rows.append((grp, sym, None))
            continue
        half_med, _ = agg(snaps, lambda s: s["half"])
        d1_med, _ = agg(snaps, lambda s: s["depth1"])
        s20_med, s20_max = agg(snaps, lambda s: s["slips"][20])
        s100_med, _ = agg(snaps, lambda s: s["slips"][100])
        s1k_med, _ = agg(snaps, lambda s: s["slips"][1000])
        rows.append((grp, sym, {"half": half_med, "d1": d1_med,
                                "s20": s20_med, "s20max": s20_max,
                                "s100": s100_med, "s1k": s1k_med, "n": len(snaps)}))

    def sortkey(r):
        return (0 if r[0] == "RWA" else 1, r[2]["s20"] if r[2] and r[2]["s20"] is not None else 1e9)
    rows.sort(key=sortkey)

    def verdict(s20, cap):
        if s20 is None:
            return "carnet trop mince <$20"
        if s20 < cap:
            return f"OK (<cap {cap:.0f})"
        if s20 < cap * 1.5:
            return "LIMITE"
        return "MORT (mirage)"

    hdr = (f"{'GRP':4} {'SYM':6} {'half½':>7} {'slip@20':>8} {'(max)':>7} "
           f"{'slip@100':>9} {'slip@1k':>8} {'depthL1$':>9} {'n':>3}  verdict")
    print("\n" + hdr)
    print("-" * len(hdr))
    for grp, sym, m in rows:
        if not m:
            print(f"{grp:4} {sym:6} {'—':>7}  (pas de carnet / non listé sur le builder)")
            continue
        f = lambda v, w=8, p=2: (f"{v:>{w}.{p}f}" if v is not None else f"{'—':>{w}}")
        print(f"{grp:4} {sym:6} {f(m['half'],7)} {f(m['s20'],8)} {f(m['s20max'],7)} "
              f"{f(m['s100'],9)} {f(m['s1k'],8)} {f(m['d1'],9,0)} {m['n']:>3}  "
              f"{verdict(m['s20'], args.cap)}")
    print("\nLecture : slip@$X = coût moyen (buy+sell)/2 pour croiser $X, en bps. "
          f"cap IOC = {args.cap:.0f} bps. slip@$20 = petite taille ; slip@$1k = grosse taille.")
    print("Rappel : borne INFÉRIEURE (latence exec non incluse). > cap à l'instant = mort d'office.")


if __name__ == "__main__":
    main()
