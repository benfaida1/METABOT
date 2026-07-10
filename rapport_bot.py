#!/usr/bin/env python3
"""
rapport_bot.py

Genere un RAPPORT COMPLET du Trend D1 Bot depuis le debut de son execution.

Lit deux sources :
    - trend_d1_state.json  : positions ouvertes + trades clos + P&L
    - trend_d1.log         : historique de toutes les executions (signaux)

Produit un rapport texte affiche a l'ecran ET ecrit dans rapport_bot.txt.

Usage :
    python3 rapport_bot.py
    python3 rapport_bot.py --state trend_d1_state.json --log trend_d1.log
    python3 rapport_bot.py --out mon_rapport.txt
"""
import argparse
import json
import os
import re
from datetime import datetime, timezone


SYMBOLES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]


# =====================================================================
# Chargement des donnees
# =====================================================================
def charger_state(chemin):
    if not os.path.exists(chemin):
        return None
    with open(chemin, "r", encoding="utf-8") as f:
        return json.load(f)


def parser_log(chemin):
    """Extrait les executions et signaux du fichier log.

    Renvoie :
        executions : liste de dict {date, signaux: {sym: {signal, close, ma50, ma200, high20}}}
        premiere_date, derniere_date
    """
    if not os.path.exists(chemin):
        return [], None, None

    executions = []
    courante = None
    dates = []

    # Regex pour extraire une ligne de signal
    # ex: [2026-05-28 00:30:02 UTC]   BTCUSDT    signal=SELL  close=74495.80 MA50=77210.11 MA200=80011.35 high20=82479.32
    re_debut = re.compile(r"\[([\d\-]+) [\d:]+ UTC\] Trend D1 Bot - debut execution")
    re_signal = re.compile(
        r"\[([\d\-]+) [\d:]+ UTC\]\s+(\w+)\s+signal=(\w+)\s+"
        r"close=([\d.]+)\s+MA50=([\d.]+)\s+MA200=([\d.]+)\s+high20=([\d.]+)"
    )
    re_action = re.compile(r"-> (OUVERTURE|FERMETURE) paper")

    with open(chemin, "r", encoding="utf-8") as f:
        for ligne in f:
            m = re_debut.search(ligne)
            if m:
                if courante:
                    executions.append(courante)
                courante = {"date": m.group(1), "signaux": {}, "actions": []}
                dates.append(m.group(1))
                continue
            m = re_signal.search(ligne)
            if m and courante is not None:
                date, sym, sig, close, ma50, ma200, high20 = m.groups()
                courante["signaux"][sym] = {
                    "signal": sig,
                    "close": float(close),
                    "ma50": float(ma50),
                    "ma200": float(ma200),
                    "high20": float(high20),
                }
                continue
            m = re_action.search(ligne)
            if m and courante is not None:
                courante["actions"].append(m.group(1))

    if courante:
        executions.append(courante)

    premiere = dates[0] if dates else None
    derniere = dates[-1] if dates else None
    return executions, premiere, derniere


# =====================================================================
# Generation du rapport
# =====================================================================
def generer_rapport(state, executions, premiere, derniere):
    L = []
    def w(s=""):
        L.append(s)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    w("=" * 70)
    w("            RAPPORT COMPLET - TREND D1 BOT")
    w("=" * 70)
    w(f"  Genere le          : {now}")
    w(f"  Coins suivis       : {', '.join(SYMBOLES)}")
    if premiere and derniere:
        w(f"  Premiere execution : {premiere}")
        w(f"  Derniere execution : {derniere}")
    w(f"  Nb d'executions    : {len(executions)}")
    w("")

    # -----------------------------------------------------------------
    # 1. ETAT ACTUEL
    # -----------------------------------------------------------------
    w("=" * 70)
    w("  1. ETAT ACTUEL DU PORTEFEUILLE")
    w("=" * 70)
    if not state:
        w("  (aucun fichier d'etat trouve)")
    else:
        capital_coin = state.get("capital_par_coin", 1000.0)
        positions = state.get("positions", {})
        trades = state.get("trades_clos", [])

        w(f"  Capital virtuel par coin : {capital_coin:.0f}$")
        w(f"  Capital total alloue     : {capital_coin * len(SYMBOLES):.0f}$")
        w(f"  Positions ouvertes       : {len(positions)}")
        w(f"  Trades clotures          : {len(trades)}")
        w("")

        if positions:
            w("  --- Positions ouvertes ---")
            for sym, pos in positions.items():
                w(f"    {sym:8} entree {pos['entry_date']} @ {pos['entry_price']:.2f}$ "
                  f"| units={pos['units']:.6f} | engage {pos['capital_engage']:.0f}$")
        else:
            w("  Aucune position ouverte (100% cash).")
            w("  -> Le bot attend une tendance haussiere (MA50 > MA200 + breakout).")
    w("")

    # -----------------------------------------------------------------
    # 2. TRADES CLOTURES
    # -----------------------------------------------------------------
    w("=" * 70)
    w("  2. HISTORIQUE DES TRADES CLOTURES")
    w("=" * 70)
    trades = state.get("trades_clos", []) if state else []
    if not trades:
        w("  Aucun trade cloture depuis le debut.")
        w("  (Normal si le marche est en bear : le bot reste en cash.)")
    else:
        w(f"{'SYMBOLE':<9} {'ENTREE':<12} {'SORTIE':<12} {'ENTRY':>10} "
          f"{'EXIT':>10} {'P&L%':>8} {'P&L$':>9} {'DUREE':>6}")
        w("-" * 70)
        pnl_total = 0.0
        wins = 0
        for t in trades:
            pnl_total += t["pnl_abs"]
            if t["pnl_pct"] > 0:
                wins += 1
            w(f"{t['symbole']:<9} {t['entry_date']:<12} {t['exit_date']:<12} "
              f"{t['entry_price']:>9.2f}$ {t['exit_price']:>9.2f}$ "
              f"{t['pnl_pct']:>+7.2f}% {t['pnl_abs']:>+8.2f}$ "
              f"{t['duree_jours']:>5.0f}j")
        w("-" * 70)
        wr = 100 * wins / len(trades)
        w(f"  Total trades  : {len(trades)}")
        w(f"  Gagnants      : {wins} ({wr:.0f}% win rate)")
        w(f"  P&L realise   : {pnl_total:+.2f}$")
    w("")

    # -----------------------------------------------------------------
    # 3. PERFORMANCE PAR COIN
    # -----------------------------------------------------------------
    w("=" * 70)
    w("  3. PERFORMANCE PAR COIN (trades clos)")
    w("=" * 70)
    if trades:
        par_coin = {}
        for t in trades:
            d = par_coin.setdefault(t["symbole"], {"n": 0, "pnl": 0.0, "wins": 0})
            d["n"] += 1
            d["pnl"] += t["pnl_abs"]
            if t["pnl_pct"] > 0:
                d["wins"] += 1
        w(f"{'SYMBOLE':<9} {'TRADES':>7} {'GAGNANTS':>9} {'WINRATE':>8} {'P&L$':>10}")
        w("-" * 70)
        for sym in SYMBOLES:
            if sym in par_coin:
                d = par_coin[sym]
                wr = 100 * d["wins"] / d["n"]
                w(f"{sym:<9} {d['n']:>7} {d['wins']:>9} {wr:>7.0f}% {d['pnl']:>+9.2f}$")
            else:
                w(f"{sym:<9} {'0':>7} {'-':>9} {'-':>8} {'0.00':>10}")
    else:
        w("  Aucun trade a analyser.")
    w("")

    # -----------------------------------------------------------------
    # 4. DERNIERS SIGNAUX OBSERVES
    # -----------------------------------------------------------------
    w("=" * 70)
    w("  4. DERNIERS SIGNAUX OBSERVES (5 dernieres executions)")
    w("=" * 70)
    if not executions:
        w("  (aucune execution parsee dans le log)")
    else:
        for ex in executions[-5:]:
            w(f"  {ex['date']} :")
            for sym in SYMBOLES:
                s = ex["signaux"].get(sym)
                if s:
                    tendance = "haussiere" if s["ma50"] > s["ma200"] else "baissiere"
                    w(f"    {sym:8} {s['signal']:5} close={s['close']:>10.2f} "
                      f"(MA50 {'>' if s['ma50'] > s['ma200'] else '<'} MA200 -> {tendance})")
            if ex["actions"]:
                w(f"    >> ACTIONS : {', '.join(ex['actions'])}")
            w("")

    # -----------------------------------------------------------------
    # 5. RESUME / LECTURE
    # -----------------------------------------------------------------
    w("=" * 70)
    w("  5. LECTURE DU RAPPORT")
    w("=" * 70)
    nb_pos = len(state.get("positions", {})) if state else 0
    nb_trades = len(trades)
    if nb_trades == 0 and nb_pos == 0:
        w("  Le bot n'a encore ouvert AUCUNE position depuis son demarrage.")
        w("  C'est le comportement attendu en marche baissier : MA50 < MA200")
        w("  sur les 4 coins, donc aucune condition d'entree n'est remplie.")
        w("")
        w("  -> Rien d'anormal. Le bot protege le capital en restant en cash.")
        w("  -> Il entrera automatiquement au prochain 'golden cross' + breakout.")
    else:
        pnl_total = sum(t["pnl_abs"] for t in trades)
        w(f"  Le bot a realise {nb_trades} trade(s) cloture(s).")
        w(f"  P&L realise cumule : {pnl_total:+.2f}$")
        w(f"  Positions actuellement ouvertes : {nb_pos}")
    w("")
    w("=" * 70)
    w("  Fin du rapport.")
    w("=" * 70)

    return "\n".join(L)


# =====================================================================
# Main
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Rapport complet du Trend D1 Bot.")
    ap.add_argument("--state", default="trend_d1_state.json")
    ap.add_argument("--log", default="trend_d1.log")
    ap.add_argument("--out", default="rapport_bot.txt")
    args = ap.parse_args()

    state = charger_state(args.state)
    executions, premiere, derniere = parser_log(args.log)

    rapport = generer_rapport(state, executions, premiere, derniere)

    print(rapport)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(rapport + "\n")
    print(f"\n[OK] Rapport ecrit dans : {args.out}")


if __name__ == "__main__":
    main()
