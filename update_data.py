#!/usr/bin/env python3
"""
Régénère data.js pour richlistbtc.com à partir des données publiques.

Sources :
  - bitinfocharts.com : distribution des adresses par tranche (nombre + masse BTC),
    table « adresses plus riches que $X », classement des 1 000 plus grosses
    adresses, ancres exactes aux rangs 5 000 et 10 000.

Sécurité : si une donnée manque ou semble incohérente, le script s'arrête
avec un code d'erreur SANS écrire data.js — le site garde alors les
données de la veille.

Aucune dépendance externe (bibliothèque standard uniquement).
"""

import json
import math
import re
import sys
import time
import urllib.request
from datetime import date

BASE = "https://bitinfocharts.com/top-100-richest-bitcoin-addresses{suffix}.html"
UA = {"User-Agent": "Mozilla/5.0 (compatible; richlistbtc-updater/1.0; +https://richlistbtc.com)"}
DELAY = 2.5  # secondes entre requêtes, par respect pour la source

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre"]

# Tranches où la calibration Pareto a validé mieux que l'interpolation log-log
PARETO_BRACKETS = {(0.001, 0.01), (1, 10), (10, 100), (100, 1000), (1000, 10000)}


def fetch(suffix=""):
    url = BASE.format(suffix=suffix)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="ignore")
    time.sleep(DELAY)
    return html


def parse_top_rows(html):
    """Lignes du classement : (rang, adresse, solde BTC)."""
    rows = re.findall(
        r"<tr[^>]*>\s*<td>(\d+)</td>\s*<td[^>]*>.*?bitcoin/address/"
        r"([13a-zA-Z0-9]{25,90}).*?</td>\s*<td[^>]*>([\d,\.]+)\s*BTC",
        html, re.S)
    return [(int(r), a, float(b.replace(",", ""))) for r, a, b in rows]


def parse_distribution(html):
    """Tranches : (lo, hi, nb adresses, total BTC, total USD)."""
    rows = re.findall(
        r"<tr>\s*<td>[\[\(]([\d,\.]+) - ([\d,\.]+)\)</td>\s*"
        r"<td data-val='(\d+)'>.*?<td data-val='([\d\.]+)'>.*?"
        r"<td[^>]*>\$([\d,]+)</td>", html, re.S)
    out = []
    for lo, hi, n, btc, usd in rows:
        out.append((float(lo.replace(",", "")), float(hi.replace(",", "")),
                    int(n), float(btc), float(usd.replace(",", ""))))
    return out


def parse_richer_than(html):
    """Table « adresses plus riches que $X » : {seuil USD: nb adresses}."""
    m = re.search(r"<caption>Addresses richer than</caption>(.*?)</table>", html, re.S)
    if not m:
        return {}
    seg = m.group(1)
    ths = [int(t.replace(",", "").replace("$", ""))
           for t in re.findall(r"<th[^>]*>\$([\d,]+)</th>", seg)]
    tds = [int(t.replace(",", ""))
           for t in re.findall(r"<td[^>]*>([\d,]+)</td>", seg)]
    return dict(zip(ths, tds)) if len(ths) == len(tds) else {}


def mean_pareto(a, lo, hi):
    if abs(a) < 1e-9:
        return (hi - lo) / math.log(hi / lo)
    if abs(a - 1) < 1e-9:
        return math.log(hi / lo) / (lo ** -1 - hi ** -1)
    return a / (lo ** -a - hi ** -a) * (hi ** (1 - a) - lo ** (1 - a)) / (1 - a)


def solve_alpha(lo, hi, mean):
    """Résout l'exposant Pareto dont la moyenne tronquée vaut `mean` (bissection)."""
    a, b = -5.0, 8.0
    fa = mean_pareto(a, lo, hi) - mean
    fb = mean_pareto(b, lo, hi) - mean
    if fa * fb > 0:
        return None
    for _ in range(200):
        m = (a + b) / 2
        fm = mean_pareto(m, lo, hi) - mean
        if fa * fm <= 0:
            b = m
        else:
            a, fa = m, fm
    return round((a + b) / 2, 4)


def main():
    # ── 1) Page 1 : distribution, table USD, top 100 ──────────────────
    p1 = fetch()
    dist = parse_distribution(p1)
    richer = parse_richer_than(p1)
    top = parse_top_rows(p1)

    if len(dist) < 10:
        sys.exit("ERREUR : table de distribution incomplète (%d tranches)" % len(dist))
    if len(top) != 100:
        sys.exit("ERREUR : page 1 du classement incomplète (%d lignes)" % len(top))

    # ── 2) Pages 2 à 10 : top 1000 ────────────────────────────────────
    for p in range(2, 11):
        rows = parse_top_rows(fetch(f"-{p}"))
        if len(rows) != 100:
            sys.exit(f"ERREUR : page {p} incomplète ({len(rows)} lignes)")
        top += rows
    top.sort()
    bals = [b for _, _, b in top]
    if len(bals) != 1000 or any(bals[i] < bals[i + 1] for i in range(999)):
        sys.exit("ERREUR : top 1000 incohérent")

    # ── 3) Ancres exactes : rangs 5 000 et 10 000 ─────────────────────
    anchors = []
    for p in (50, 100):
        rows = parse_top_rows(fetch(f"-{p}"))
        if rows:
            rank, _, bal = max(rows)
            if rank == p * 100:
                anchors.append((bal, rank))

    # ── 4) Comptages cumulés exacts depuis les tranches ───────────────
    dist.sort()  # par borne basse croissante
    total = sum(n for _, _, n, _, _ in dist)
    if not (30_000_000 < total < 200_000_000):
        sys.exit(f"ERREUR : total d'adresses implausible ({total})")

    cum, points = 0, []
    for lo, hi, n, _, _ in reversed(dist):
        points.append((lo if lo > 0 else 1e-8, cum + n))
        cum += n

    # ── 5) Points USD convertis au prix du snapshot ────────────────────
    big = max(dist, key=lambda d: d[3])          # tranche la plus massive
    price = big[4] / big[3]                       # USD / BTC du snapshot
    if not (1_000 < price < 10_000_000):
        sys.exit(f"ERREUR : prix implausible ({price})")
    for usd, count in richer.items():
        points.append((usd / price, count))

    # ── 6) Ancres + plancher du top 1000 ──────────────────────────────
    points += anchors
    points.append((bals[-1], 1000))

    # ── 7) Fusion : tri par solde, monotonie stricte des comptages ────
    points.sort()
    merged = []
    for b, c in points:
        while merged and merged[-1][1] <= c:
            merged.pop()                          # le point le plus fin gagne
        if not merged or b > merged[-1][0]:
            merged.append((b, c))
    merged = [(b, c) for b, c in merged if c >= 1000]  # au-delà : top 1000 exact
    merged = [(b, c) for b, c in merged] if merged[0][1] == total else merged
    if len(merged) < 12:
        sys.exit(f"ERREUR : trop peu de points exacts ({len(merged)})")

    # ── 8) Alphas Pareto calibrés sur la masse BTC des tranches ───────
    alphas = {}
    for lo, hi, n, btc_mass, _ in dist:
        if (lo, hi) in PARETO_BRACKETS and n > 0:
            a = solve_alpha(lo, hi, btc_mass / n)
            if a is not None:
                alphas[(lo, hi)] = a

    def alpha_for(b1, b2):
        mid = math.sqrt(b1 * b2)
        for (lo, hi), a in alphas.items():
            if lo <= mid < hi:
                return a
        return None

    final = []
    for i, (b, c) in enumerate(merged):
        nb = merged[i + 1][0] if i + 1 < len(merged) else b * 10
        final.append([b, c, alpha_for(b, nb)])

    # ── 9) Écriture de data.js ─────────────────────────────────────────
    today = date.today()
    snap_iso = today.isoformat()
    snap = f"{today.day} {MOIS[today.month - 1]} {today.year}"
    bal_js = "[" + ",".join(f"{b:.2f}".rstrip("0").rstrip(".") for b in bals) + "]"
    pts_js = json.dumps(final, separators=(",", ":"))

    out = f"""/* Données générées automatiquement — ne pas modifier à la main.
   Régénéré chaque nuit par update_data.py via GitHub Actions. */
const SNAPSHOT_ISO = "{snap_iso}";
const SNAPSHOT_DATE = "{snap}";
const TOTAL = {total};
const POINTS = {pts_js}; // [seuil, nb exact ≥ seuil, alpha Pareto calibré sur la masse BTC de la tranche (null = interpolation log-log)]
const TOPBAL = {bal_js}; // soldes exacts du top 1000, ordre décroissant
const TOP_FLOOR = TOPBAL[TOPBAL.length-1];
"""
    with open("data.js", "w", encoding="utf-8") as f:
        f.write(out)
    print(f"OK — {len(final)} points exacts, top 1000 (plancher {bals[-1]:,.0f} BTC), "
          f"total {total:,} adresses, prix snapshot {price:,.0f} $, date {snap}")


if __name__ == "__main__":
    main()
