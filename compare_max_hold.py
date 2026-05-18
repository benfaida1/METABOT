#!/usr/bin/env python3
"""
compare_max_hold.py

Compare l'effet du TIME stop (MAX_HOLD_CANDLES_15M) sur les performances
des strategies. Re-utilise les fonctions de backtest.py mais patche la
constante avant chaque scenario.

Idee : si on enleve / allonge le time stop, les trades qui auraient atteint
leur TP ne seront plus coupes prematurement. On veut voir si ca transforme
l'esperance negative en positive.

Usage :
    python compare_max_hold.py                                 # par defaut : BTC+ETH, 3 strats, 90 jours
    python compare_max_hold.py --jours 365                     # full year
    python compare_max_hold.py --symboles BTCUSDT --strategie TREND_FOLLOW
    python compare_max_hold.py --max-holds 16 32 96 480 99999  # custom scenarios
"""
import argparse
import sys
import time

import backtest  # le module existant - on va patcher ses constantes


# Valeurs de MAX_HOLD a tester (en bougies 15m).
# 16=4h (defaut actuel), 32=8h, 96=24h, 480=5j, 99999=desactive
DEFAULT_MAX_HOLDS = [16, 32, 96, 480, 99999]


def label_max_hold(n):
    """Convertit un nombre de bougies 15m en label lisible."""
    if n >= 99999:
        return "OFF (no limit)"
    minutes = n * 15
    if minutes < 60:
        return f"{minutes}min"
    if minutes < 1440:
        return f"{minutes/60:.0f}h"
    return f"{minutes/1440:.1f}j"


def stats_pour_trades(trades):
    """Calcule WR, gain_moy, perte_moy, esperance, total."""
    if not trades:
        return {"n": 0, "wr": 0, "gain_moy": 0, "perte_moy": 0,
                "esperance": 0, "total": 0, "tp": 0, "sl": 0, "time": 0, "eod": 0}

    n = len(trades)
    wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] <= 0]
    wr = 100 * len(wins) / n
    gain_moy = sum(wins) / len(wins) if wins else 0
    perte_moy = sum(losses) / len(losses) if losses else 0
    esperance = (wr / 100) * gain_moy + (1 - wr / 100) * perte_moy
    total = sum(t["pnl_pct"] for t in trades)

    # Compte des raisons de sortie
    reasons = {"TP": 0, "SL/TRAIL": 0, "TIME": 0, "EOD": 0}
    for t in trades:
        r = t.get("raison", "?")
        if r in reasons:
            reasons[r] += 1

    return {
        "n": n, "wr": wr, "gain_moy": gain_moy, "perte_moy": perte_moy,
        "esperance": esperance, "total": total,
        "tp": reasons["TP"], "sl": reasons["SL/TRAIL"],
        "time": reasons["TIME"], "eod": reasons["EOD"],
    }


def main():
    ap = argparse.ArgumentParser(description="Compare l'effet du TIME stop.")
    ap.add_argument("--jours", type=int, default=90,
                    help="Periode (defaut 90 jours pour aller vite)")
    ap.add_argument("--symboles", nargs="+",
                    default=["BTCUSDT", "ETHUSDT"],
                    help="Symboles a tester (defaut BTCUSDT ETHUSDT)")
    ap.add_argument("--strategies", nargs="+",
                    default=["TREND_FOLLOW", "PULLBACK", "BREAKOUT"],
                    help="Strategies a tester (defaut sans MEAN_REVERSION)")
    ap.add_argument("--max-holds", nargs="+", type=int,
                    default=DEFAULT_MAX_HOLDS,
                    help="Valeurs de MAX_HOLD a tester (en bougies 15m)")
    ap.add_argument("--window-15m", type=int, default=200,
                    help="Window 15m (defaut 200, rapide)")
    ap.add_argument("--window-1h", type=int, default=80)
    args = ap.parse_args()

    print("=" * 78)
    print("  COMPARAISON DU TIME STOP")
    print("=" * 78)
    print(f"  Periode    : {args.jours} jours")
    print(f"  Symboles   : {', '.join(args.symboles)}")
    print(f"  Strategies : {', '.join(args.strategies)}")
    print(f"  Scenarios  : {[label_max_hold(n) for n in args.max_holds]}")
    print(f"  Window     : 15m={args.window_15m}  1h={args.window_1h}")
    print()

    # ---- Telechargement des klines (une seule fois pour tous les scenarios) --
    print("Telechargement de l'historique...")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.jours * 86400 * 1000

    klines_par_sym = {}
    for sym in args.symboles:
        print(f"  {sym} 15m + 1h ...", end=" ", flush=True)
        k15 = backtest.fetch_klines_periode(sym, "15m", start_ms, end_ms)
        k1h = backtest.fetch_klines_periode(sym, "1h", start_ms, end_ms)
        if not k15 or not k1h:
            print("FAIL")
            sys.exit(1)
        klines_par_sym[sym] = (k15, k1h)
        print(f"OK ({len(k15)} bougies 15m, {len(k1h)} bougies 1h)")
    print()

    # ---- Backtest pour chaque (max_hold, strategie, symbole) ---------------
    # Cle = (max_hold, strategie) -> liste de tous les trades sur tous les symboles
    resultats = {}

    for max_hold in args.max_holds:
        # PATCH la constante du module backtest
        backtest.MAX_HOLD_CANDLES_15M = max_hold

        for strat in args.strategies:
            all_trades = []
            for sym, (k15, k1h) in klines_par_sym.items():
                trades = backtest.backtest_strategie(
                    sym, strat, k15, k1h,
                    window_15m=args.window_15m,
                    window_1h=args.window_1h,
                )
                all_trades.extend(trades)
            resultats[(max_hold, strat)] = stats_pour_trades(all_trades)
            print(f"  done : max_hold={label_max_hold(max_hold):<14}  {strat:<16}  N={len(all_trades)}")

    # ---- Affichage du tableau comparatif ----------------------------------
    for strat in args.strategies:
        print()
        print("=" * 78)
        print(f"  STRATEGIE : {strat}")
        print("=" * 78)
        print(f"{'MAX_HOLD':<15} {'N':>5} {'WR%':>6} {'GAIN%':>8} {'PERTE%':>8} {'ESPER%':>8} {'TOTAL%':>9}  {'TP':>4} {'SL':>4} {'TIME':>5}")
        print("-" * 78)
        for max_hold in args.max_holds:
            s = resultats[(max_hold, strat)]
            mark = ""
            if s["esperance"] > 0:
                mark = "  <-- GAGNANT"
            print(f"{label_max_hold(max_hold):<15} {s['n']:>5} {s['wr']:>5.1f}% "
                  f"{s['gain_moy']:>+7.2f}% {s['perte_moy']:>+7.2f}% "
                  f"{s['esperance']:>+7.3f}% {s['total']:>+8.1f}%  "
                  f"{s['tp']:>4} {s['sl']:>4} {s['time']:>5}{mark}")

    print()
    print("=" * 78)
    print("  RESUME : meilleurs scenarios par strategie")
    print("=" * 78)
    print(f"{'STRATEGIE':<16} {'MEILLEUR MAX_HOLD':<20} {'ESPERANCE':>11} {'GAIN vs DEFAUT (16)':>22}")
    print("-" * 78)
    for strat in args.strategies:
        # Trouve le meilleur max_hold par esperance
        best = max(args.max_holds, key=lambda m: resultats[(m, strat)]["esperance"])
        best_e = resultats[(best, strat)]["esperance"]
        ref_e = resultats[(16, strat)]["esperance"] if 16 in args.max_holds else 0
        delta = best_e - ref_e
        print(f"{strat:<16} {label_max_hold(best):<20} {best_e:>+10.3f}% {delta:>+21.3f}%")

    print()
    print("Conclusion :")
    print("  - Si ESPERANCE devient positive avec un MAX_HOLD plus long,")
    print("    c'est LA modification a faire dans backtest.py et metabot.py.")
    print("  - Si ESPERANCE reste negative partout, le probleme vient des")
    print("    signaux d'entree (a creuser ensuite).")


if __name__ == "__main__":
    main()
