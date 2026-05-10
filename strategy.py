#!/usr/bin/env python3
"""
METABOT - Module 4 : Strategies d'entree.

Pour chaque type de strategie suggere par regime.py, ce module produit un
signal d'entree concret (ou pas) sur la base des klines 15m (et parfois 1h).

Strategies disponibles :
  - TREND_FOLLOW     : suivre une tendance haussiere alignee
  - PULLBACK         : acheter le creux dans une tendance haussiere
  - BREAKOUT         : entrer sur cassure d'une compression Bollinger
  - MEAN_REVERSION   : acheter le bas d'un range stable (RSI tres bas)

Chaque fonction retourne :
    None                                          si pas de signal
    {"signal": True, "prix": float, "atr_pct": float,
     "raison": str, "strategie": str}             si entree

Le module n'execute aucun ordre. Il decrit ce qu'il faudrait acheter et
a quel prix de reference. Le sizing, les frais, le SL/TP et l'execution
sont du ressort de l'orchestrateur (metabot.py).
"""
import math

import scanner   # http_get_json, atr_pct_journalier, adx_wilder
import regime    # ema, ema_series, bollinger_width_series, rsi_dernier


# =====================================================================
# Configuration des strategies
# =====================================================================
# Communs
ATR_FLOOR_PCT_15M = 0.30          # ATR 15m mini pour qu'un trade vaille la peine

# TREND_FOLLOW
TF_VOLUME_MULT       = 1.0        # vol bougie fermee >= moyenne (pas affaibli)
TF_RSI_MIN           = 50.0
TF_RSI_MAX           = 70.0

# PULLBACK
PB_RSI_BAS           = 40.0       # touche dans les N dernieres bougies
PB_LOOKBACK          = 15         # fenetre pour le creux RSI
PB_RSI_REBOND        = 45.0       # RSI courant doit avoir rebondi au-dessus

# BREAKOUT
BO_SQUEEZE_LOOKBACK  = 10         # squeeze present dans les N dernieres bougies
BO_VOLUME_MULT       = 1.3        # vol cassure >= 1.3x moy 20 (renforce sans extreme)
BO_RSI_MIN           = 55.0
BO_HIGHS_LOOKBACK    = 20         # cassure du plus haut des N dernieres bougies

# MEAN_REVERSION
MR_ADX_MAX           = 20.0       # ADX faible (range)
MR_RSI_MAX           = 30.0       # survendu
MR_BB_PERIODE        = 20
MR_BB_SIGMA          = 2


# =====================================================================
# Indicateurs locaux qui ne sont pas dans scanner/regime
# =====================================================================
def rsi_series(values, period=14):
    """Serie complete des RSI (lissage Wilder)."""
    if len(values) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out = []
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        if al > 0:
            rs = ag / al
            out.append(100 - 100 / (1 + rs))
        else:
            out.append(100.0)
    return out


def bollinger_bands(closes, period=20, sigma=2):
    """Retourne (lower, middle, upper) pour la derniere bougie. None si insuffisant."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = sum(window) / period
    var = sum((x - sma) ** 2 for x in window) / period
    std = math.sqrt(var)
    return (sma - sigma * std, sma, sma + sigma * std)


def volume_moyen(klines, periode=20):
    """Volume moyen des `periode` bougies precedant la derniere."""
    if len(klines) < periode + 1:
        return None
    fenetre = klines[-(periode + 1):-1]
    return sum(k["v"] for k in fenetre) / periode


def bougie_verte(kline):
    return kline["c"] > kline["o"]


# =====================================================================
# Strategie 1 : TREND_FOLLOW
# =====================================================================
def signal_trend_follow(klines_15m):
    """Cassure / continuation dans tendance haussiere alignee.
       Analyse la derniere bougie FERMEE (klines[-1] = bougie en cours, ignoree)."""
    if not klines_15m or len(klines_15m) < 31:
        return None
    klines_15m = klines_15m[:-1]    # exclut la bougie en cours
    closes = [k["c"] for k in klines_15m]
    last = klines_15m[-1]

    # 1) Bougie courante verte
    if not bougie_verte(last):
        return None

    # 2) Prix au-dessus de la EMA20
    e20 = regime.ema(closes, 20)
    if e20 is None or last["c"] <= e20:
        return None

    # 3) Volume confirmant
    vol_avg = volume_moyen(klines_15m, 20)
    if vol_avg is None or last["v"] < vol_avg * TF_VOLUME_MULT:
        return None

    # 4) RSI dans la zone "trend" (ni trop bas, ni trop chaud)
    rsi = regime.rsi_dernier(closes, 14)
    if rsi is None or rsi < TF_RSI_MIN or rsi > TF_RSI_MAX:
        return None

    # 5) ATR plancher
    atr_p = scanner.atr_pct_journalier(klines_15m, 14)
    if atr_p is None or atr_p < ATR_FLOOR_PCT_15M:
        return None

    return {
        "signal": True,
        "strategie": "TREND_FOLLOW",
        "prix": last["c"],
        "atr_pct": atr_p,
        "raison": (f"Cassure EMA20 15m, RSI={rsi:.1f}, "
                   f"vol={last['v']/vol_avg:.2f}x, ATR={atr_p:.2f}%"),
    }


# =====================================================================
# Strategie 2 : PULLBACK
# =====================================================================
def signal_pullback(klines_15m, klines_1h):
    """Achat du creux dans tendance haussiere : RSI bas qui rebondit.
       Analyse les dernieres bougies FERMEES sur les deux TFs."""
    if not klines_15m or len(klines_15m) < 31:
        return None
    if not klines_1h or len(klines_1h) < 61:
        return None
    klines_15m = klines_15m[:-1]
    klines_1h  = klines_1h[:-1]
    closes_15m = [k["c"] for k in klines_15m]
    closes_1h  = [k["c"] for k in klines_1h]
    last = klines_15m[-1]

    # 1) Tendance 1h intacte (filtre de fond)
    e50_1h = regime.ema(closes_1h, 50)
    if e50_1h is None or closes_1h[-1] <= e50_1h:
        return None

    # 2) RSI 15m a touche le creux dans la fenetre lookback
    rsis = rsi_series(closes_15m, 14)
    if len(rsis) < PB_LOOKBACK + 1:
        return None
    creux_atteint = min(rsis[-PB_LOOKBACK:]) <= PB_RSI_BAS
    if not creux_atteint:
        return None

    # 3) RSI courant a rebondi au-dessus de PB_RSI_REBOND
    if rsis[-1] <= PB_RSI_REBOND:
        return None
    # 3b) RSI en hausse stricte sur la derniere bougie
    if rsis[-1] <= rsis[-2]:
        return None

    # 4) Bougie courante verte (confirmation visuelle du rebond)
    if not bougie_verte(last):
        return None

    # 5) ATR plancher
    atr_p = scanner.atr_pct_journalier(klines_15m, 14)
    if atr_p is None or atr_p < ATR_FLOOR_PCT_15M:
        return None

    return {
        "signal": True,
        "strategie": "PULLBACK",
        "prix": last["c"],
        "atr_pct": atr_p,
        "raison": (f"Creux RSI 15m={min(rsis[-PB_LOOKBACK:]):.1f} -> "
                   f"rebond {rsis[-1]:.1f}, prix>EMA50(1h), ATR={atr_p:.2f}%"),
    }


# =====================================================================
# Strategie 3 : BREAKOUT
# =====================================================================
def signal_breakout(klines_15m):
    """Entree sur sortie de compression Bollinger avec volume + cassure.
       Analyse la derniere bougie FERMEE."""
    if not klines_15m or len(klines_15m) < 101:
        return None
    klines_15m = klines_15m[:-1]
    closes = [k["c"] for k in klines_15m]
    highs  = [k["h"] for k in klines_15m]
    last = klines_15m[-1]

    # 1) Squeeze present dans les BO_SQUEEZE_LOOKBACK dernieres bougies
    bbw_serie = regime.bollinger_width_series(closes, 20, 2)
    fenetre = [w for w in bbw_serie[-100:] if w is not None]
    if len(fenetre) < 50:
        return None
    tries = sorted(fenetre)
    seuil_p20 = tries[max(0, len(tries) * 20 // 100 - 1)]

    recents = bbw_serie[-(BO_SQUEEZE_LOOKBACK + 1):-1]   # exclut la bougie courante
    squeeze_recent = any((w is not None and w <= seuil_p20) for w in recents)
    if not squeeze_recent:
        return None

    # 2) Cassure stricte du plus haut des 20 dernieres bougies (avant la courante)
    if len(highs) < BO_HIGHS_LOOKBACK + 1:
        return None
    plus_haut_recent = max(highs[-(BO_HIGHS_LOOKBACK + 1):-1])
    if last["c"] <= plus_haut_recent:
        return None

    # 3) Volume confirmant
    vol_avg = volume_moyen(klines_15m, 20)
    if vol_avg is None or last["v"] < vol_avg * BO_VOLUME_MULT:
        return None

    # 4) RSI > BO_RSI_MIN
    rsi = regime.rsi_dernier(closes, 14)
    if rsi is None or rsi < BO_RSI_MIN:
        return None

    # 5) Bougie courante verte
    if not bougie_verte(last):
        return None

    # 6) ATR plancher
    atr_p = scanner.atr_pct_journalier(klines_15m, 14)
    if atr_p is None or atr_p < ATR_FLOOR_PCT_15M:
        return None

    return {
        "signal": True,
        "strategie": "BREAKOUT",
        "prix": last["c"],
        "atr_pct": atr_p,
        "raison": (f"Cassure {plus_haut_recent:.6f} apres squeeze, "
                   f"RSI={rsi:.1f}, vol={last['v']/vol_avg:.2f}x, ATR={atr_p:.2f}%"),
    }


# =====================================================================
# Strategie 4 : MEAN_REVERSION
# =====================================================================
def signal_mean_reversion(klines_15m):
    """Achat zone basse Bollinger en range, RSI tres bas qui amorce un rebond.
       Analyse la derniere bougie FERMEE."""
    if not klines_15m or len(klines_15m) < 61:
        return None
    klines_15m = klines_15m[:-1]
    closes = [k["c"] for k in klines_15m]
    last = klines_15m[-1]

    # 1) Range confirme : ADX 15m bas
    adx = scanner.adx_wilder(klines_15m, 14)
    if adx is None or adx >= MR_ADX_MAX:
        return None

    # 2) RSI 15m survendu
    rsi = regime.rsi_dernier(closes, 14)
    if rsi is None or rsi > MR_RSI_MAX:
        return None

    # 3) Prix touche / sous la BB inferieure
    bands = bollinger_bands(closes, MR_BB_PERIODE, MR_BB_SIGMA)
    if bands is None:
        return None
    bb_low, bb_mid, bb_up = bands
    # Tolerance : "proche" = within 0.2% au-dessus de la lower
    if last["c"] > bb_low * 1.002:
        return None

    # 4) Bougie verte (rebond commence)
    if not bougie_verte(last):
        return None

    # 5) ATR plancher
    atr_p = scanner.atr_pct_journalier(klines_15m, 14)
    if atr_p is None or atr_p < ATR_FLOOR_PCT_15M:
        return None

    return {
        "signal": True,
        "strategie": "MEAN_REVERSION",
        "prix": last["c"],
        "atr_pct": atr_p,
        "raison": (f"Range ADX={adx:.1f}, RSI={rsi:.1f}, "
                   f"prix {last['c']:.6f} <= BB_low {bb_low:.6f}, "
                   f"target BB_mid {bb_mid:.6f}"),
        "tp_target": bb_mid,        # cible naturelle pour ce style
    }


# =====================================================================
# Dispatcher
# =====================================================================
DISPATCHER = {
    "TREND_FOLLOW":   signal_trend_follow,
    "PULLBACK":       signal_pullback,
    "BREAKOUT":       signal_breakout,
    "MEAN_REVERSION": signal_mean_reversion,
}


def detecter_signal(strategie, klines_15m, klines_1h=None):
    """Appelle la bonne fonction selon le nom de strategie."""
    fn = DISPATCHER.get(strategie)
    if fn is None:
        return None
    if strategie == "PULLBACK":
        return fn(klines_15m, klines_1h)
    return fn(klines_15m)


# =====================================================================
# motif_rejet : pourquoi une strategie a refuse ce coup-ci ?
# Reproduit les checks des fonctions signal_*** dans le meme ordre et
# retourne le label du premier check qui echoue, ou None si tout passe.
# A garder synchro avec les fonctions de signal en cas d'evolution.
# =====================================================================
def motif_rejet(strategie, klines_15m, klines_1h=None):
    if not klines_15m or len(klines_15m) < 31:
        return "klines 15m insuffisant"
    kl15 = klines_15m[:-1]
    closes_15m = [k["c"] for k in kl15]
    last = kl15[-1]

    e20   = regime.ema(closes_15m, 20)
    rsi   = regime.rsi_dernier(closes_15m, 14)
    atr_p = scanner.atr_pct_journalier(kl15, 14)
    vol_avg = volume_moyen(kl15, 20)

    if strategie == "TREND_FOLLOW":
        if not bougie_verte(last):
            return "bougie rouge"
        if e20 is None or last["c"] <= e20:
            return "close<=EMA20"
        if vol_avg is None or last["v"] < vol_avg * TF_VOLUME_MULT:
            ratio = last["v"] / vol_avg if vol_avg else 0
            return f"vol {ratio:.2f}x<{TF_VOLUME_MULT}x"
        if rsi is None:
            return "RSI=NA"
        if rsi < TF_RSI_MIN:
            return f"RSI {rsi:.1f}<{TF_RSI_MIN}"
        if rsi > TF_RSI_MAX:
            return f"RSI {rsi:.1f}>{TF_RSI_MAX}"
        if atr_p is None or atr_p < ATR_FLOOR_PCT_15M:
            return f"ATR {atr_p}%<{ATR_FLOOR_PCT_15M}%"
        return None

    if strategie == "PULLBACK":
        if not klines_1h or len(klines_1h) < 61:
            return "klines 1h insuffisant"
        kl1h = klines_1h[:-1]
        closes_1h = [k["c"] for k in kl1h]
        e50_1h = regime.ema(closes_1h, 50)
        if e50_1h is None or closes_1h[-1] <= e50_1h:
            return "1h close<=EMA50"
        rsis = rsi_series(closes_15m, 14)
        if len(rsis) < PB_LOOKBACK + 1:
            return "RSI serie trop courte"
        mini = min(rsis[-PB_LOOKBACK:])
        if mini > PB_RSI_BAS:
            return f"creux RSI={mini:.1f}>{PB_RSI_BAS}"
        if rsis[-1] <= PB_RSI_REBOND:
            return f"RSI {rsis[-1]:.1f}<={PB_RSI_REBOND} (rebond non confirme)"
        if rsis[-1] <= rsis[-2]:
            return f"RSI {rsis[-1]:.1f}<=prev {rsis[-2]:.1f}"
        if not bougie_verte(last):
            return "bougie rouge"
        if atr_p is None or atr_p < ATR_FLOOR_PCT_15M:
            return f"ATR {atr_p}%<{ATR_FLOOR_PCT_15M}%"
        return None

    if strategie == "BREAKOUT":
        if len(kl15) < 100:
            return "klines<100"
        bbw_serie = regime.bollinger_width_series(closes_15m, 20, 2)
        fenetre = [w for w in bbw_serie[-100:] if w is not None]
        if len(fenetre) < 50:
            return "BBwidth indispo"
        tries = sorted(fenetre)
        seuil_p20 = tries[max(0, len(tries) * 20 // 100 - 1)]
        recents = bbw_serie[-(BO_SQUEEZE_LOOKBACK + 1):-1]
        squeeze_recent = any((w is not None and w <= seuil_p20) for w in recents)
        if not squeeze_recent:
            return "pas de squeeze recent"
        highs = [k["h"] for k in kl15]
        plus_haut_recent = max(highs[-(BO_HIGHS_LOOKBACK + 1):-1])
        if last["c"] <= plus_haut_recent:
            return f"close<=high {plus_haut_recent:.6f}"
        if vol_avg is None or last["v"] < vol_avg * BO_VOLUME_MULT:
            ratio = last["v"] / vol_avg if vol_avg else 0
            return f"vol {ratio:.2f}x<{BO_VOLUME_MULT}x"
        if rsi is None or rsi < BO_RSI_MIN:
            return f"RSI {rsi}<{BO_RSI_MIN}"
        if not bougie_verte(last):
            return "bougie rouge"
        if atr_p is None or atr_p < ATR_FLOOR_PCT_15M:
            return f"ATR {atr_p}%<{ATR_FLOOR_PCT_15M}%"
        return None

    if strategie == "MEAN_REVERSION":
        adx = scanner.adx_wilder(kl15, 14)
        if adx is None or adx >= MR_ADX_MAX:
            return f"ADX {adx}>={MR_ADX_MAX}"
        if rsi is None or rsi > MR_RSI_MAX:
            return f"RSI {rsi}>{MR_RSI_MAX}"
        bb = bollinger_bands(closes_15m, MR_BB_PERIODE, MR_BB_SIGMA)
        if bb is None:
            return "BB indispo"
        if last["c"] > bb[0] * 1.002:
            return f"close>BB_low"
        if not bougie_verte(last):
            return "bougie rouge"
        if atr_p is None or atr_p < ATR_FLOOR_PCT_15M:
            return f"ATR {atr_p}%<{ATR_FLOOR_PCT_15M}%"
        return None

    return "strategie inconnue"


# =====================================================================
# CLI de debug : on demande directement Binance pour un symbole et une strategie
# =====================================================================
def _check(label, ok, detail):
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label:<32} {detail}")
    return ok


def diagnose(strategie, klines_15m, klines_1h=None):
    """Affiche chaque check etape par etape (sur la bougie FERMEE)."""
    if not klines_15m or len(klines_15m) < 31:
        print("  [FAIL] klines 15m insuffisant")
        return

    # Aligne sur la bougie fermee comme les fonctions de signal
    klines_15m = klines_15m[:-1]
    if klines_1h is not None and len(klines_1h) >= 2:
        klines_1h = klines_1h[:-1]

    closes_15m = [k["c"] for k in klines_15m]
    last = klines_15m[-1]
    print(f"  Bougie FERMEE : open={last['o']:.6f}  close={last['c']:.6f}  "
          f"vol={last['v']:.0f}")

    # Indicateurs communs
    e20 = regime.ema(closes_15m, 20)
    rsi = regime.rsi_dernier(closes_15m, 14)
    atr_p = scanner.atr_pct_journalier(klines_15m, 14)
    vol_avg = volume_moyen(klines_15m, 20)
    print(f"  EMA20(15m)={e20:.6f}  RSI(15m)={rsi:.1f}  ATR={atr_p:.2f}%  "
          f"vol_moy20={vol_avg:.0f}")

    if strategie == "TREND_FOLLOW":
        _check("bougie verte",          bougie_verte(last),
               f"close>open ? {last['c']:.6f} > {last['o']:.6f}")
        _check("close > EMA20",         last["c"] > e20,
               f"{last['c']:.6f} > {e20:.6f}")
        _check(f"volume >= {TF_VOLUME_MULT}x moy", last["v"] >= vol_avg * TF_VOLUME_MULT,
               f"ratio = {last['v']/vol_avg:.2f}x")
        _check(f"RSI in [{TF_RSI_MIN};{TF_RSI_MAX}]", TF_RSI_MIN <= rsi <= TF_RSI_MAX,
               f"RSI = {rsi:.1f}")
        _check(f"ATR >= {ATR_FLOOR_PCT_15M}%", atr_p >= ATR_FLOOR_PCT_15M,
               f"ATR = {atr_p:.2f}%")

    elif strategie == "PULLBACK":
        if not klines_1h or len(klines_1h) < 60:
            print("  [FAIL] klines 1h insuffisant")
            return
        closes_1h = [k["c"] for k in klines_1h]
        e50_1h = regime.ema(closes_1h, 50)
        _check("prix > EMA50(1h)",      closes_1h[-1] > e50_1h,
               f"{closes_1h[-1]:.6f} > {e50_1h:.6f}")
        rsis = rsi_series(closes_15m, 14)
        if len(rsis) >= PB_LOOKBACK + 1:
            mini = min(rsis[-PB_LOOKBACK:])
            _check(f"RSI 15m touche <= {PB_RSI_BAS} (en {PB_LOOKBACK} bougies)",
                   mini <= PB_RSI_BAS, f"min={mini:.1f}")
            _check(f"RSI 15m courant > {PB_RSI_REBOND}", rsis[-1] > PB_RSI_REBOND,
                   f"RSI = {rsis[-1]:.1f}")
            _check("RSI en hausse stricte",  rsis[-1] > rsis[-2],
                   f"{rsis[-1]:.1f} > {rsis[-2]:.1f}")
        _check("bougie verte", bougie_verte(last),
               f"close>open ? {last['c']:.6f} > {last['o']:.6f}")
        _check(f"ATR >= {ATR_FLOOR_PCT_15M}%", atr_p >= ATR_FLOOR_PCT_15M,
               f"ATR = {atr_p:.2f}%")

    elif strategie == "BREAKOUT":
        bbw_serie = regime.bollinger_width_series(closes_15m, 20, 2)
        fenetre = [w for w in bbw_serie[-100:] if w is not None]
        if len(fenetre) >= 50:
            tries = sorted(fenetre)
            seuil_p20 = tries[max(0, len(tries) * 20 // 100 - 1)]
            recents = bbw_serie[-(BO_SQUEEZE_LOOKBACK + 1):-1]
            squeeze_recent = any((w is not None and w <= seuil_p20) for w in recents)
            bbw_min_recent = min((w for w in recents if w is not None), default=None)
            _check(f"squeeze present (BBwidth <= P20)", squeeze_recent,
                   f"min recent={bbw_min_recent}  seuil P20={seuil_p20:.5f}")
        highs = [k["h"] for k in klines_15m]
        plus_haut = max(highs[-(BO_HIGHS_LOOKBACK + 1):-1])
        _check(f"close > max high {BO_HIGHS_LOOKBACK} bougies", last["c"] > plus_haut,
               f"{last['c']:.6f} > {plus_haut:.6f}")
        _check(f"volume >= {BO_VOLUME_MULT}x moy",
               last["v"] >= vol_avg * BO_VOLUME_MULT,
               f"ratio = {last['v']/vol_avg:.2f}x")
        _check(f"RSI > {BO_RSI_MIN}", rsi > BO_RSI_MIN, f"RSI = {rsi:.1f}")
        _check("bougie verte", bougie_verte(last),
               f"close>open ? {last['c']:.6f} > {last['o']:.6f}")
        _check(f"ATR >= {ATR_FLOOR_PCT_15M}%", atr_p >= ATR_FLOOR_PCT_15M,
               f"ATR = {atr_p:.2f}%")

    elif strategie == "MEAN_REVERSION":
        adx = scanner.adx_wilder(klines_15m, 14)
        bb = bollinger_bands(closes_15m, MR_BB_PERIODE, MR_BB_SIGMA)
        bb_low, bb_mid, bb_up = bb if bb else (None, None, None)
        _check(f"ADX < {MR_ADX_MAX} (range)", adx is not None and adx < MR_ADX_MAX,
               f"ADX = {adx:.1f}" if adx is not None else "ADX=NA")
        _check(f"RSI <= {MR_RSI_MAX} (survendu)", rsi <= MR_RSI_MAX,
               f"RSI = {rsi:.1f}")
        if bb_low is not None:
            _check("close <= BB_lower (tol 0.2%)", last["c"] <= bb_low * 1.002,
                   f"close={last['c']:.6f}  BB_low={bb_low:.6f}")
        _check("bougie verte", bougie_verte(last),
               f"close>open ? {last['c']:.6f} > {last['o']:.6f}")
        _check(f"ATR >= {ATR_FLOOR_PCT_15M}%", atr_p >= ATR_FLOOR_PCT_15M,
               f"ATR = {atr_p:.2f}%")


def main():
    import sys
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--verbose", "-v")]
    if len(args) < 2:
        print("Usage : python3 strategy.py SYMBOLE STRATEGIE [--verbose]")
        print("Strategies : " + ", ".join(DISPATCHER.keys()))
        sys.exit(1)
    symbole = args[0].upper()
    strategie = args[1].upper()
    if strategie not in DISPATCHER:
        print(f"Strategie inconnue : {strategie}")
        sys.exit(1)

    print(f"Test {strategie} sur {symbole}...")
    kl15 = regime.obtenir_klines(symbole, "15m", 200)
    kl1h = regime.obtenir_klines(symbole, "1h", 100) if strategie == "PULLBACK" else None
    if not kl15:
        print("Echec fetch 15m.")
        sys.exit(1)

    sig = detecter_signal(strategie, kl15, kl1h)
    if sig is None:
        print("Pas de signal.")
        if verbose:
            print()
            print("Diagnostic detaille :")
            diagnose(strategie, kl15, kl1h)
    else:
        print(f"SIGNAL {sig['strategie']} sur {symbole} :")
        print(f"  prix entree : {sig['prix']}")
        print(f"  ATR        : {sig['atr_pct']:.2f}%")
        print(f"  raison     : {sig['raison']}")
        if "tp_target" in sig:
            print(f"  cible TP    : {sig['tp_target']}")


if __name__ == "__main__":
    main()
