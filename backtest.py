#!/usr/bin/env python3
"""
BACKTEST - Replay des strategies metabot sur historique Binance.

Objectif : verifier statistiquement le comportement reel des 4 strategies
(TREND_FOLLOW / PULLBACK / BREAKOUT / MEAN_REVERSION) sur 3-12 mois
d'historique, sans risquer de capital reel.

Methode :
  1. Telecharge les klines 15m + 1h depuis Binance pour une liste de paires
     liquides (BTC, ETH, SOL, BNB, XRP, AVAX, LINK, DOGE, MATIC, ADA).
  2. Pour chaque bougie 15m fermee, applique strategy.detecter_signal()
     (la MEME fonction utilisee par metabot.py en production).
  3. Quand un signal apparait, simule un trade avec EXACTEMENT les memes
     parametres de risque que metabot.py :
        - SL  = max(1.5% , 1.5 x ATR%)
        - TP  = 3 x ATR% (R:R 1:2)
        - Trailing stop a +2R, 2 x ATR%
        - Break-even a +1R + 0.3% buffer
        - Time stop a 4h (16 bougies 15m)
        - Frais 0.075% + slippage 0.05% (round-trip)
  4. Suit le trade bougie par bougie en utilisant high/low pour determiner
     si SL/TP/trail a ete touche (hypothese pessimiste : si SL et TP dans
     la meme bougie, on suppose que SL a ete touche en premier).
  5. Output : stats par strategie x symbole, courbe d'equity, top/flop trades.

Usage :
    python3 backtest.py                          # 3 mois, 10 symboles
    python3 backtest.py --jours 180              # 6 mois
    python3 backtest.py --symboles BTCUSDT ETHUSDT --jours 90
    python3 backtest.py --strategie BREAKOUT     # une seule strategie
    python3 backtest.py --json resultats.json    # export JSON

Pas d'execution d'ordre. Lecture seule. Ne touche pas a etat_bot.json.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import scanner
import regime
import strategy


# =====================================================================
# Parametres de risque (alignes sur metabot.py)
# =====================================================================
ATR_SL_MULT         = 1.5
ATR_TP_MULT         = 3.0
ATR_TRAIL_MULT      = 2.0
SL_FLOOR_PCT        = 0.015
BREAK_EVEN_BUFFER   = 0.003
MAX_HOLD_CANDLES_15M = 16   # 4h en bougies 15m
FEE_RATE            = 0.00075
SLIPPAGE_RATE       = 0.0005

# Symboles par defaut : top liquides spot Binance
SYMBOLES_DEFAUT = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "ADAUSDT", "DOTUSDT",
]

STRATEGIES_DEFAUT = list(strategy.DISPATCHER.keys())


# =====================================================================
# Telechargement historique (paginate par 1000 klines)
# =====================================================================
def fetch_klines_periode(symbole, intervalle, start_ms, end_ms):
    """Recupere TOUTES les klines entre start_ms et end_ms via pagination.

    Binance limite a 1000 klines par appel. On boucle en avancant
    `startTime` jusqu'a couvrir l'intervalle complet.
    """
    out = []
    cur = start_ms
    base = scanner.BASE_URL
    while cur < end_ms:
        url = (f"{base}/api/v3/klines?symbol={symbole}&interval={intervalle}"
               f"&startTime={cur}&endTime={end_ms}&limit=1000")
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read().decode())
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            print(f"  ! erreur fetch {symbole} {intervalle}: {e}")
            return None
        if not data:
            break
        for b in data:
            try:
                out.append({
                    "t": int(b[0]),
                    "o": float(b[1]),
                    "h": float(b[2]),
                    "l": float(b[3]),
                    "c": float(b[4]),
                    "v": float(b[5]),
                })
            except (ValueError, IndexError):
                continue
        # Avance au timestamp juste apres la derniere bougie recue
        cur = int(data[-1][0]) + 1
        if len(data) < 1000:
            break
        time.sleep(0.15)   # politesse rate-limit
    return out


def index_1h_pour_15m(klines_1h, t_15m):
    """Renvoie l'index de la derniere kline 1h dont open_time <= t_15m.
       Recherche dichotomique. -1 si rien."""
    lo, hi = 0, len(klines_1h) - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if klines_1h[mid]["t"] <= t_15m:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


# =====================================================================
# Simulation d'un trade (SL / TP / trail / break-even / time)
# =====================================================================
def simuler_sortie(entry_price, sl_pct, trail_pct, klines_apres):
    """Replique la logique de metabot.py:monitor_positions().

    Retourne (exit_price, raison, candles_passees, prix_max_atteint).
    Si rien ne ferme le trade dans le lookahead disponible -> sortie au
    close de la derniere bougie avec raison='EOD' (donnees epuisees).
    """
    entry = entry_price
    prix_max = entry
    break_even = False
    tp_px = entry * (1 + sl_pct * (ATR_TP_MULT / ATR_SL_MULT))

    for i, k in enumerate(klines_apres):
        # 1) Mise a jour du plus haut
        if k["h"] > prix_max:
            prix_max = k["h"]

        # 2) Calcul du SL effectif (BE + trail eventuels)
        sl_px = entry * (1 - sl_pct)

        # Break-even active a +1R (utilise le high pour declencher)
        if not break_even and prix_max >= entry * (1 + sl_pct):
            break_even = True
        if break_even:
            sl_px = max(sl_px, entry * (1 + BREAK_EVEN_BUFFER))

        # Trailing active a +2R
        if prix_max >= entry * (1 + sl_pct * 2):
            sl_px = max(sl_px, prix_max * (1 - trail_pct))

        # 3) Time stop ?
        if i + 1 >= MAX_HOLD_CANDLES_15M:
            # Sortie au close de cette bougie
            return k["c"], "TIME", i + 1, prix_max

        # 4) SL touche dans cette bougie ?
        # Hypothese pessimiste : si la bougie a un low <= SL, on sort au SL.
        if k["l"] <= sl_px:
            return sl_px, "SL/TRAIL", i + 1, prix_max

        # 5) TP touche dans cette bougie ?
        if k["h"] >= tp_px:
            return tp_px, "TP", i + 1, prix_max

    # Donnees epuisees : sortie au close de la derniere bougie
    if klines_apres:
        return klines_apres[-1]["c"], "EOD", len(klines_apres), prix_max
    return entry, "EOD", 0, prix_max


# =====================================================================
# Backtest d'une strategie sur un symbole
# =====================================================================
def backtest_strategie(symbole, strategie_name, klines_15m, klines_1h):
    """Boucle bougie par bougie, applique detecter_signal sur des slices."""
    trades = []
    fn = strategy.DISPATCHER.get(strategie_name)
    if fn is None:
        return trades

    # On a besoin d'assez d'historique pour les indicateurs.
    # detecter_signal exclut la bougie en cours via [:-1], donc on passe
    # klines_15m[:i+1] et la fonction analysera la bougie i.
    # Min requis : 100 bougies pour BREAKOUT, 60 pour MR, 31 pour TF, et
    # 61 bougies 1h pour PULLBACK. On commence a i=120 pour etre safe.
    i_start = 120
    i = i_start
    nb_signaux = 0
    while i < len(klines_15m) - 2:
        slice_15m = klines_15m[:i + 1]

        if strategie_name == "PULLBACK":
            t_15m = slice_15m[-1]["t"]
            j = index_1h_pour_15m(klines_1h, t_15m)
            if j < 60:
                i += 1
                continue
            slice_1h = klines_1h[:j + 1]
            sig = strategy.detecter_signal(strategie_name, slice_15m, slice_1h)
        else:
            sig = strategy.detecter_signal(strategie_name, slice_15m)

        if sig is None:
            i += 1
            continue

        nb_signaux += 1
        entry_close = sig["prix"]
        entry_fill = entry_close * (1 + SLIPPAGE_RATE)   # fill cote acheteur
        atr_p = sig["atr_pct"]
        sl_pct = max(SL_FLOOR_PCT, atr_p / 100 * ATR_SL_MULT)
        trail_pct = max(SL_FLOOR_PCT, atr_p / 100 * ATR_TRAIL_MULT)

        # Sortie : on regarde les bougies SUIVANTES (i+1, i+2, ...)
        apres = klines_15m[i + 1 : i + 1 + MAX_HOLD_CANDLES_15M + 2]
        exit_close, raison, dt_candles, prix_max = simuler_sortie(
            entry_fill, sl_pct, trail_pct, apres
        )
        exit_fill = exit_close * (1 - SLIPPAGE_RATE)

        # P&L net frais (round-trip)
        pnl_brut_pct = (exit_fill / entry_fill - 1)
        pnl_net_pct = pnl_brut_pct - 2 * FEE_RATE   # 1 fee a l'achat + 1 a la vente
        gain_max_pct = (prix_max / entry_fill - 1) - FEE_RATE   # MFE realiste

        trades.append({
            "symbole": symbole,
            "strategie": strategie_name,
            "t_entree": int(slice_15m[-1]["t"]),
            "duree_min": dt_candles * 15,
            "entry": entry_fill,
            "exit":  exit_fill,
            "sl_pct": sl_pct * 100,
            "trail_pct": trail_pct * 100,
            "atr_pct": atr_p,
            "pnl_pct": pnl_net_pct * 100,
            "mfe_pct": gain_max_pct * 100,
            "raison": raison,
        })

        # On saute la fin de ce trade pour eviter chevauchement
        i = i + 1 + dt_candles

    return trades


# =====================================================================
# Stats agregees
# =====================================================================
def stats(trades):
    """Calcule les indicateurs cles d'une liste de trades."""
    if not trades:
        return {
            "n": 0, "winrate": 0.0, "pnl_moyen": 0.0, "pnl_total": 0.0,
            "wins": 0, "losses": 0,
            "meilleur": 0.0, "pire": 0.0,
            "duree_moy": 0.0,
            "expectancy": 0.0,
            "ratio_gain_perte": 0.0,
            "max_dd": 0.0,
        }
    n = len(trades)
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    wr = len(wins) / n * 100
    expectancy = (wr / 100) * avg_win + (1 - wr / 100) * avg_loss
    ratio = abs(avg_win / avg_loss) if avg_loss < 0 else 0

    # Max drawdown sur l'equity (en supposant 10% du capital par trade)
    # Approximation : ici on agrege en %. Le drawdown reel sur l'equity
    # depend du sizing, mais ce DD% est un proxy raisonnable.
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    return {
        "n": n,
        "winrate": wr,
        "pnl_moyen": sum(pnls) / n,
        "pnl_total": sum(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "meilleur": max(pnls),
        "pire": min(pnls),
        "duree_moy": sum(t["duree_min"] for t in trades) / n,
        "expectancy": expectancy,
        "ratio_gain_perte": ratio,
        "max_dd": max_dd,
    }


# =====================================================================
# Rapport texte
# =====================================================================
def imprimer_rapport(trades_par_strat, jours, symboles):
    print()
    print("=" * 78)
    print(f"BACKTEST METABOT - {jours} jours - {len(symboles)} symboles")
    print(f"Symboles : {', '.join(symboles)}")
    print("=" * 78)

    # Stats par strategie (tous symboles confondus)
    print()
    print(f"{'STRATEGIE':<18} {'N':>5} {'WR%':>7} {'PNL_MOY%':>9} "
          f"{'EXPEC%':>8} {'G/L':>6} {'MAX_DD%':>8} {'TOTAL%':>8}")
    print("-" * 78)
    total_trades = 0
    total_pnl = 0.0
    for strat, trades in trades_par_strat.items():
        s = stats(trades)
        total_trades += s["n"]
        total_pnl += s["pnl_total"]
        print(f"{strat:<18} {s['n']:>5} {s['winrate']:>6.1f}% "
              f"{s['pnl_moyen']:>+8.2f}% {s['expectancy']:>+7.2f}% "
              f"{s['ratio_gain_perte']:>5.2f} {s['max_dd']:>7.2f}% "
              f"{s['pnl_total']:>+7.2f}%")
    print("-" * 78)
    print(f"{'TOTAL':<18} {total_trades:>5} {'':>7} {'':>9} {'':>8} "
          f"{'':>6} {'':>8} {total_pnl:>+7.2f}%")

    # Breakdown par strategie x symbole
    print()
    print("Detail par symbole :")
    print(f"{'STRATEGIE':<16} {'SYMBOLE':<10} {'N':>4} {'WR%':>6} "
          f"{'PNL_MOY%':>9} {'TOTAL%':>8}")
    print("-" * 60)
    for strat, all_trades in trades_par_strat.items():
        par_sym = {}
        for t in all_trades:
            par_sym.setdefault(t["symbole"], []).append(t)
        for sym in sorted(par_sym.keys()):
            s = stats(par_sym[sym])
            print(f"{strat:<16} {sym:<10} {s['n']:>4} {s['winrate']:>5.1f}% "
                  f"{s['pnl_moyen']:>+8.2f}% {s['pnl_total']:>+7.2f}%")

    # Top / flop trades
    tous = [t for ts in trades_par_strat.values() for t in ts]
    if tous:
        tous_tri = sorted(tous, key=lambda t: t["pnl_pct"])
        print()
        print("5 pires trades :")
        for t in tous_tri[:5]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(t["t_entree"] / 1000))
            print(f"  {ts}  {t['symbole']:<10} {t['strategie']:<15} "
                  f"{t['pnl_pct']:>+6.2f}%  duree {t['duree_min']}min  raison: {t['raison']}")
        print()
        print("5 meilleurs trades :")
        for t in tous_tri[-5:]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(t["t_entree"] / 1000))
            print(f"  {ts}  {t['symbole']:<10} {t['strategie']:<15} "
                  f"{t['pnl_pct']:>+6.2f}%  duree {t['duree_min']}min  raison: {t['raison']}")

    # Diagnostic global
    print()
    print("=" * 78)
    print("DIAGNOSTIC :")
    if total_trades == 0:
        print("  Aucun trade declenche. Verifier la periode et les symboles.")
    else:
        wr_global = sum(1 for t in tous if t["pnl_pct"] > 0) / total_trades * 100
        rythme = total_trades / jours
        print(f"  Trades par jour (moy)     : {rythme:.2f}")
        print(f"  Win rate global           : {wr_global:.1f}%")
        print(f"  P&L cumule (somme des %)  : {total_pnl:+.2f}%")
        if total_pnl > 0:
            print("  -> Strategie globalement rentable sur la periode.")
        else:
            print("  -> Strategie globalement perdante sur la periode.")
    print("=" * 78)


# =====================================================================
# CLI
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Backtest des strategies metabot.")
    ap.add_argument("--jours", type=int, default=90,
                    help="Profondeur d'historique (defaut 90 jours)")
    ap.add_argument("--symboles", nargs="+", default=SYMBOLES_DEFAUT,
                    help=f"Symboles a tester (defaut: {' '.join(SYMBOLES_DEFAUT)})")
    ap.add_argument("--strategie", default=None,
                    help="Strategie unique a tester (defaut: toutes)")
    ap.add_argument("--json", default=None,
                    help="Si fourni, exporte tous les trades dans ce fichier JSON")
    args = ap.parse_args()

    strategies = [args.strategie.upper()] if args.strategie else STRATEGIES_DEFAUT
    for s in strategies:
        if s not in strategy.DISPATCHER:
            print(f"Strategie inconnue: {s}")
            sys.exit(1)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.jours * 86400 * 1000

    print(f"Telechargement de {args.jours} jours pour {len(args.symboles)} symboles...")
    klines_par_sym = {}
    for sym in args.symboles:
        print(f"  {sym} 15m + 1h ...", end=" ", flush=True)
        kl15 = fetch_klines_periode(sym, "15m", start_ms, end_ms)
        if not kl15:
            print("echec 15m, skip")
            continue
        kl1h = fetch_klines_periode(sym, "1h", start_ms, end_ms)
        if not kl1h:
            print("echec 1h, skip")
            continue
        klines_par_sym[sym] = {"15m": kl15, "1h": kl1h}
        print(f"OK ({len(kl15)} bougies 15m, {len(kl1h)} bougies 1h)")

    if not klines_par_sym:
        print("Aucun symbole charge, arret.")
        sys.exit(1)

    print()
    print("Backtest en cours...")
    trades_par_strat = {s: [] for s in strategies}
    for strat in strategies:
        for sym, k in klines_par_sym.items():
            ts = backtest_strategie(sym, strat, k["15m"], k["1h"])
            trades_par_strat[strat].extend(ts)
            print(f"  {strat:<16} {sym:<10} -> {len(ts)} trades")

    imprimer_rapport(trades_par_strat, args.jours, list(klines_par_sym.keys()))

    if args.json:
        export = {
            "jours": args.jours,
            "symboles": list(klines_par_sym.keys()),
            "trades": [t for ts in trades_par_strat.values() for t in ts],
        }
        with open(args.json, "w") as f:
            json.dump(export, f, indent=2)
        print(f"\nTrades exportes vers {args.json}")


if __name__ == "__main__":
    main()
