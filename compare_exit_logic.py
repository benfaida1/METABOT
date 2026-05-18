#!/usr/bin/env python3
"""
compare_exit_logic.py

Compare 4 configurations de logique de sortie pour tester si le trailing
stop et/ou le break-even sont responsables de couper les gros gagnants
avant qu'ils n'atteignent leur TP plein.

Scenarios testes :
    BASELINE       : trailing + break-even (config actuelle)
    NO_TRAIL       : break-even seul, pas de trailing
    NO_BE          : trailing seul, pas de break-even
    NO_TRAIL_NO_BE : SL fixe + TP fixe uniquement (pure 1:2 R:R)
    NO_TRAIL_NO_BE_NO_TIME : meme chose mais sans time stop non plus

Astuce : on patche les constantes du module backtest pour desactiver le
trailing (ATR_TRAIL_MULT enorme -> trail_pct trop large -> jamais active)
et le BE (BREAK_EVEN_BUFFER tres negatif -> max(sl_px, entry*-1) = sl_px).

Usage :
    python compare_exit_logic.py
    python compare_exit_logic.py --jours 365
    python compare_exit_logic.py --symboles BTCUSDT --strategies TREND_FOLLOW
"""
import argparse
import sys
import time

import backtest


SCENARIOS = [
    # (label, ATR_TRAIL_MULT, BREAK_EVEN_BUFFER, MAX_HOLD_CANDLES_15M)
    ("BASELINE",                2.0,     0.003,    16),
    ("NO_TRAIL",                99999.0, 0.003,    16),
    ("NO_BE",                   2.0,    -99999.0,  16),
    ("NO_TRAIL_NO_BE",          99999.0,-99999.0,  16),
    ("NO_TRAIL_NO_BE_NO_TIME",  99999.0,-99999.0,  99999),
]


def stats_pour_trades(trades):
    if not trades:
        return None
    n = len(trades)
    wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] <= 0]
    wr = 100 * len(wins) / n
    gain_moy = sum(wins) / len(wins) if wins else 0
    perte_moy = sum(losses) / len(losses) if losses else 0
    esperance = (wr / 100) * gain_moy + (1 - wr / 100) * perte_moy
    total = sum(t["pnl_pct"] for t in trades)

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--jours", type=int, default=365)
    ap.add_argument("--symboles", nargs="+",
                    default=["BTCUSDT", "ETHUSDT"])
    ap.add_argument("--strategies", nargs="+",
                    default=["TREND_FOLLOW", "PULLBACK", "BREAKOUT"])
    ap.add_argument("--window-15m", type=int, default=200)
    ap.add_argument("--window-1h", type=int, default=80)
    args = ap.parse_args()

    print("=" * 78)
    print("  COMPARAISON DES LOGIQUES DE SORTIE")
    print("=" * 78)
    print(f"  Periode    : {args.jours} jours")
    print(f"  Symboles   : {', '.join(args.symboles)}")
    print(f"  Strategies : {', '.join(args.strategies)}")
    print(f"  Scenarios  : {len(SCENARIOS)}")
    print()

    # Telechargement
    print("Telechargement de l'historique...")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.jours * 86400 * 1000
    klines_par_sym = {}
    for sym in args.symboles:
        print(f"  {sym} ...", end=" ", flush=True)
        k15 = backtest.fetch_klines_periode(sym, "15m", start_ms, end_ms)
        k1h = backtest.fetch_klines_periode(sym, "1h", start_ms, end_ms)
        if not k15 or not k1h:
            print("FAIL")
            sys.exit(1)
        klines_par_sym[sym] = (k15, k1h)
        print(f"OK ({len(k15)} bougies 15m)")
    print()

    # Backtest pour chaque scenario
    resultats = {}
    for (label, trail_mult, be_buf, max_hold) in SCENARIOS:
        backtest.ATR_TRAIL_MULT      = trail_mult
        backtest.BREAK_EVEN_BUFFER   = be_buf
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
            s = stats_pour_trades(all_trades)
            resultats[(label, strat)] = s
            print(f"  done : {label:<26} {strat:<16} N={s['n'] if s else 0}")

    # Affichage : un tableau par strategie
    for strat in args.strategies:
        print()
        print("=" * 78)
        print(f"  STRATEGIE : {strat}")
        print("=" * 78)
        print(f"{'SCENARIO':<26} {'N':>5} {'WR%':>6} {'GAIN%':>8} {'PERTE%':>8} {'ESPER%':>8} {'TOTAL%':>9}  {'TP':>4} {'SL':>4} {'TIME':>5}")
        print("-" * 78)
        for (label, _, _, _) in SCENARIOS:
            s = resultats.get((label, strat))
            if not s:
                print(f"{label:<26} (no trades)")
                continue
            mark = "  <-- GAGNANT" if s["esperance"] > 0 else ""
            print(f"{label:<26} {s['n']:>5} {s['wr']:>5.1f}% "
                  f"{s['gain_moy']:>+7.2f}% {s['perte_moy']:>+7.2f}% "
                  f"{s['esperance']:>+7.3f}% {s['total']:>+8.1f}%  "
                  f"{s['tp']:>4} {s['sl']:>4} {s['time']:>5}{mark}")

    # Resume final
    print()
    print("=" * 78)
    print("  RESUME : meilleur scenario par strategie")
    print("=" * 78)
    print(f"{'STRATEGIE':<16} {'MEILLEUR':<28} {'ESPERANCE':>11} {'vs BASELINE':>14}")
    print("-" * 78)
    for strat in args.strategies:
        best_label = max((s[0] for s in SCENARIOS),
                         key=lambda lbl: resultats.get((lbl, strat), {}).get("esperance", -999))
        best_e = resultats[(best_label, strat)]["esperance"]
        base_e = resultats[("BASELINE", strat)]["esperance"]
        delta = best_e - base_e
        sign = "+" if delta >= 0 else ""
        print(f"{strat:<16} {best_label:<28} {best_e:>+10.3f}% {sign}{delta:>12.3f}%")

    print()
    print("Lecture du tableau :")
    print("  - Si NO_TRAIL_NO_BE bat BASELINE : c'est le trailing+BE qui coupent les gagnants.")
    print("    -> Solution : desactiver trailing et break-even dans metabot.py")
    print("  - Si BASELINE reste le meilleur : trailing+BE etaient bien calibres,")
    print("    et le probleme vient des signaux d'entree.")
    print()
    print("Note : Le test patche backtest.ATR_TRAIL_MULT et backtest.BREAK_EVEN_BUFFER")
    print("en memoire pour simuler les configurations. Le code source n'est pas modifie.")


if __name__ == "__main__":
    main()
