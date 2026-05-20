#!/usr/bin/env python3
"""
backtest_trend_d1_multi.py

Backtest de la strategie Trend Following D1 sur PLUSIEURS cryptos
en parallele avec allocation egale du capital.

Principe :
    - Capital initial reparti egalement (ex: 10 000$ = 3 333$ par coin sur 3)
    - Chaque coin a son propre backtest independant avec MEMES regles :
        ENTREE : Close > MA50 ET MA50 > MA200 ET Close > plus haut 20j
        SORTIE : Close < MA50
    - Pas de rebalancing entre coins (chacun garde sa part)
    - Equity portfolio = somme des equities individuelles a chaque date

Pourquoi diversifier :
    - Quand BTC est en cash, ETH ou SOL peut etre actif
    - Lisse le drawdown global
    - Multiplie le nombre d'opportunites par an

Usage :
    python backtest_trend_d1_multi.py
    python backtest_trend_d1_multi.py --symboles BTCUSDT ETHUSDT SOLUSDT BNBUSDT
    python backtest_trend_d1_multi.py --capital 10000 --annees 5
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


BASE_URL = "https://api.binance.com"
FEE_RATE = 0.001


def fetch_klines_daily(symbole, start_ms, end_ms):
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


def backtest_trend(klines, capital_init):
    """Backtest Trend D1 independant sur un seul coin."""
    capital = capital_init
    position = None
    trades = []
    equity_by_t = {}    # timestamp_ms -> equity_USD

    MIN_HISTORY = 200
    closes_all = [k["c"] for k in klines]

    for i in range(MIN_HISTORY, len(klines)):
        bar = klines[i]
        close = bar["c"]

        ma50  = sum(closes_all[i - 50 + 1 : i + 1]) / 50
        ma200 = sum(closes_all[i - 200 + 1 : i + 1]) / 200
        high20 = max(k["h"] for k in klines[i - 20 : i])

        if position is None:
            if close > ma50 and ma50 > ma200 and close > high20:
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
            if close < ma50:
                gross = position["units"] * close
                fee = gross * FEE_RATE
                capital = gross - fee
                trades.append({
                    "entry_t": position["entry_t"],
                    "exit_t": bar["t"],
                    "entry_price": position["entry_price"],
                    "exit_price": close,
                    "pnl_pct": (capital / position["entry_capital"] - 1) * 100,
                    "duration_days": int((bar["t"] - position["entry_t"]) / 86400000),
                })
                position = None

        # Equity courante (mark-to-market)
        if position:
            equity_by_t[bar["t"]] = position["units"] * close
        else:
            equity_by_t[bar["t"]] = capital

    # Valeur finale (mark-to-market si position ouverte)
    if position:
        valeur_finale = position["units"] * klines[-1]["c"]
    else:
        valeur_finale = capital

    return {
        "trades": trades,
        "equity_by_t": equity_by_t,
        "valeur_finale": valeur_finale,
        "capital_init": capital_init,
        "position_ouverte_a_la_fin": position is not None,
    }


def combiner_equity(resultats_par_sym):
    """Combine les equity curves de plusieurs coins en une courbe portfolio."""
    # Collecte tous les timestamps possibles
    all_t = set()
    for sym, res in resultats_par_sym.items():
        all_t.update(res["equity_by_t"].keys())
    all_t = sorted(all_t)

    # Pour chaque timestamp, equity portfolio = somme des equities individuelles
    # Si un coin n'a pas de valeur a ce timestamp (pas encore liste), on prend
    # le capital initial (cash).
    portfolio_equity = []
    derniere_eq = {sym: res["capital_init"] for sym, res in resultats_par_sym.items()}

    for t in all_t:
        for sym, res in resultats_par_sym.items():
            if t in res["equity_by_t"]:
                derniere_eq[sym] = res["equity_by_t"][t]
        portfolio_equity.append((t, sum(derniere_eq.values())))

    return portfolio_equity


def analyser_portfolio(portfolio_equity, capital_init_total, resultats_par_sym):
    """Stats globales sur la courbe equity portfolio."""
    if not portfolio_equity:
        return None

    # Drawdown
    peak = capital_init_total
    max_dd_pct = 0
    max_dd_t = None
    for (t, eq) in portfolio_equity:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd_pct:
            max_dd_pct = dd
            max_dd_t = t

    # Performance par annee
    eq_par_annee = {}
    for (t, eq) in portfolio_equity:
        an = datetime.fromtimestamp(t / 1000, tz=timezone.utc).year
        eq_par_annee.setdefault(an, []).append(eq)
    perf_annuelle = {}
    for an in sorted(eq_par_annee.keys()):
        eqs = eq_par_annee[an]
        debut, fin = eqs[0], eqs[-1]
        perf_annuelle[an] = {
            "debut": debut, "fin": fin,
            "perf_pct": (fin / debut - 1) * 100,
        }

    valeur_finale = portfolio_equity[-1][1]
    rendement_total = (valeur_finale / capital_init_total - 1) * 100

    nb_annees = (portfolio_equity[-1][0] - portfolio_equity[0][0]) / (365.25 * 86400 * 1000)
    if nb_annees > 0:
        rendement_annuel = ((valeur_finale / capital_init_total) ** (1 / nb_annees) - 1) * 100
    else:
        rendement_annuel = 0

    nb_trades_total = sum(len(r["trades"]) for r in resultats_par_sym.values())

    return {
        "valeur_finale": valeur_finale,
        "rendement_total_pct": rendement_total,
        "rendement_annuel_pct": rendement_annuel,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_t": max_dd_t,
        "nb_annees": nb_annees,
        "perf_annuelle": perf_annuelle,
        "nb_trades_total": nb_trades_total,
    }


def afficher(stats_portfolio, resultats_par_sym, capital_init_total, symboles):
    print()
    print("=" * 78)
    print(f"  RESULTATS PORTFOLIO MULTI-COIN ({len(symboles)} coins)")
    print("=" * 78)
    print(f"  Capital initial total : {capital_init_total:>12.2f} $")
    print(f"  Capital final total   : {stats_portfolio['valeur_finale']:>12.2f} $")
    print(f"  Rendement total       : {stats_portfolio['rendement_total_pct']:>+12.2f}%")
    print(f"  Rendement annualise   : {stats_portfolio['rendement_annuel_pct']:>+12.2f}%/an")
    print(f"  Periode               : {stats_portfolio['nb_annees']:>12.2f} annees")
    print(f"  Drawdown max          : {stats_portfolio['max_drawdown_pct']:>12.2f}%")
    if stats_portfolio['max_drawdown_t']:
        dt = datetime.fromtimestamp(stats_portfolio['max_drawdown_t'] / 1000, tz=timezone.utc)
        print(f"  Date du DD max        : {dt.strftime('%Y-%m-%d')}")
    print(f"  Trades total (tous coins): {stats_portfolio['nb_trades_total']}")

    # Detail par coin
    print()
    print("=" * 78)
    print("  PERFORMANCE PAR COIN")
    print("=" * 78)
    print(f"{'SYMBOLE':<10} {'CAP_INIT':>10} {'CAP_FINAL':>11} {'RENDEMENT':>11} "
          f"{'TRADES':>7}")
    print("-" * 78)
    for sym in symboles:
        res = resultats_par_sym.get(sym)
        if not res:
            print(f"{sym:<10}  (pas de donnees)")
            continue
        rdt = (res["valeur_finale"] / res["capital_init"] - 1) * 100
        print(f"{sym:<10} {res['capital_init']:>9.0f}$ "
              f"{res['valeur_finale']:>10.2f}$ {rdt:>+10.2f}% "
              f"{len(res['trades']):>7}")

    # Performance par annee
    print()
    print("=" * 78)
    print("  PORTFOLIO PERFORMANCE PAR ANNEE")
    print("=" * 78)
    print(f"{'ANNEE':<8} {'EQUITY_DEBUT':>14} {'EQUITY_FIN':>14} {'PERF%':>10}")
    print("-" * 78)
    for an in sorted(stats_portfolio['perf_annuelle'].keys()):
        p = stats_portfolio['perf_annuelle'][an]
        mark = ""
        if p['perf_pct'] > 50:
            mark = "  EXCELLENT"
        elif p['perf_pct'] > 20:
            mark = "  bon"
        elif p['perf_pct'] < -10:
            mark = "  perte"
        print(f"{an:<8} {p['debut']:>13.2f}$ {p['fin']:>13.2f}$ "
              f"{p['perf_pct']:>+9.2f}%{mark}")

    # Lecture
    print()
    print("=" * 78)
    print("  LECTURE")
    print("=" * 78)
    print(f"  Strategie : Trend Following D1 sur {len(symboles)} coins en parallele.")
    print(f"  Chaque coin a recu {100/len(symboles):.0f}% du capital initial.")
    print(f"  Allocation : pas de rebalancing, chaque coin reste sur sa part.")
    print()
    print(f"  Conversion en %/jour :")
    rdt_jour = stats_portfolio['rendement_annuel_pct'] / 365
    print(f"   - Rendement annualise         : {stats_portfolio['rendement_annuel_pct']:+.2f}%/an")
    print(f"   - Equivalent en %/jour moyen  : {rdt_jour:+.3f}%/jour")
    print()
    print(f"  Si tu deployais avec 1000$ aujourd'hui :")
    cap1k_1an = 1000 * (1 + stats_portfolio['rendement_annuel_pct']/100)
    cap1k_3an = 1000 * (1 + stats_portfolio['rendement_annuel_pct']/100)**3
    cap1k_5an = 1000 * (1 + stats_portfolio['rendement_annuel_pct']/100)**5
    print(f"   - Apres 1 an  : {cap1k_1an:>8.0f}$")
    print(f"   - Apres 3 ans : {cap1k_3an:>8.0f}$")
    print(f"   - Apres 5 ans : {cap1k_5an:>8.0f}$")
    print(f"  (chiffres backtest - le live sera 20-30% inferieur typiquement)")


def main():
    ap = argparse.ArgumentParser(description="Backtest Trend Following D1 Multi-Coin.")
    ap.add_argument("--symboles", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--annees", type=int, default=7)
    args = ap.parse_args()

    print(f"Backtest Trend Following D1 Multi-Coin")
    print(f"  Symboles : {', '.join(args.symboles)}")
    print(f"  Capital  : {args.capital:.0f}$ ({args.capital/len(args.symboles):.0f}$ par coin)")
    print(f"  Periode  : {args.annees} ans")
    print()

    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - args.annees * 365 * 86400 * 1000
    capital_par_coin = args.capital / len(args.symboles)

    resultats_par_sym = {}
    for sym in args.symboles:
        print(f"  Telechargement {sym} ...", end=" ", flush=True)
        klines = fetch_klines_daily(sym, start_ms, end_ms)
        if not klines or len(klines) < 250:
            print(f"FAIL (pas assez de donnees, seulement {len(klines) if klines else 0} bougies)")
            print(f"  --> {sym} ignore. Si c'est un coin recent (SOL, AVAX...),")
            print(f"      l'historique 1d n'est dispo que depuis sa cotation Binance.")
            continue
        print(f"OK ({len(klines)} bougies)")
        resultat = backtest_trend(klines, capital_par_coin)
        resultats_par_sym[sym] = resultat

    if not resultats_par_sym:
        print("Aucun coin n'a de donnees valides. Sortie.")
        sys.exit(1)

    portfolio_equity = combiner_equity(resultats_par_sym)
    capital_init_total = capital_par_coin * len(resultats_par_sym)
    stats = analyser_portfolio(portfolio_equity, capital_init_total, resultats_par_sym)
    afficher(stats, resultats_par_sym, capital_init_total, list(resultats_par_sym.keys()))


if __name__ == "__main__":
    main()
