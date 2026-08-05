# rwa-reversion-scan

Scanner **keyless** de mean-reversion des couloirs RWA perp (base = `lighter` × `{extended, variational, hl-xyz}`), tournant sur **GitHub Actions** = IP cloud isolée.

**Pourquoi** : mesurer si les dislocations de spread entre venues **reviennent** (edge de mean-reversion) sans taper lighter depuis une IP maison — le WAF lighter (AWS `x-amzn-waf-action: captcha`, HTTP 405) bloque l'IP sous charge, et tous les devices maison partagent une seule IP. L'IP du runner Actions est isolée (validé : HTTP 200).

## Lancer

Onglet **Actions** → `reversion-scan` → **Run workflow**. Ou en CLI :

```
gh workflow run reversion-scan.yml -f minutes=350
```

Inputs : `minutes` (max ~350 sous le cap Actions de 6h), `hedges`, `symbols` (vide = tout l'univers), `lighter_gap` (throttle anti-429), `interval`.

À lancer **en séance US** (carnet RWA le plus profond ; hors-séance = non représentatif).

## Sortie

Artifact `reversion-<run_id>` :
- `reversion.csv` — série temporelle brute `(t, base, hedge, sym, mids, basis, gross, crossing)`.
- `reversion_report.txt` — par couloir : demi-vie AR(1), franchissements de μ/h, `edge2σ = 2·grossσ − feesRT`, verdict `REVIENT` / `DÉRIVE`.

## Outils

- `tools/builder_depth.py` — mesure crossing/basis/gross live (endpoints publics), écrit le CSV + checkpoints.
- `tools/reversion_analyze.py` — lit le CSV, calcule les métriques de réversion.

Repo **public** → minutes Actions **illimitées**. Aucun secret, endpoints publics uniquement.
