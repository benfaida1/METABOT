#!/usr/bin/env python3
"""
backtest_funding_arb.py

Backtest de la strategie Funding Rate Arbitrage sur Binance.

Strategie :
    - Quand le funding rate d'un perpetuel > seuil d'entree :
        * Achete N USD de spot
        * Vend N USD de perp (short futures)
        * Position market-neutral : peu importe l'evolution du prix
    - Toutes les 8h, le short perp recoit (ou paie) le funding rate
    - Quand le funding rate < seuil de sortie : ferme tout

Output :
    - Tableau comparatif par symbole et par seuil
    - Nombre d'opportunites/an, gain net moyen, rendement annuel, drawdown

Hypotheses de couts :
    - Spot taker : 0.075% (avec BNB)
    - Futures taker : 0.04% (avec BNB)
    - Slippage : 0.02% par leg
    - Cout total d'un cycle complet : ~0.31%

Usage :
    python backtest_funding_arb.py
    python backtest_funding_arb.py --jours 365 --symboles BTCUSDT ETHUSDT
    python backtest_funding_arb.py --seuils 0.0001 0.0002 0.0003 0.0005
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request


SPOT_BASE    = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"

# Frais (taker, avec BNB discount actif)
FEE_SPOT      = 0.00075   # 0.075%
FEE_FUTURES   = 0.0004    # 0.04%
SLIPPAGE_LEG  = 0.0002    # 0.02% par leg
FUNDING_INTERVAL_HOURS = 8


# =====================================================================
# Telechargement des donnees
# =====================================================================
def fetch_funding_history(symbole, start_ms, end_ms):
    """Recupere l'historique funding rate d'un perpetuel (USDT-M Futures)."""
    out = []
    cur = start_ms
    while cur < end_ms:
        url = (f"{FUTURES_BASE}/fapi/v1/fundingRate"
               f"?symbol={symbole}&startTime={cur}&endTime={end_ms}&limit=1000")
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read().decode())
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            print(f"  ! erreur fetch funding {symbole}: {e}")
            return None
        if not data:
            break
        for row in data:
            out.append({
                "t": int(row["fundingTime"]),
                "rate": float(row["fundingRate"]),
                "mark": float(row.get("markPrice") or 0),
            })
        cur = int(data[-1]["fundingTime"]) + 1
        if len(data) < 1000:
            break
        time.sleep(0.15)
    return out


# =====================================================================
# Simulation de la strategie
# =====================================================================
def simuler(funding_history, seuil_entree, seuil_sortie, notional=1000.0):
    """
    Simule la strategie sur l'historique funding.

    Args:
        funding_history : liste de {"t", "rate", "mark"}
        seuil_entree   : ex 0.0003 (=0.03% par 8h, soit ~33%/an annualise)
        seuil_sortie   : ex 0.0001
        notional        : taille de la position en USD (peu importe, on
                          calcule en % du notional)

    Returns:
        dict avec stats agreges.
    """
    state = "flat"
    cycles = []
    cycle_courant = None
    equity_pct = 0.0    # gain cumule en % du notional
    peak_pct   = 0.0
    max_dd_pct = 0.0

    cout_ouverture = (FEE_SPOT + FEE_FUTURES + 2 * SLIPPAGE_LEG)
    cout_fermeture = (FEE_SPOT + FEE_FUTURES + 2 * SLIPPAGE_LEG)

    for ev in funding_history:
        rate = ev["rate"]

        if state == "flat":
            if rate >= seuil_entree:
                # Ouverture
                state = "long_spot_short_perp"
                equity_pct -= cout_ouverture * 100  # en %
                cycle_courant = {
                    "t_entree": ev["t"],
                    "rate_entree": rate,
                    "fundings_recus": 0.0,    # en % du notional
                    "nb_fundings": 0,
                    "cout_total": cout_ouverture * 100,
                }
                _maj_dd(equity_pct)

        elif state == "long_spot_short_perp":
            # On RECEIVE le funding (positif si rate > 0 car on est short perp)
            equity_pct += rate * 100   # en %
            cycle_courant["fundings_recus"] += rate * 100
            cycle_courant["nb_fundings"] += 1

            if rate <= seuil_sortie:
                # Fermeture
                state = "flat"
                equity_pct -= cout_fermeture * 100
                cycle_courant["t_sortie"] = ev["t"]
                cycle_courant["cout_total"] += cout_fermeture * 100
                cycle_courant["pnl_net_pct"] = (
                    cycle_courant["fundings_recus"]
                    - cycle_courant["cout_total"]
                )
                cycles.append(cycle_courant)
                cycle_courant = None

        # Mise a jour drawdown
        peak_pct = max(peak_pct, equity_pct)
        dd = peak_pct - equity_pct
        max_dd_pct = max(max_dd_pct, dd)

    # Fermeture forcee si position ouverte a la fin (sortie au prix de fermeture)
    if state != "flat" and cycle_courant:
        equity_pct -= cout_fermeture * 100
        cycle_courant["t_sortie"] = funding_history[-1]["t"]
        cycle_courant["cout_total"] += cout_fermeture * 100
        cycle_courant["pnl_net_pct"] = (
            cycle_courant["fundings_recus"] - cycle_courant["cout_total"]
        )
        cycles.append(cycle_courant)

    return {
        "cycles": cycles,
        "nb_cycles": len(cycles),
        "pnl_total_pct": equity_pct,
        "max_dd_pct": max_dd_pct,
    }


def _maj_dd(equity):
    """No-op : DD est calcule dans la boucle principale."""
    pass


# =====================================================================
# Statistiques annuelles
# =====================================================================
def stats_annuelles(resultat, periode_jours):
    """Convertit pnl total en rendement annuel + ratios."""
    pnl_total = resultat["pnl_total_pct"]
    nb_cycles = resultat["nb_cycles"]
    cycles    = resultat["cycles"]
    if not cycles:
        return {
            "rendement_annuel_pct": 0.0,
            "pnl_total_pct": 0.0,
            "nb_cycles": 0,
            "nb_cycles_annuel": 0,
            "gain_moy_par_cycle_pct": 0.0,
            "max_dd_pct": resultat["max_dd_pct"],
            "duree_moy_jours": 0.0,
            "wr_pct": 0.0,
        }

    # Annualisation lineaire (pas de compound car on ne reinvestit pas le pnl)
    rendement_annuel = pnl_total * (365.0 / periode_jours)
    nb_cycles_annuel = nb_cycles * (365.0 / periode_jours)

    # Duree moyenne d'un cycle
    durees_h = []
    for c in cycles:
        if "t_sortie" in c:
            durees_h.append((c["t_sortie"] - c["t_entree"]) / 1000 / 3600)
    duree_moy_h = sum(durees_h) / len(durees_h) if durees_h else 0
    duree_moy_jours = duree_moy_h / 24

    # Win rate
    wins = sum(1 for c in cycles if c.get("pnl_net_pct", 0) > 0)
    wr = 100 * wins / len(cycles)

    # Gain moyen par cycle (net)
    pnls = [c.get("pnl_net_pct", 0) for c in cycles]
    gain_moy = sum(pnls) / len(pnls) if pnls else 0

    return {
        "rendement_annuel_pct": rendement_annuel,
        "pnl_total_pct": pnl_total,
        "nb_cycles": nb_cycles,
        "nb_cycles_annuel": nb_cycles_annuel,
        "gain_moy_par_cycle_pct": gain_moy,
        "max_dd_pct": resultat["max_dd_pct"],
        "duree_moy_jours": duree_moy_jours,
        "wr_pct": wr,
    }


# =====================================================================
# Main
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Backtest funding rate arbitrage.")
    ap.add_argument("--jours", type=int, default=365)
    ap.add_argument("--symboles", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
    ap.add_argument("--seuils", nargs="+", type=float,
                    default=[0.0001, 0.0002, 0.0003, 0.0005],
                    help="Seuils d'entree (rate par 8h). Defaut 0.01-0.05%%")
    ap.add_argument("--seuil-sortie", type=float, default=0.00005,
                    help="Seuil de sortie (defaut 0.005%% par 8h)")
    args = ap.parse_args()

    print("=" * 78)
    print("  BACKTEST FUNDING RATE ARBITRAGE")
    print("=" * 78)
    print(f"  Periode      : {args.jours} jours")
    print(f"  Symboles     : {', '.join(args.symboles)}")
    print(f"  Seuils entree: {[f'{s*100:.3f}%' for s in args.seuils]}")
    print(f"  Seuil sortie : {args.seuil_sortie*100:.3f}%")
    print(f"  Cout/cycle   : {(2*(FEE_SPOT+FEE_FUTURES+2*SLIPPAGE_LEG))*100:.3f}%")
    print()

    # Telechargement
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.jours * 86400 * 1000

    print("Telechargement de l'historique funding rates...")
    histoires = {}
    for sym in args.symboles:
        print(f"  {sym} ...", end=" ", flush=True)
        h = fetch_funding_history(sym, start_ms, end_ms)
        if h is None or len(h) == 0:
            print("FAIL (skip)")
            continue
        histoires[sym] = h
        # Stats descriptives funding
        rates = [e["rate"] for e in h]
        rate_moy = sum(rates) / len(rates) if rates else 0
        rate_max = max(rates) if rates else 0
        rate_min = min(rates) if rates else 0
        nb_pos = sum(1 for r in rates if r > 0)
        print(f"OK ({len(h)} fundings, moy={rate_moy*100:+.4f}%, "
              f"max={rate_max*100:+.4f}%, min={rate_min*100:+.4f}%, "
              f"positif {100*nb_pos/len(rates):.0f}%)")
    print()

    if not histoires:
        print("Aucune donnee. Sortie.")
        sys.exit(1)

    # Backtest pour chaque (symbole, seuil)
    resultats = {}
    for sym, h in histoires.items():
        for seuil in args.seuils:
            r = simuler(h, seuil, args.seuil_sortie)
            s = stats_annuelles(r, args.jours)
            resultats[(sym, seuil)] = s

    # Affichage par symbole
    for sym in histoires.keys():
        print()
        print("=" * 78)
        print(f"  SYMBOLE : {sym}")
        print("=" * 78)
        print(f"{'SEUIL_IN':>10} {'NB/AN':>7} {'WR%':>6} {'GAIN_MOY%':>11} "
              f"{'DUREE_J':>9} {'RDT_AN%':>9} {'DD_MAX%':>9}")
        print("-" * 78)
        for seuil in args.seuils:
            s = resultats[(sym, seuil)]
            mark = ""
            if s["rendement_annuel_pct"] > 5:
                mark = "  <-- bon"
            elif s["rendement_annuel_pct"] > 0:
                mark = "  <-- legerement positif"
            print(f"{seuil*100:>9.3f}% {s['nb_cycles_annuel']:>7.1f} "
                  f"{s['wr_pct']:>5.1f}% {s['gain_moy_par_cycle_pct']:>+10.3f}% "
                  f"{s['duree_moy_jours']:>8.2f} "
                  f"{s['rendement_annuel_pct']:>+8.2f}% "
                  f"{s['max_dd_pct']:>8.2f}%{mark}")

    # Resume global : meilleur seuil par symbole, et somme si on combine
    print()
    print("=" * 78)
    print("  RESUME : meilleur seuil par symbole")
    print("=" * 78)
    print(f"{'SYMBOLE':<10} {'MEILLEUR_SEUIL':<16} {'NB/AN':>7} "
          f"{'RDT_AN%':>9} {'DD_MAX%':>9}")
    print("-" * 78)
    rdt_combine = 0.0
    for sym in histoires.keys():
        best_seuil = max(args.seuils,
                         key=lambda s: resultats[(sym, s)]["rendement_annuel_pct"])
        s = resultats[(sym, best_seuil)]
        rdt_combine += s["rendement_annuel_pct"] / len(histoires)
        print(f"{sym:<10} {best_seuil*100:>4.3f}%{'':<10} "
              f"{s['nb_cycles_annuel']:>7.1f} "
              f"{s['rendement_annuel_pct']:>+8.2f}% "
              f"{s['max_dd_pct']:>8.2f}%")
    print()
    print(f"  Rendement combine (capital reparti egalement) : "
          f"~{rdt_combine:+.2f}%/an")

    # Lecture & recommandations
    print()
    print("=" * 78)
    print("  LECTURE")
    print("=" * 78)
    print("  NB/AN          : nombre de cycles ouverture/fermeture par an")
    print("  WR%            : taux de cycles profitables (apres frais)")
    print("  GAIN_MOY%      : gain net moyen par cycle (apres frais)")
    print("  DUREE_J        : duree moyenne d'un cycle (jours)")
    print("  RDT_AN%        : rendement annualise (sans compound)")
    print("  DD_MAX%        : drawdown maximum sur la periode")
    print()
    print("  REFERENCE :")
    print("    Banque         :  +3%/an")
    print("    Stablecoin lend :  +5%/an")
    print("    Indice SP500   : +10%/an")
    print("    Bon resultat   : +15-25%/an")
    print("    Tres bon       : +25-50%/an")


if __name__ == "__main__":
    main()
