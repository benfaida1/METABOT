#!/usr/bin/env python3
"""
backtest_trend_d1.py

Backtest de la strategie Trend Following sur BTC en Daily.

Logique simple et eprouvee :
    ENTREE : Close > MA50  ET  MA50 > MA200  ET  Close > plus haut 20j
    SORTIE : Close < MA50

Aucun levier, aucun short, aucun Futures. Position spot uniquement.
Quand pas en position : 100% cash. Quand en position : 100% en BTC.

Frais : 0.1% par trade (taker spot Binance, sans BNB discount)

Usage :
    python backtest_trend_d1.py
    python backtest_trend_d1.py --capital 1000
    python backtest_trend_d1.py --symbole ETHUSDT
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


BASE_URL = "https://api.binance.com"
FEE_RATE = 0.001    # 0.1% spot taker (conservateur)


# =====================================================================
# Telechargement des klines daily
# =====================================================================
def fetch_klines_daily(symbole, start_ms, end_ms):
    """Recupere toutes les klines 1d entre start_ms et end_ms."""
    out = []
    cur = start_ms
    while cur < end_ms:
        url = (f"{BASE_URL}/api/v3/klines?symbol={symbole}&interval=1d"
               f"&startTime={cur}&endTime={end_ms}&limit=1000")
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read().decode())
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            print(f"  ! erreur fetch {symbole}: {e}")
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
        cur = int(data[-1][0]) + 1
        if len(data) < 1000:
            break
        time.sleep(0.15)
    return out


# =====================================================================
# Backtest
# =====================================================================
def backtest_trend(klines, capital_init=10000.0):
    """Trend following daily.

    Entree : close > MA50 ET MA50 > MA200 ET close > max(high des 20j precedents)
    Sortie : close < MA50
    """
    capital = capital_init
    position = None
    trades = []
    equity_curve = []     # liste de (timestamp_ms, equity_USD)

    # On a besoin d'au moins 200 jours d'historique pour la MA200
    MIN_HISTORY = 200

    # On precalcule les closes pour eviter de recompter
    closes_all = [k["c"] for k in klines]

    for i in range(MIN_HISTORY, len(klines)):
        bar = klines[i]
        close = bar["c"]

        # Indicateurs
        ma50  = sum(closes_all[i - 50 + 1 : i + 1]) / 50
        ma200 = sum(closes_all[i - 200 + 1 : i + 1]) / 200
        # Plus haut des 20 jours PRECEDENTS (pas inclus le jour courant)
        high20 = max(k["h"] for k in klines[i - 20 : i])

        if position is None:
            # Pas en position : chercher entree
            if close > ma50 and ma50 > ma200 and close > high20:
                # Achat
                fee = capital * FEE_RATE
                units = (capital - fee) / close
                position = {
                    "entry_t": bar["t"],
                    "entry_price": close,
                    "entry_capital": capital,
                    "units": units,
                }
                capital = 0
        else:
            # En position : chercher sortie
            if close < ma50:
                # Vente
                gross = position["units"] * close
                fee = gross * FEE_RATE
                capital = gross - fee
                pnl_pct = (capital / position["entry_capital"] - 1) * 100
                trades.append({
                    "entry_t": position["entry_t"],
                    "exit_t": bar["t"],
                    "entry_price": position["entry_price"],
                    "exit_price": close,
                    "pnl_pct": pnl_pct,
                    "duration_days": int((bar["t"] - position["entry_t"]) / 86400000),
                })
                position = None

        # Equity courante (mark-to-market)
        if position:
            equity_curve.append((bar["t"], position["units"] * close))
        else:
            equity_curve.append((bar["t"], capital))

    # Position encore ouverte ? On marque a la derniere bougie pour le calcul
    if position:
        final_close = klines[-1]["c"]
        gross = position["units"] * final_close
        # On ne ferme PAS pour de vrai (on dit juste : ca vaut ca maintenant)
        valeur_finale = gross
    else:
        valeur_finale = capital

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "capital_init": capital_init,
        "valeur_finale": valeur_finale,
        "rendement_total_pct": (valeur_finale / capital_init - 1) * 100,
        "position_ouverte_a_la_fin": position is not None,
    }


# =====================================================================
# Stats
# =====================================================================
def analyser(resultat, klines, capital_init):
    """Calcule toutes les metriques utiles."""
    trades = resultat["trades"]
    equity = resultat["equity_curve"]

    # Drawdown max
    peak = capital_init
    max_dd_pct = 0
    max_dd_t = None
    for (t, eq) in equity:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd_pct:
            max_dd_pct = dd
            max_dd_t = t

    # Performance par annee
    perf_annuelle = {}
    eq_par_annee = {}
    for (t, eq) in equity:
        an = datetime.fromtimestamp(t / 1000, tz=timezone.utc).year
        eq_par_annee.setdefault(an, []).append(eq)

    annees = sorted(eq_par_annee.keys())
    for an in annees:
        eqs = eq_par_annee[an]
        debut = eqs[0]
        fin   = eqs[-1]
        perf = (fin / debut - 1) * 100
        perf_annuelle[an] = {"debut": debut, "fin": fin, "perf_pct": perf}

    # Buy & Hold de reference (sur la meme periode)
    prix_debut = klines[200]["c"]   # debut backtest = bougie 200
    prix_fin = klines[-1]["c"]
    bh_total = (prix_fin / prix_debut - 1) * 100

    # Drawdown Buy & Hold
    prix_max = klines[200]["h"]
    bh_dd_max = 0
    for k in klines[200:]:
        if k["h"] > prix_max:
            prix_max = k["h"]
        dd = (prix_max - k["l"]) / prix_max * 100
        if dd > bh_dd_max:
            bh_dd_max = dd

    # Stats trades
    if trades:
        pnls = [t["pnl_pct"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        nb = len(trades)
        wr = 100 * len(wins) / nb
        gain_moy = sum(wins) / len(wins) if wins else 0
        perte_moy = sum(losses) / len(losses) if losses else 0
        meilleur = max(pnls)
        pire = min(pnls)
        duree_moy = sum(t["duration_days"] for t in trades) / nb
    else:
        nb = wr = gain_moy = perte_moy = meilleur = pire = duree_moy = 0

    # Rendement annualise
    nb_annees = (equity[-1][0] - equity[0][0]) / (365.25 * 86400 * 1000)
    if nb_annees > 0:
        rendement_annuel = ((resultat["valeur_finale"] / capital_init) ** (1 / nb_annees) - 1) * 100
        bh_annuel = ((prix_fin / prix_debut) ** (1 / nb_annees) - 1) * 100
    else:
        rendement_annuel = 0
        bh_annuel = 0

    return {
        "nb_trades": nb,
        "win_rate": wr,
        "gain_moy_pct": gain_moy,
        "perte_moy_pct": perte_moy,
        "meilleur_trade_pct": meilleur,
        "pire_trade_pct": pire,
        "duree_moy_jours": duree_moy,
        "rendement_total_pct": resultat["rendement_total_pct"],
        "rendement_annuel_pct": rendement_annuel,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_t": max_dd_t,
        "nb_annees": nb_annees,
        "perf_annuelle": perf_annuelle,
        "bh_total_pct": bh_total,
        "bh_annuel_pct": bh_annuel,
        "bh_drawdown_pct": bh_dd_max,
    }


# =====================================================================
# Affichage
# =====================================================================
def afficher(stats, symbole, capital_init, resultat):
    print()
    print("=" * 78)
    print(f"  RESULTATS TREND FOLLOWING D1 - {symbole}")
    print("=" * 78)
    print(f"  Capital initial      : {capital_init:>12.2f} $")
    print(f"  Capital final        : {resultat['valeur_finale']:>12.2f} $")
    print(f"  Rendement total      : {stats['rendement_total_pct']:>+12.2f}%")
    print(f"  Rendement annualise  : {stats['rendement_annuel_pct']:>+12.2f}%/an")
    print(f"  Periode              : {stats['nb_annees']:>12.2f} annees")
    print(f"  Drawdown max         : {stats['max_drawdown_pct']:>12.2f}%")
    if stats['max_drawdown_t']:
        dt = datetime.fromtimestamp(stats['max_drawdown_t'] / 1000, tz=timezone.utc)
        print(f"  Date du DD max       : {dt.strftime('%Y-%m-%d')}")
    if resultat["position_ouverte_a_la_fin"]:
        print(f"  Position             : OUVERTE a la fin du backtest")

    print()
    print(f"  Trades               : {stats['nb_trades']}")
    if stats['nb_trades'] > 0:
        print(f"  Win rate             : {stats['win_rate']:>12.1f}%")
        print(f"  Gain moyen trade     : {stats['gain_moy_pct']:>+12.2f}%")
        print(f"  Perte moyenne trade  : {stats['perte_moy_pct']:>+12.2f}%")
        print(f"  Meilleur trade       : {stats['meilleur_trade_pct']:>+12.2f}%")
        print(f"  Pire trade           : {stats['pire_trade_pct']:>+12.2f}%")
        print(f"  Duree moy. d'un trade: {stats['duree_moy_jours']:>12.0f} jours")

    print()
    print("=" * 78)
    print("  COMPARAISON AVEC BUY & HOLD")
    print("=" * 78)
    print(f"{'METRIQUE':<25} {'TREND D1':>15} {'BUY & HOLD':>15} {'AVANTAGE':>15}")
    print("-" * 78)
    delta_rdt = stats['rendement_annuel_pct'] - stats['bh_annuel_pct']
    delta_dd  = stats['bh_drawdown_pct'] - stats['max_drawdown_pct']
    print(f"{'Rendement /an':<25} {stats['rendement_annuel_pct']:>+14.2f}% "
          f"{stats['bh_annuel_pct']:>+14.2f}% {delta_rdt:>+14.2f}pts")
    print(f"{'Drawdown max':<25} {stats['max_drawdown_pct']:>14.2f}% "
          f"{stats['bh_drawdown_pct']:>14.2f}% {delta_dd:>+14.2f}pts")
    print(f"{'Rendement total':<25} {stats['rendement_total_pct']:>+14.2f}% "
          f"{stats['bh_total_pct']:>+14.2f}% "
          f"{stats['rendement_total_pct'] - stats['bh_total_pct']:>+14.2f}pts")

    print()
    print("=" * 78)
    print("  PERFORMANCE PAR ANNEE")
    print("=" * 78)
    print(f"{'ANNEE':<8} {'EQUITY_DEBUT':>14} {'EQUITY_FIN':>14} {'PERF%':>10}")
    print("-" * 78)
    for an in sorted(stats['perf_annuelle'].keys()):
        p = stats['perf_annuelle'][an]
        mark = ""
        if p['perf_pct'] > 50:
            mark = "  EXCELLENT"
        elif p['perf_pct'] > 20:
            mark = "  bon"
        elif p['perf_pct'] < -10:
            mark = "  perte"
        print(f"{an:<8} {p['debut']:>13.2f}$ {p['fin']:>13.2f}$ "
              f"{p['perf_pct']:>+9.2f}%{mark}")

    print()
    print("=" * 78)
    print("  DETAIL DES 10 DERNIERS TRADES")
    print("=" * 78)
    print(f"{'ENTREE':<12} {'SORTIE':<12} {'ENTRY':>10} {'EXIT':>10} "
          f"{'PNL%':>8} {'DUREE':>7}")
    print("-" * 78)
    for t in resultat['trades'][-10:]:
        de = datetime.fromtimestamp(t['entry_t'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
        ds = datetime.fromtimestamp(t['exit_t'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
        print(f"{de:<12} {ds:<12} {t['entry_price']:>9.2f}$ "
              f"{t['exit_price']:>9.2f}$ {t['pnl_pct']:>+7.2f}% "
              f"{t['duration_days']:>5}j")

    print()
    print("=" * 78)
    print("  LECTURE")
    print("=" * 78)
    print(f"  Strategie : Achete BTC quand tendance haussiere claire,")
    print(f"              vend quand la tendance se casse, sinon cash.")
    print()
    print(f"  AVANTAGES vs Buy & Hold :")
    print(f"   - Reduction du drawdown ({stats['bh_drawdown_pct']:.0f}% -> "
          f"{stats['max_drawdown_pct']:.0f}%)")
    print(f"   - Capital en cash pendant les bear markets (securite)")
    print(f"   - Pas de stress emotionnel pendant les crashes")
    print()
    print(f"  INCONVENIENTS :")
    print(f"   - Periodes longues en cash (parfois 6-12 mois sans trade)")
    print(f"   - Manque parfois le debut des bull markets")
    print(f"   - Sous-performe pendant les bull markets violents (vs Buy & Hold)")


# =====================================================================
# Main
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Backtest Trend Following Daily.")
    ap.add_argument("--symbole", default="BTCUSDT")
    ap.add_argument("--capital", type=float, default=10000.0,
                    help="Capital initial en USD (defaut 10000)")
    ap.add_argument("--annees", type=int, default=7,
                    help="Nombre d'annees d'historique (defaut 7)")
    args = ap.parse_args()

    print(f"Telechargement {args.symbole} 1d sur {args.annees} ans...")
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - args.annees * 365 * 86400 * 1000

    klines = fetch_klines_daily(args.symbole, start_ms, end_ms)
    if not klines or len(klines) < 250:
        print("Echec telechargement ou pas assez de donnees.")
        sys.exit(1)
    print(f"  OK : {len(klines)} bougies daily")

    resultat = backtest_trend(klines, capital_init=args.capital)
    stats = analyser(resultat, klines, args.capital)
    afficher(stats, args.symbole, args.capital, resultat)


if __name__ == "__main__":
    main()
