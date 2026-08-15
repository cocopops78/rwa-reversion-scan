#!/usr/bin/env python3
"""reversion_analyze.py — métriques de mean-reversion par couloir depuis le CSV série
temporelle produit par `builder_depth.py --mode crossing --csv <f>`.

Le scan donne μ/σ TERMINAL ; il ne teste pas si le spread REVIENT. Ici, par couloir :
  • demi-vie (AR(1) sur le basis démoyenné : Δx = β·x_{t-1}) = temps de retour à mi-écart ;
  • franchissements de la moyenne / heure = fréquence des aller-retours ;
  • σ, edge2σ = 2·grossσ − feesRT ; verdict REVIENT / DÉRIVE.

Un basis à offset persistant (POS%~100) n'oscille pas autour de 0 → on démoyenne par μ
avant de mesurer la réversion.

Colonnes CSV attendues :
  iso_time,epoch,base,hedge,symbol,base_mid,hedge_mid,basis_bps,gross_bps,crossing_bps

CAVEAT : extended/variational sont RFQ (marks cachés) → σ et demi-vie biaisés (sauts
d'escalier du mark), à confirmer. Les venues carnet (hl-xyz/lighter/vest) sont fiables.

Usage : python tools/reversion_analyze.py <fichier.csv> [--min-n 30]
"""
import sys, csv, math, statistics, argparse, collections

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# taker/jambe (bps) — même table que builder_depth.py. RT = 2×(base+hedge).
TAKER_BPS = {"hl-xyz": 0.9, "lighter": 0.0, "vest": 1.0, "extended": 2.25, "variational": 0.0,
             "txflow": 4.5,          # taker VIP0 0.045% (maker 0.015%)
             "paradex": 0.0,         # 0-fee (mais carnet fin -> slippage, cf spr)
             "hyperliquid": 3.5}     # taker base 0.035% (tiers en baisse selon volume)


def _halflife(x, dt_min):
    """AR(1) sur la série démoyennée x : Δx_t = β·x_{t-1}. φ=1+β ; demi-vie = -ln2/ln(φ),
    convertie samples→minutes via dt_min (pas d'échantillonnage médian).
    Renvoie (β, demi-vie_min|None). None si φ∉]0,1[ (φ≥1 = dérive/marche aléatoire ;
    φ≤0 = sur-réversion = bruit)."""
    if len(x) < 10 or dt_min <= 0:
        return None, None
    num = den = 0.0
    for i in range(1, len(x)):
        lag = x[i - 1]
        num += lag * (x[i] - x[i - 1])
        den += lag * lag
    if den <= 0:
        return None, None
    beta = num / den
    phi = 1.0 + beta
    if not (0.0 < phi < 1.0):
        return beta, None
    return beta, (-math.log(2) / math.log(phi)) * dt_min


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--min-n", type=int, default=30,
                    help="n minimal pour un verdict fiable (défaut 30)")
    ap.add_argument("--max-spread", type=float, default=40.0,
                    help="spread carnet moyen (bps) au-delà duquel le couloir = MIRAGE (feed cassé/illiquide)")
    ap.add_argument("--by-token", action="store_true",
                    help="grouper la sortie par TOKEN (comparer venue1:venue2 pour chaque token)")
    args = ap.parse_args()

    groups = collections.defaultdict(list)   # (base,hedge,sym) -> [(epoch,basis,gross,crossing)]
    with open(args.csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            # Parser AVANT d'accéder au groupe : sinon un 2e header (merge partA+partB) ou une
            # ligne pourrie créerait un groupe VIDE via le defaultdict (float lève après la
            # création) → statistics.mean([]) crash plus bas. Ici float lève → continue → rien créé.
            try:
                rec = (float(r["epoch"]), float(r["basis_bps"]),
                       float(r["gross_bps"]), float(r["crossing_bps"]),
                       float(r.get("spread", 0) or 0))
            except (KeyError, ValueError, TypeError):
                continue
            groups[(r["base"], r["hedge"], r["symbol"])].append(rec)

    if not groups:
        print(f"[reversion] aucune donnée exploitable dans {args.csv}")
        return

    hdr = (f"{'SYM':13} {'base':11} {'hedge':11} {'n':>5} {'span_h':>6} {'basisμ':>8} "
           f"{'grossσ':>7} {'spread':>7} {'demi-vie':>9} {'crois/h':>7} {'edge2σ':>7}  verdict")
    print(hdr)
    print("-" * len(hdr))
    out = []
    for (base, hedge, sym), rows in groups.items():
        rows.sort(key=lambda z: z[0])
        n = len(rows)
        ep = [z[0] for z in rows]
        basis = [z[1] for z in rows]
        gross = [z[2] for z in rows]
        spr = [z[4] for z in rows]
        bmu = statistics.mean(basis)
        gsig = statistics.pstdev(gross)
        spread_mu = statistics.mean(spr) if spr else 0.0
        span_h = max((ep[-1] - ep[0]) / 3600.0, 1e-9)
        # pas d'échantillonnage médian (ignore les gros trous = checkpoints/reconnexions)
        dts = [ep[i] - ep[i - 1] for i in range(1, n) if 0 < ep[i] - ep[i - 1] < 3600]
        dt_min = (statistics.median(dts) / 60.0) if dts else 0.0
        x = [b - bmu for b in basis]   # démoyenné : réversion vers l'offset μ, pas vers 0
        crossings = sum(1 for i in range(1, n) if (x[i] > 0) != (x[i - 1] > 0))
        cph = crossings / span_h
        beta, hl = _halflife(x, dt_min)
        fees = 2 * (TAKER_BPS.get(base, 0.0) + TAKER_BPS.get(hedge, 0.0))
        edge = 2 * gsig - fees
        # MIRAGE : carnet cassé/illiquide (spread énorme) → l'edge est un artefact de MID, PAS
        # exécutable (ex paradex sur alts : spread 90-6470 bps). On MARQUE, on n'exclut pas la venue.
        if spread_mu > args.max_spread:
            verdict = f"MIRAGE spread={spread_mu:.0f}"
        elif n < args.min_n:
            verdict = f"n<{args.min_n} (peu fiable)"
        elif hl is not None and hl < 240 and cph >= 1.0 and edge > 0:
            verdict = "REVIENT — piste"
        elif hl is not None and hl < 240 and cph >= 1.0:
            verdict = "revient mais edge<frais"
        elif beta is not None and beta >= 0:
            verdict = "DÉRIVE (pas de réversion)"
        else:
            verdict = "réversion faible/bruit"
        out.append((sym, base, hedge, n, span_h, bmu, gsig, spread_mu, hl, cph, edge, verdict))

    if args.by_token:
        # Par TOKEN (comparer venue1:venue2 pour chaque token), puis edge2σ décroissant.
        out.sort(key=lambda r: (r[0].split("-")[0], -r[10]))
    else:
        # MIRAGE en dernier, REVIENT en premier, puis edge2σ décroissant.
        out.sort(key=lambda r: (r[11].startswith("MIRAGE"), not r[11].startswith("REVIENT"), -r[10]))
    for sym, base, hedge, n, span_h, bmu, gsig, spread_mu, hl, cph, edge, verdict in out:
        hl_s = f"{hl:>9.0f}" if hl is not None else f"{'—':>9}"
        print(f"{sym:13} {base:11} {hedge:11} {n:>5} {span_h:>6.1f} {bmu:>+8.1f} "
              f"{gsig:>7.1f} {spread_mu:>7.1f} {hl_s} {cph:>7.1f} {edge:>+7.1f}  {verdict}")
    print("\nLecture : grossσ = amplitude d'oscillation ; spread = carnet moyen (jambe la plus mince, "
          "garde-fou exécutabilité) ; edge2σ = 2·grossσ − feesRT. REVIENT = demi-vie<240min ET "
          f"crois/h≥1 ET edge>0. MIRAGE = spread > {args.max_spread:.0f} bps (feed cassé → inexécutable).")
    print("RFQ INCLUS (variational ; extended sur RWA) : marks cachés → σ possiblement gonflé, à "
          "confirmer sur la durée. Venues carnet (lighter/extended/hyperliquid/vest) = fiables. "
          "--by-token pour comparer par token.")


if __name__ == "__main__":
    main()
