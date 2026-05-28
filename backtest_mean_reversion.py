#!/usr/bin/env python3
"""
backtest_mean_reversion.py

Backtest de la strategie Mean Reversion (RSI2 + MA200) sur BTC et ETH,
+ reconstruction de la courbe Trend D1 sur les memes coins/periode pour
mesurer la complementarite.

Specification : MEAN_REVERSION_BACKTEST_SPEC.md (v1 baseline FIGE).

Regles Mean Reversion (v1) :
    Entree :  Close[D] > MA200[D] ET RSI2[D] < 10
              -> ouvre LONG a Open[D+1]
    Sortie :  Close[D] > MA10[D]                          (reversion realisee)
              OU jours_en_position >= 10                  (time-stop)
              OU Close[D] < MA200[D]                      (regime casse)
              -> ferme a Open[D+1]
    Sizing : 100% du capital alloue au coin quand en position, sinon cash.
    Frais  : 0.1% par leg (entree ET sortie).

Regles Trend D1 (baseline, reconstruction du bot existant) :
    Entree :  Close[D] > MA50[D] ET MA50[D] > MA200[D] ET Close[D] > High20[D]
              ou High20[D] = max(highs des 20 bougies PRECEDENTES, excluant D)
              -> ouvre LONG a Open[D+1]
    Sortie :  Close[D] < MA50[D]
              -> ferme a Open[D+1]

Anti-bug :
    - Pas de lookahead : decision sur close[D], execution sur open[D+1].
    - Warmup 200 bougies (MA200 doit etre valide).
    - Determinisme total (pas de hasard).
    - Trous API : si une bougie manque, on skip sans fabriquer de prix.
    - Frais factures sur les 2 legs explicitement.

Usage :
    python3 backtest_mean_reversion.py
    python3 backtest_mean_reversion.py --capital 1000
    python3 backtest_mean_reversion.py --json-out trades.json
    python3 backtest_mean_reversion.py --symboles BTCUSDT
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


BASE_URL = "https://api.binance.com"
FEE_RATE = 0.001    # 0.1% par leg (taker Binance)

# Univers spec : BTC + ETH uniquement
SYMBOLES_SPEC = ["BTCUSDT", "ETHUSDT"]


# =====================================================================
# Telechargement avec pagination (necessaire pour 7 ans = ~2555 bougies)
# =====================================================================
def fetch_klines_daily(symbole, start_ms, end_ms):
    """Pagine /api/v3/klines pour recuperer toutes les bougies daily.

    Retourne une liste de dict {t, o, h, l, c, v} triee chronologiquement,
    ou None en cas d'erreur reseau / json.
    """
    out = []
    cur = start_ms
    while cur < end_ms:
        url = (f"{BASE_URL}/api/v3/klines?symbol={symbole}"
               f"&interval=1d&startTime={cur}&endTime={end_ms}&limit=1000")
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
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
        time.sleep(0.15)   # politesse rate-limit
    return out


# =====================================================================
# Indicateurs (calcules SANS lookahead : value[i] n'utilise que data[<=i])
# =====================================================================
def calc_sma(values, period):
    """SMA simple. out[i] = moyenne de values[i-period+1 .. i].
    out[i] = None si i < period - 1."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    cumsum = sum(values[:period])
    out[period - 1] = cumsum / period
    for i in range(period, len(values)):
        cumsum += values[i] - values[i - period]
        out[i] = cumsum / period
    return out


def calc_rsi_wilder(closes, period):
    """RSI Wilder. out[i] = RSI a la close i.
    out[i] = None pour i < period (premier RSI valide a i=period).

    Methode :
        1) Premier avg_gain / avg_loss = SMA des `period` premiers
           (gain, loss) calcules a partir des diff closes[1] - closes[0], ...
        2) Wilder smoothing : avg = (prev_avg * (period-1) + current) / period
    """
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out

    # Diffs jour-a-jour : diffs[i] = closes[i+1] - closes[i]
    # gains[i] = max(0, diffs[i]), losses[i] = max(0, -diffs[i])
    n = len(closes)
    gains = [0.0] * (n - 1)
    losses = [0.0] * (n - 1)
    for i in range(n - 1):
        d = closes[i + 1] - closes[i]
        if d > 0:
            gains[i] = d
        else:
            losses[i] = -d

    # Premier RSI valide a l'index `period` (utilise les `period` premiers diffs)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else (100.0 - 100.0 / (1.0 + avg_gain / avg_loss))

    # Wilder smoothing pour les suivants
    for i in range(period + 1, n):
        diff_idx = i - 1   # indice dans gains/losses correspondant a la close i
        avg_gain = (avg_gain * (period - 1) + gains[diff_idx]) / period
        avg_loss = (avg_loss * (period - 1) + losses[diff_idx]) / period
        out[i] = 100.0 if avg_loss == 0 else (100.0 - 100.0 / (1.0 + avg_gain / avg_loss))

    return out


# =====================================================================
# Backtest Mean Reversion
# =====================================================================
def backtest_mean_reversion(klines, capital_init, fee=FEE_RATE):
    """Backtest MR v1 selon spec section 2.

    Retourne dict avec trades, equity_curve, status_curve (in_position bool
    par timestamp), valeur_finale, etc.
    """
    n = len(klines)
    if n < 202:
        return _resultat_vide(capital_init)

    closes = [k["c"] for k in klines]

    rsi2  = calc_rsi_wilder(closes, 2)
    ma200 = calc_sma(closes, 200)
    ma10  = calc_sma(closes, 10)

    # Anti-bug : alignement des indicateurs. Si une fonction d'indicateur
    # decale la longueur de sa sortie par rapport a closes, tous les
    # references closes[i]/rsi2[i]/... pointeraient sur des dates differentes.
    # Cette assertion garantit qu'on lit toujours la meme date pour tous.
    assert len(rsi2) == len(ma200) == len(ma10) == len(closes), (
        f"MR : desalignement indicateurs "
        f"(rsi2={len(rsi2)} ma200={len(ma200)} ma10={len(ma10)} closes={len(closes)})"
    )

    capital = capital_init
    position = None
    trades = []
    equity_curve = []     # liste de (timestamp_ms, equity_usd, in_position_bool)

    # Demarrage : on doit avoir MA200 valide (i >= 199) ET on doit avoir
    # une bougie i+1 disponible pour l'execution. On commence donc a i=200
    # (premier i ou MA200 et RSI2 et MA10 sont toutes valides, et ou i+1 existe).
    #
    # Ordre des operations par iteration i (anti-bug accounting) :
    #   1) Push equity au close[i] avec la position COURANTE (pre-decision).
    #      -> equity[i] reflete l'etat reel AU close de la bougie i, AVANT
    #         l'execution qui aura lieu au open[i+1].
    #   2) Decision sur close[i] (RSI2/MA200/MA10).
    #   3) Si signal : update position (l'execution est conceptuellement au
    #      open[i+1], donc la position aura un impact des l'iteration i+1).
    for i in range(200, n - 1):
        bar = klines[i]
        next_bar = klines[i + 1]
        close_d = closes[i]

        # 1) Push equity au close[i] AVEC L'ETAT PRE-DECISION
        _push_equity(equity_curve, bar, position, close_d, capital)

        # 2) Garde-fou : si indicateur None (ne devrait pas arriver apres warmup)
        if rsi2[i] is None or ma200[i] is None or ma10[i] is None:
            continue

        open_dplus1 = next_bar["o"]

        # 3) Logique de decision
        if position is None:
            # ENTREE : Close[D] > MA200[D] ET RSI2[D] < 10
            if close_d > ma200[i] and rsi2[i] < 10:
                # Execution a Open[D+1]
                capital_avant_fee = capital
                fee_entree = capital_avant_fee * fee
                units = (capital_avant_fee - fee_entree) / open_dplus1
                position = {
                    "entry_t": next_bar["t"],
                    "entry_price": open_dplus1,
                    "entry_capital": capital_avant_fee,
                    "fee_entree": fee_entree,
                    "units": units,
                    "days_in_position": 0,
                }
                capital = 0.0
        else:
            # En position : incremente le compteur puis check les sorties
            position["days_in_position"] += 1

            exit_reason = None
            if close_d > ma10[i]:
                exit_reason = "MA10"           # reversion realisee
            elif position["days_in_position"] >= 10:
                exit_reason = "TIME"           # time-stop
            elif close_d < ma200[i]:
                exit_reason = "MA200_BREAK"    # garde-fou regime

            if exit_reason:
                # Execution a Open[D+1]
                gross = position["units"] * open_dplus1
                fee_sortie = gross * fee
                capital = gross - fee_sortie
                pnl_pct = (capital / position["entry_capital"] - 1.0) * 100.0
                trades.append({
                    "entry_t": position["entry_t"],
                    "exit_t": next_bar["t"],
                    "entry_price": position["entry_price"],
                    "exit_price": open_dplus1,
                    "entry_capital": position["entry_capital"],
                    "exit_capital": capital,
                    "fee_entree": position["fee_entree"],
                    "fee_sortie": fee_sortie,
                    "pnl_pct": pnl_pct,
                    "duration_days": position["days_in_position"],
                    "exit_reason": exit_reason,
                })
                position = None

    # Push final pour la derniere bougie disponible (qui n'a pas pu etre traitee
    # dans la boucle car on n'a pas de bougie i+1 pour l'execution).
    last_bar = klines[-1]
    last_close = closes[-1]
    _push_equity(equity_curve, last_bar, position, last_close, capital)

    # Valeur finale : si une position est encore ouverte a la fin, on la
    # marque a la derniere close (mark-to-market, on ne ferme PAS de force).
    if position:
        valeur_finale = position["units"] * last_close
    else:
        valeur_finale = capital

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "capital_init": capital_init,
        "valeur_finale": valeur_finale,
        "position_ouverte_a_la_fin": position is not None,
    }


# =====================================================================
# Backtest Trend D1 (reconstruction baseline)
# =====================================================================
def backtest_trend_d1(klines, capital_init, fee=FEE_RATE):
    """Reconstruction du Trend D1 selon spec section 4.

    Entree : Close[D] > MA50[D] ET MA50[D] > MA200[D] ET Close[D] > High20[D]
             High20[D] = max(highs des 20 bougies PRECEDENTES, excluant D)
    Sortie : Close[D] < MA50[D]
    Execution : Open[D+1], fees 0.1% par leg.
    """
    n = len(klines)
    if n < 202:
        return _resultat_vide(capital_init)

    closes = [k["c"] for k in klines]
    highs  = [k["h"] for k in klines]

    ma50  = calc_sma(closes, 50)
    ma200 = calc_sma(closes, 200)

    # Anti-bug : meme assertion que pour MR.
    assert len(ma50) == len(ma200) == len(closes) == len(highs), (
        f"Trend D1 : desalignement indicateurs "
        f"(ma50={len(ma50)} ma200={len(ma200)} closes={len(closes)} highs={len(highs)})"
    )

    capital = capital_init
    position = None
    trades = []
    equity_curve = []

    # Meme ordre des operations que pour Mean Reversion :
    #   1) Push equity au close[i] AVEC L'ETAT PRE-DECISION
    #   2) Check indicateurs ready
    #   3) Decide + (eventuellement) update position (execution a open[i+1])
    for i in range(200, n - 1):
        bar = klines[i]
        next_bar = klines[i + 1]
        close_d = closes[i]

        # 1) Push equity au close[i] avant la decision
        _push_equity(equity_curve, bar, position, close_d, capital)

        # 2) Indicateurs ready ? (et i >= 20 pour avoir High20 sur 20 bougies precedentes)
        if ma50[i] is None or ma200[i] is None or i < 20:
            continue

        open_dplus1 = next_bar["o"]
        # High20 = max des highs sur les 20 bougies STRICTEMENT precedentes
        high20 = max(highs[i - 20 : i])

        if position is None:
            if close_d > ma50[i] and ma50[i] > ma200[i] and close_d > high20:
                capital_avant_fee = capital
                fee_entree = capital_avant_fee * fee
                units = (capital_avant_fee - fee_entree) / open_dplus1
                position = {
                    "entry_t": next_bar["t"],
                    "entry_price": open_dplus1,
                    "entry_capital": capital_avant_fee,
                    "fee_entree": fee_entree,
                    "units": units,
                }
                capital = 0.0
        else:
            if close_d < ma50[i]:
                gross = position["units"] * open_dplus1
                fee_sortie = gross * fee
                capital = gross - fee_sortie
                pnl_pct = (capital / position["entry_capital"] - 1.0) * 100.0
                trades.append({
                    "entry_t": position["entry_t"],
                    "exit_t": next_bar["t"],
                    "entry_price": position["entry_price"],
                    "exit_price": open_dplus1,
                    "entry_capital": position["entry_capital"],
                    "exit_capital": capital,
                    "fee_entree": position["fee_entree"],
                    "fee_sortie": fee_sortie,
                    "pnl_pct": pnl_pct,
                    "duration_days": int((next_bar["t"] - position["entry_t"]) / 86400000),
                    "exit_reason": "MA50",
                })
                position = None

    # Push final pour la derniere bougie
    last_bar = klines[-1]
    last_close = closes[-1]
    _push_equity(equity_curve, last_bar, position, last_close, capital)

    if position:
        valeur_finale = position["units"] * last_close
    else:
        valeur_finale = capital

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "capital_init": capital_init,
        "valeur_finale": valeur_finale,
        "position_ouverte_a_la_fin": position is not None,
    }


def _push_equity(curve, bar, position, close_d, capital):
    """Ajoute un point a l'equity curve avec le statut in_position."""
    if position:
        eq = position["units"] * close_d
        curve.append((bar["t"], eq, True))
    else:
        curve.append((bar["t"], capital, False))


def _resultat_vide(capital_init):
    return {
        "trades": [],
        "equity_curve": [],
        "capital_init": capital_init,
        "valeur_finale": capital_init,
        "position_ouverte_a_la_fin": False,
    }


# =====================================================================
# Metriques
# =====================================================================
def metriques(resultat):
    """Calcule les metriques de la spec section 5.1."""
    trades = resultat["trades"]
    equity = resultat["equity_curve"]
    capital_init = resultat["capital_init"]
    valeur_finale = resultat["valeur_finale"]

    n_trades = len(trades)
    rdt_total = (valeur_finale / capital_init - 1.0) * 100.0

    # Annees couvertes (sur l'equity curve)
    if len(equity) >= 2:
        nb_annees = (equity[-1][0] - equity[0][0]) / (365.25 * 86400 * 1000)
    else:
        nb_annees = 0

    if nb_annees > 0:
        cagr = ((valeur_finale / capital_init) ** (1.0 / nb_annees) - 1.0) * 100.0
    else:
        cagr = 0.0

    # Win rate, gain/perte moyennes, R:R
    if n_trades > 0:
        pnls = [t["pnl_pct"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = 100.0 * len(wins) / n_trades
        gain_moy = sum(wins) / len(wins) if wins else 0.0
        perte_moy = sum(losses) / len(losses) if losses else 0.0
        rr = abs(gain_moy / perte_moy) if perte_moy < 0 else 0.0
        duree_moy = sum(t["duration_days"] for t in trades) / n_trades
    else:
        wr = gain_moy = perte_moy = rr = duree_moy = 0.0

    # Max drawdown (sur l'equity curve, base sur le peak)
    peak = capital_init
    max_dd = 0.0
    for (t, eq, _) in equity:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # Exposition : % de jours ou on est en position
    if equity:
        n_in_pos = sum(1 for (_, _, in_pos) in equity if in_pos)
        expo = 100.0 * n_in_pos / len(equity)
    else:
        expo = 0.0

    return {
        "nb_trades": n_trades,
        "win_rate_pct": wr,
        "gain_moy_pct": gain_moy,
        "perte_moy_pct": perte_moy,
        "rr_realise": rr,
        "rdt_total_pct": rdt_total,
        "cagr_pct": cagr,
        "max_dd_pct": max_dd,
        "duree_moy_jours": duree_moy,
        "exposition_pct": expo,
        "nb_annees": nb_annees,
    }


# =====================================================================
# Rendements journaliers et correlation
# =====================================================================
def rendements_journaliers(equity_curve):
    """Convertit equity_curve en dict {timestamp: rendement_journalier}.
    Rendement = eq[i]/eq[i-1] - 1."""
    out = {}
    if len(equity_curve) < 2:
        return out
    prev_eq = equity_curve[0][1]
    for (t, eq, _) in equity_curve[1:]:
        if prev_eq > 0:
            out[t] = (eq / prev_eq) - 1.0
        prev_eq = eq
    return out


def correlation_pearson(x_dict, y_dict, mask_ts=None):
    """Pearson sur timestamps communs. Si mask_ts donne, restreint a ces ts."""
    common = set(x_dict.keys()) & set(y_dict.keys())
    if mask_ts is not None:
        common &= mask_ts
    if len(common) < 2:
        return 0.0
    xs = [x_dict[t] for t in common]
    ys = [y_dict[t] for t in common]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(xs, ys))
    denx = (sum((xi - mx) ** 2 for xi in xs)) ** 0.5
    deny = (sum((yi - my) ** 2 for yi in ys)) ** 0.5
    if denx == 0 or deny == 0:
        return 0.0
    return num / (denx * deny)


def timestamps_en_cash(equity_curve):
    """Renvoie l'ensemble des timestamps ou in_position = False."""
    return {t for (t, _, in_pos) in equity_curve if not in_pos}


def rendement_cumule_sur_masque(returns_dict, mask_ts):
    """Rendement compose (produit (1+r) - 1) sur les timestamps de mask_ts.

    Interpretation : si on appliquait la strategie UNIQUEMENT les jours du
    masque (et qu'on etait cash le reste du temps), quel serait le rendement
    cumule sur la periode ?
    """
    if not mask_ts:
        return 0.0
    prod = 1.0
    n_used = 0
    for t in mask_ts:
        if t in returns_dict:
            prod *= (1.0 + returns_dict[t])
            n_used += 1
    if n_used == 0:
        return 0.0
    return (prod - 1.0) * 100.0


def fraction_pnl_sur_masque(equity_curve, mask_ts):
    """Renvoie le % du P&L $ cumule venant des timestamps de mask_ts.

    Methode : on additionne les deltas d'equity (d_t = eq_t - eq_{t-1}) sur
    les timestamps de mask_ts, on divise par la somme totale des deltas.

    Limitations :
    - Si le P&L total est ~0, on renvoie 0 (division indefinie).
    - Peut depasser 100% si le complement (jours hors masque) est globalement
      negatif pour la strategie.
    """
    if len(equity_curve) < 2:
        return 0.0, 0.0, 0.0
    total_pnl = 0.0
    mask_pnl = 0.0
    for i in range(1, len(equity_curve)):
        t = equity_curve[i][0]
        dpnl = equity_curve[i][1] - equity_curve[i - 1][1]
        total_pnl += dpnl
        if t in mask_ts:
            mask_pnl += dpnl
    if abs(total_pnl) < 1e-9:
        frac_pct = 0.0
    else:
        frac_pct = (mask_pnl / total_pnl) * 100.0
    return mask_pnl, total_pnl, frac_pct


# =====================================================================
# Combine equity curves par sym -> portfolio
# =====================================================================
def equity_portfolio(equity_par_sym):
    """Somme les equity curves de plusieurs symboles sur les timestamps unions.
    Si un sym n'a pas de point a un timestamp, on prend son capital initial."""
    all_ts = set()
    for eq in equity_par_sym.values():
        all_ts.update(t for (t, _, _) in eq)
    all_ts = sorted(all_ts)

    # Index par timestamp pour chaque sym, et capital initial fallback
    derniere_eq = {sym: eq[0][1] if eq else 0.0 for sym, eq in equity_par_sym.items()}
    idx_par_sym = {sym: 0 for sym in equity_par_sym}

    out = []
    for t in all_ts:
        total = 0.0
        any_pos = False
        for sym, eq in equity_par_sym.items():
            # Avance l'index tant que eq[idx][0] <= t
            while idx_par_sym[sym] < len(eq) and eq[idx_par_sym[sym]][0] <= t:
                derniere_eq[sym] = eq[idx_par_sym[sym]][1]
                if eq[idx_par_sym[sym]][2]:
                    any_pos = True
                idx_par_sym[sym] += 1
            total += derniere_eq[sym]
        out.append((t, total, any_pos))
    return out


# =====================================================================
# Affichage
# =====================================================================
def afficher_metriques(label, m):
    print()
    print("=" * 78)
    print(f"  {label}")
    print("=" * 78)
    print(f"  Nb trades            : {m['nb_trades']:>8}")
    print(f"  Win rate             : {m['win_rate_pct']:>8.1f}%")
    print(f"  Gain moyen par trade : {m['gain_moy_pct']:>+8.2f}%")
    print(f"  Perte moyenne        : {m['perte_moy_pct']:>+8.2f}%")
    print(f"  R:R realise          : {m['rr_realise']:>8.2f}")
    print(f"  Rendement total      : {m['rdt_total_pct']:>+8.2f}%")
    print(f"  CAGR                 : {m['cagr_pct']:>+8.2f}%/an")
    print(f"  Max drawdown         : {m['max_dd_pct']:>8.2f}%")
    print(f"  Duree moy. en trade  : {m['duree_moy_jours']:>8.1f} jours")
    print(f"  Exposition (% temps) : {m['exposition_pct']:>8.1f}%")
    print(f"  Periode              : {m['nb_annees']:>8.2f} ans")


def afficher_complementarite(mr_par_sym, trend_par_sym):
    """Section 5.2 de la spec : test de complementarite.

    On a remplace la "correlation pendant Trend cash" (degeneree car la serie
    Trend est constante a 0 sur ces periodes -> variance nulle -> correlation
    indefinie) par deux metriques plus parlantes :
      - RDT_CUM_MR_PDT_TC  : rendement compose de MR sur les jours Trend-cash
                              (= ce que MR ferait si on l'appliquait UNIQUEMENT
                               ces jours-la et qu'on etait cash le reste)
      - FRAC_PNL_MR_PDT_TC : % du P&L $ total de MR qui vient des jours Trend-cash
    """
    print()
    print("=" * 78)
    print("  TEST DE COMPLEMENTARITE (Mean Reversion vs Trend D1)")
    print("=" * 78)
    print()
    print(f"{'COIN':<10} {'CORR_GLOBAL':>13} {'RDT_MR_PDT_TC':>16} "
          f"{'FRAC_PNL_TC':>13} {'N_JRS_TC':>10}")
    print("-" * 78)

    for sym in mr_par_sym.keys():
        mr_eq = mr_par_sym[sym]["equity_curve"]
        trend_eq = trend_par_sym[sym]["equity_curve"]

        mr_ret = rendements_journaliers(mr_eq)
        trend_ret = rendements_journaliers(trend_eq)

        # 1) Correlation globale des rendements journaliers
        corr_global = correlation_pearson(mr_ret, trend_ret)

        # 2) Timestamps ou Trend D1 est en CASH (in_position = False)
        ts_cash_trend = timestamps_en_cash(trend_eq)

        # 3) Rendement compose de MR sur ces jours uniquement
        rdt_mr_pdt_tc = rendement_cumule_sur_masque(mr_ret, ts_cash_trend)

        # 4) Fraction du P&L $ total de MR venant de ces jours
        mask_pnl, total_pnl, frac_pct = fraction_pnl_sur_masque(mr_eq, ts_cash_trend)

        # 5) Nombre de jours dans le masque (intersection avec les jours ou MR a une obs)
        n_jrs_tc = len(ts_cash_trend & set(mr_ret.keys()))

        print(f"{sym:<10} {corr_global:>+12.3f} {rdt_mr_pdt_tc:>+15.2f}% "
              f"{frac_pct:>+12.1f}% {n_jrs_tc:>10}")

    print()
    print("  Lecture :")
    print("  - CORR_GLOBAL       : correlation des rendements journaliers MR vs Trend")
    print("                        (toute la periode). Proche de 0 = decorrele = bon.")
    print("  - RDT_MR_PDT_TC     : rendement compose de MR limite aux jours ou Trend")
    print("                        D1 est en CASH. Positif et significatif = MR ajoute")
    print("                        bien de la valeur quand Trend ne fait rien.")
    print("  - FRAC_PNL_TC       : % du P&L $ total de MR qui vient des jours Trend-cash.")
    print("                        Eleve = la plupart du profit MR vient des periodes ou")
    print("                        Trend dort. C'est la signature d'une vraie complementarite.")
    print("                        Peut > 100% si le reste du temps MR est legerement perdant.")
    print("  - N_JRS_TC          : nombre de jours ou Trend D1 etait en cash sur la periode.")


def afficher_portfolio_combine(mr_par_sym, trend_par_sym, capital_par_strat):
    """Section 5.2 : portefeuille 50/50 combine."""
    print()
    print("=" * 78)
    print("  PORTEFEUILLE 50/50 COMBINE (MR + Trend D1)")
    print("=" * 78)

    # Equity portfolio MR (somme sur tous les coins de MR)
    eq_mr_global = equity_portfolio(
        {sym: res["equity_curve"] for sym, res in mr_par_sym.items()}
    )
    # Idem Trend
    eq_trend_global = equity_portfolio(
        {sym: res["equity_curve"] for sym, res in trend_par_sym.items()}
    )

    # Capital initial MR = sum des capitals_init de chaque coin MR
    cap_init_mr = sum(res["capital_init"] for res in mr_par_sym.values())
    cap_init_trend = sum(res["capital_init"] for res in trend_par_sym.values())

    # Resultat virtuel "portfolio MR"
    res_mr = {
        "trades": [t for r in mr_par_sym.values() for t in r["trades"]],
        "equity_curve": eq_mr_global,
        "capital_init": cap_init_mr,
        "valeur_finale": eq_mr_global[-1][1] if eq_mr_global else cap_init_mr,
        "position_ouverte_a_la_fin": False,
    }
    res_trend = {
        "trades": [t for r in trend_par_sym.values() for t in r["trades"]],
        "equity_curve": eq_trend_global,
        "capital_init": cap_init_trend,
        "valeur_finale": eq_trend_global[-1][1] if eq_trend_global else cap_init_trend,
        "position_ouverte_a_la_fin": False,
    }

    m_mr = metriques(res_mr)
    m_trend = metriques(res_trend)

    # Combine 50/50 : a chaque timestamp, valeur = 0.5 * eq_mr + 0.5 * eq_trend
    # (suppose un rebalancing virtuel a t=0, pas de rebalancing intra)
    common_ts = sorted(set(t for (t, _, _) in eq_mr_global) &
                       set(t for (t, _, _) in eq_trend_global))
    mr_by_t    = {t: eq for (t, eq, _) in eq_mr_global}
    trend_by_t = {t: eq for (t, eq, _) in eq_trend_global}

    # On normalise : chaque strategie demarre virtuellement avec 1 unite,
    # puis on prend la moyenne arithmetique des courbes normalisees.
    if not common_ts:
        print("  ! Pas de timestamps communs entre MR et Trend. Skip.")
        return

    eq_combine = []
    for t in common_ts:
        # Normalise par capital initial -> facteur de croissance
        f_mr    = mr_by_t[t] / cap_init_mr
        f_trend = trend_by_t[t] / cap_init_trend
        # Equite combinee normalisee = moyenne
        f_comb = 0.5 * f_mr + 0.5 * f_trend
        eq_combine.append((t, f_comb, None))

    valeur_finale_comb = eq_combine[-1][1]
    rdt_total_comb = (valeur_finale_comb - 1.0) * 100.0
    nb_annees = (eq_combine[-1][0] - eq_combine[0][0]) / (365.25 * 86400 * 1000)
    cagr_comb = ((valeur_finale_comb) ** (1.0 / nb_annees) - 1.0) * 100.0 if nb_annees > 0 else 0.0

    # Drawdown combine
    peak = 1.0
    max_dd_comb = 0.0
    for (_, f, _) in eq_combine:
        if f > peak:
            peak = f
        dd = (peak - f) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd_comb:
            max_dd_comb = dd

    print()
    print(f"{'STRATEGIE':<20} {'CAGR':>10} {'MAX_DD':>10} {'RDT_TOTAL':>12} {'TRADES':>8}")
    print("-" * 78)
    print(f"{'Mean Reversion':<20} {m_mr['cagr_pct']:>+9.2f}% {m_mr['max_dd_pct']:>9.2f}% "
          f"{m_mr['rdt_total_pct']:>+11.2f}% {m_mr['nb_trades']:>8}")
    print(f"{'Trend D1':<20} {m_trend['cagr_pct']:>+9.2f}% {m_trend['max_dd_pct']:>9.2f}% "
          f"{m_trend['rdt_total_pct']:>+11.2f}% {m_trend['nb_trades']:>8}")
    print(f"{'COMBINE 50/50':<20} {cagr_comb:>+9.2f}% {max_dd_comb:>9.2f}% "
          f"{rdt_total_comb:>+11.2f}% {m_mr['nb_trades'] + m_trend['nb_trades']:>8}")
    print()
    print("  Lecture : si le combine a un meilleur ratio CAGR/MaxDD qu'une strategie")
    print("  seule, c'est le signe d'une vraie complementarite (et pas juste d'une")
    print("  moyenne mecanique).")


# =====================================================================
# Main
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Backtest Mean Reversion + comparaison Trend D1.")
    ap.add_argument("--symboles", nargs="+", default=SYMBOLES_SPEC,
                    help="Coins a backtester (defaut : BTCUSDT ETHUSDT)")
    ap.add_argument("--annees", type=int, default=7,
                    help="Periode (defaut 7 ans)")
    ap.add_argument("--capital", type=float, default=1000.0,
                    help="Capital initial par coin et par strategie (defaut 1000)")
    ap.add_argument("--json-out", default=None,
                    help="Fichier JSON pour exporter trades + equity curves")
    args = ap.parse_args()

    print("=" * 78)
    print("  BACKTEST MEAN REVERSION (v1) + COMPARAISON TREND D1")
    print("=" * 78)
    print(f"  Symboles : {', '.join(args.symboles)}")
    print(f"  Periode  : {args.annees} ans")
    print(f"  Capital  : {args.capital}$ par coin et par strategie")
    print(f"  Frais    : {FEE_RATE*100:.2f}% par leg")
    print()

    # Telechargement
    print("Telechargement de l'historique daily (avec pagination)...")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.annees * 365 * 86400 * 1000

    klines_par_sym = {}
    for sym in args.symboles:
        print(f"  {sym} ...", end=" ", flush=True)
        klines = fetch_klines_daily(sym, start_ms, end_ms)
        if not klines or len(klines) < 250:
            n = len(klines) if klines else 0
            print(f"FAIL (seulement {n} bougies)")
            continue
        klines_par_sym[sym] = klines
        print(f"OK ({len(klines)} bougies, du "
              f"{datetime.fromtimestamp(klines[0]['t']/1000, tz=timezone.utc).strftime('%Y-%m-%d')} "
              f"au "
              f"{datetime.fromtimestamp(klines[-1]['t']/1000, tz=timezone.utc).strftime('%Y-%m-%d')})")

    if not klines_par_sym:
        print("Aucune donnee valide. Sortie.")
        sys.exit(1)

    # Backtests par symbole
    print()
    print("Execution des backtests...")
    mr_par_sym = {}
    trend_par_sym = {}
    for sym, klines in klines_par_sym.items():
        print(f"  {sym} : Mean Reversion + Trend D1 ...", end=" ", flush=True)
        mr_par_sym[sym] = backtest_mean_reversion(klines, args.capital)
        trend_par_sym[sym] = backtest_trend_d1(klines, args.capital)
        print(f"OK  (MR: {len(mr_par_sym[sym]['trades'])} trades, "
              f"Trend: {len(trend_par_sym[sym]['trades'])} trades)")

    # Affichage des metriques par symbole
    for sym in klines_par_sym.keys():
        afficher_metriques(f"MEAN REVERSION - {sym}", metriques(mr_par_sym[sym]))
    for sym in klines_par_sym.keys():
        afficher_metriques(f"TREND D1 (baseline) - {sym}", metriques(trend_par_sym[sym]))

    # Section 5.2 : complementarite
    afficher_complementarite(mr_par_sym, trend_par_sym)

    # Portefeuille combine 50/50
    afficher_portfolio_combine(mr_par_sym, trend_par_sym, args.capital)

    # Export JSON
    if args.json_out:
        export = {
            "config": {
                "symboles": list(klines_par_sym.keys()),
                "annees": args.annees,
                "capital_par_coin": args.capital,
                "fee_rate": FEE_RATE,
            },
            "mean_reversion": {
                sym: {
                    "trades": res["trades"],
                    "capital_init": res["capital_init"],
                    "valeur_finale": res["valeur_finale"],
                    "metriques": metriques(res),
                    # equity curve : on stocke aussi pour analyse externe
                    "equity_curve": [
                        {"t": t, "equity": eq, "in_position": ip}
                        for (t, eq, ip) in res["equity_curve"]
                    ],
                }
                for sym, res in mr_par_sym.items()
            },
            "trend_d1": {
                sym: {
                    "trades": res["trades"],
                    "capital_init": res["capital_init"],
                    "valeur_finale": res["valeur_finale"],
                    "metriques": metriques(res),
                    "equity_curve": [
                        {"t": t, "equity": eq, "in_position": ip}
                        for (t, eq, ip) in res["equity_curve"]
                    ],
                }
                for sym, res in trend_par_sym.items()
            },
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(export, f, indent=2)
        print(f"\nExport JSON ecrit dans : {args.json_out}")


if __name__ == "__main__":
    main()
