#!/usr/bin/env python3
"""
analyze_results.py

Analyse le fichier backtest_FULL_aggregated.json produit par run_full_backtest.ps1
et imprime 3 tableaux utiles pour comprendre OU et POURQUOI les strategies
gagnent ou perdent.

Usage :
    python analyze_results.py
    python analyze_results.py --json backtest_FULL_aggregated.json
"""
import argparse
import json
import sys
from collections import defaultdict, Counter


def charger(path):
    try:
        # utf-8-sig gere le BOM eventuel ecrit par PowerShell
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Fichier introuvable : {path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"JSON invalide dans {path} : {e}")
        sys.exit(1)


def tableau_par_paire_strategie(trades):
    """N, WR, PnL_moy, Total par (symbole, strategie)."""
    stats = defaultdict(lambda: {"n": 0, "wins": 0, "sum": 0.0})
    for t in trades:
        k = (t["symbole"], t["strategie"])
        stats[k]["n"] += 1
        stats[k]["sum"] += t["pnl_pct"]
        if t["pnl_pct"] > 0:
            stats[k]["wins"] += 1

    print("\n" + "=" * 70)
    print("  TABLEAU 1 : RESULTATS PAR PAIRE x STRATEGIE")
    print("=" * 70)
    print(f"{'SYMBOLE':<10} {'STRATEGIE':<16} {'N':>5} {'WR%':>7} {'PnL_moy%':>10} {'TOTAL%':>10}")
    print("-" * 70)

    for k in sorted(stats.keys()):
        s = stats[k]
        wr = 100 * s["wins"] / s["n"] if s["n"] else 0
        avg = s["sum"] / s["n"] if s["n"] else 0
        # Mise en evidence des combos gagnantes
        marker = " <--" if s["sum"] > 0 else ""
        print(f"{k[0]:<10} {k[1]:<16} {s['n']:>5} {wr:>6.1f}% {avg:>+9.3f}% {s['sum']:>+9.2f}%{marker}")

    # Top 5 best et top 5 worst combos
    sorted_by_total = sorted(stats.items(), key=lambda kv: kv[1]["sum"], reverse=True)
    print("\n  TOP 5 MEILLEURS (par total cumule)")
    for (sym, strat), s in sorted_by_total[:5]:
        wr = 100 * s["wins"] / s["n"]
        print(f"    {sym:<10} {strat:<16} N={s['n']:>4}  WR={wr:>5.1f}%  TOT={s['sum']:+.2f}%")

    print("\n  TOP 5 PIRES (par total cumule)")
    for (sym, strat), s in sorted_by_total[-5:]:
        wr = 100 * s["wins"] / s["n"]
        print(f"    {sym:<10} {strat:<16} N={s['n']:>4}  WR={wr:>5.1f}%  TOT={s['sum']:+.2f}%")


def tableau_raisons_sortie(trades):
    """Compte les raisons de sortie par strategie."""
    print("\n" + "=" * 70)
    print("  TABLEAU 2 : RAISONS DE SORTIE PAR STRATEGIE")
    print("=" * 70)
    print("  (TP=take profit, SL/TRAIL=stop loss ou trailing stop,")
    print("   TIME=fermeture sur time stop 4h, EOD=fin des donnees)")
    print()

    by_strat = defaultdict(Counter)
    pnl_by_reason = defaultdict(lambda: defaultdict(list))

    for t in trades:
        strat = t["strategie"]
        raison = t.get("raison", "?")
        by_strat[strat][raison] += 1
        pnl_by_reason[strat][raison].append(t["pnl_pct"])

    for strat in sorted(by_strat.keys()):
        total = sum(by_strat[strat].values())
        print(f"\n  {strat} (N={total})")
        print(f"    {'RAISON':<12} {'N':>6} {'%':>7} {'PnL_moy%':>11}")
        print(f"    {'-'*40}")
        for raison, n in sorted(by_strat[strat].items(), key=lambda kv: -kv[1]):
            pct = 100 * n / total
            pnls = pnl_by_reason[strat][raison]
            avg = sum(pnls) / len(pnls) if pnls else 0
            print(f"    {raison:<12} {n:>6} {pct:>6.1f}% {avg:>+10.3f}%")


def tableau_par_strategie_global(trades):
    """Resume global par strategie avec metriques utiles."""
    print("\n" + "=" * 70)
    print("  TABLEAU 3 : RESUME GLOBAL PAR STRATEGIE")
    print("=" * 70)

    by_strat = defaultdict(list)
    for t in trades:
        by_strat[t["strategie"]].append(t["pnl_pct"])

    print(f"{'STRATEGIE':<16} {'N':>6} {'WR%':>7} {'GAIN_MOY%':>11} {'PERTE_MOY%':>12} {'ESPERANCE%':>12}")
    print("-" * 70)
    for strat in sorted(by_strat.keys()):
        pnls = by_strat[strat]
        n = len(pnls)
        if n == 0:
            continue
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = 100 * len(wins) / n
        gain_moy = sum(wins) / len(wins) if wins else 0
        perte_moy = sum(losses) / len(losses) if losses else 0
        esperance = (wr/100) * gain_moy + (1 - wr/100) * perte_moy
        print(f"{strat:<16} {n:>6} {wr:>6.1f}% {gain_moy:>+10.3f}% {perte_moy:>+11.3f}% {esperance:>+11.3f}%")

    print()
    print("  Esperance = WR x gain_moy + (1-WR) x perte_moy")
    print("  -> Si negatif : strategie perdante en moyenne")
    print("  -> Si positif : strategie gagnante en moyenne")


def main():
    ap = argparse.ArgumentParser(description="Analyse des resultats de backtest.")
    ap.add_argument("--json", default="backtest_FULL_aggregated.json",
                    help="Fichier JSON agrege a analyser")
    args = ap.parse_args()

    data = charger(args.json)
    trades = data.get("trades", [])

    if not trades:
        print("Aucun trade dans le fichier.")
        sys.exit(1)

    print(f"\nAnalyse de {len(trades)} trades sur {data.get('jours', '?')} jours")
    print(f"Symboles : {', '.join(data.get('symboles', []))}")
    print(f"Strategies : {', '.join(data.get('strategies', []))}")

    tableau_par_strategie_global(trades)
    tableau_par_paire_strategie(trades)
    tableau_raisons_sortie(trades)


if __name__ == "__main__":
    main()
