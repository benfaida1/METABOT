#!/usr/bin/env python3
"""
METABOT - Modules 2 et 3 : Detecteur de regime + Multi-timeframe.

Pour une paire donnee, analyse 3 timeframes (4h macro, 1h meso, 15m micro)
et determine :
  - Le regime de marche sur chaque TF (TREND_HAUSSIERE, TREND_BAISSIERE,
    RANGE, SQUEEZE, CHAOS, TRANSITION)
  - Une decision combinee : TRADE_LONG / RANGE_TRADE / ATTENTE / NO_TRADE
  - Une strategie suggeree pour la Phase 4 (TREND_FOLLOW / PULLBACK /
    BREAKOUT / MEAN_REVERSION / NONE)

Sortie : regimes_actuels.json (regime de chaque paire analysee).

Usage :
    python3 regime.py                # analyse les paires de top_du_jour.json
    python3 regime.py BTCUSDT        # analyse une paire specifique
    python3 regime.py --verbose      # detail de chaque etape
"""
import json
import math
import os
import statistics
import sys
import time

# Reutilise les helpers de scanner.py (HTTP, ADX, ATR%)
import scanner


# =====================================================================
# Configuration
# =====================================================================
# Timeframes analyses
TF_MACRO = ("4h",  200)   # ~33 jours d'historique
TF_MESO  = ("1h",  200)   # ~8 jours
TF_MICRO = ("15m", 100)   # ~25 heures

# Seuils de regime
ADX_TREND_MIN     = 25.0   # >= 25 = vraie tendance
ADX_RANGE_MAX     = 20.0   # < 20 = pas de tendance (range)
ATR_CHAOS_MIN     = {      # ATR% par TF au-dela duquel = chaos
    "4h":  6.0,
    "1h":  4.0,
    "15m": 2.5,
}
SQUEEZE_PERCENTILE  = 20    # BB width dans les 20% les plus bas = squeeze
SQUEEZE_LOOKBACK    = 100   # fenetre pour le percentile
RSI_PULLBACK_MAX    = 40    # RSI 15m <= 40 dans tendance haussiere = pullback
RSI_OVERBOUGHT      = 70    # RSI > 70 = surachete (eviter d'acheter)

# Sortie
FICHIER_TOP        = "top_du_jour.json"
FICHIER_REGIMES    = "regimes_actuels.json"

# Anti rate-limit
PAUSE_ENTRE_PAIRES = 0.10


# =====================================================================
# Logging
# =====================================================================
VERBOSE = False


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def log_v(msg):
    if VERBOSE:
        log(msg)


# =====================================================================
# Indicateurs (qui ne sont pas dans scanner.py)
# =====================================================================
def ema(values, period):
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values, period):
    """Retourne la serie complete des EMA (utile pour pente)."""
    if not values or len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [None] * (period - 1)
    e = sum(values[:period]) / period
    out.append(e)
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def bollinger_width_series(closes, period=20, sigma=2):
    """Retourne la liste des largeurs de Bollinger normalisees (BB width / SMA),
       indice par indice. None la ou pas assez de donnees."""
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append(None)
            continue
        window = closes[i - period + 1: i + 1]
        sma = sum(window) / period
        if sma <= 0:
            out.append(None)
            continue
        var = sum((x - sma) ** 2 for x in window) / period
        std = math.sqrt(var)
        width = (2 * sigma * std) / sma   # largeur totale normalisee
        out.append(width)
    return out


def rsi_dernier(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    ag = gains / period
    al = losses / period
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(0.0, diff)
        loss = max(0.0, -diff)
        ag = (ag * (period - 1) + gain) / period
        al = (al * (period - 1) + loss) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


# =====================================================================
# Detection de regime sur un timeframe
# =====================================================================
def detecter_regime_tf(klines, tf_label):
    """Analyse un set de klines et retourne un dict avec :
        regime, prix, ema20, ema50, ema200, adx, atr_pct, bb_width,
        bb_squeeze, ema50_pente_pct, raison.
       Retourne None si donnees insuffisantes."""
    if not klines or len(klines) < 60:
        return None
    closes = [k["c"] for k in klines]
    px = closes[-1]

    # Indicateurs
    e20  = ema(closes, 20)
    e50  = ema(closes, 50)
    e200 = ema(closes, 200) if len(closes) >= 200 else None
    e50_serie = ema_series(closes, 50)
    e50_now  = e50_serie[-1] if e50_serie and e50_serie[-1] is not None else None
    e50_prev = e50_serie[-6] if len(e50_serie) >= 6 and e50_serie[-6] is not None else None
    pente_e50_pct = None
    if e50_now and e50_prev and e50_prev > 0:
        # variation de l'EMA50 sur les 5 dernieres bougies, en %
        pente_e50_pct = (e50_now - e50_prev) / e50_prev * 100

    adx = scanner.adx_wilder(klines, 14)
    atr_p = scanner.atr_pct_journalier(klines, 14)   # nom trompeur, marche sur tout TF

    bbw_serie = bollinger_width_series(closes, 20, 2)
    bbw_now = bbw_serie[-1] if bbw_serie else None
    fenetre_bbw = [w for w in bbw_serie[-SQUEEZE_LOOKBACK:] if w is not None]
    bb_squeeze = False
    bbw_seuil = None
    if bbw_now is not None and len(fenetre_bbw) >= SQUEEZE_LOOKBACK // 2:
        tries = sorted(fenetre_bbw)
        idx_seuil = max(0, len(tries) * SQUEEZE_PERCENTILE // 100 - 1)
        bbw_seuil = tries[idx_seuil]
        bb_squeeze = bbw_now <= bbw_seuil

    # Classification
    seuil_chaos = ATR_CHAOS_MIN.get(tf_label, 5.0)
    if atr_p is not None and atr_p > seuil_chaos:
        regime = "CHAOS"
        raison = f"ATR {atr_p:.2f}% > {seuil_chaos}% (volatilite extreme)"
    elif bb_squeeze and (adx is None or adx < ADX_RANGE_MAX):
        regime = "SQUEEZE"
        raison = f"BB width {bbw_now:.4f} <= {bbw_seuil:.4f} (P{SQUEEZE_PERCENTILE}) + ADX faible"
    elif adx is not None and adx >= ADX_TREND_MIN and e50_now is not None:
        # Tendance possible
        if px > e50_now and (pente_e50_pct is None or pente_e50_pct > 0):
            regime = "TREND_HAUSSIERE"
            raison = f"prix>EMA50, ADX={adx:.1f}, pente EMA50={pente_e50_pct:+.2f}%"
        elif px < e50_now and (pente_e50_pct is None or pente_e50_pct < 0):
            regime = "TREND_BAISSIERE"
            raison = f"prix<EMA50, ADX={adx:.1f}, pente EMA50={pente_e50_pct:+.2f}%"
        else:
            regime = "TRANSITION"
            raison = f"ADX={adx:.1f} fort mais signaux EMA mixtes"
    elif adx is not None and adx < ADX_RANGE_MAX:
        regime = "RANGE"
        raison = f"ADX={adx:.1f} < {ADX_RANGE_MAX} (pas de tendance)"
    else:
        regime = "TRANSITION"
        adx_str = f"{adx:.1f}" if adx is not None else "NA"
        raison = (f"ADX={adx_str} entre {ADX_RANGE_MAX} et {ADX_TREND_MIN} "
                  f"(zone grise)")

    return {
        "tf": tf_label,
        "regime": regime,
        "raison": raison,
        "prix": round(px, 8),
        "ema20":  round(e20, 8)  if e20 is not None else None,
        "ema50":  round(e50, 8)  if e50 is not None else None,
        "ema200": round(e200, 8) if e200 is not None else None,
        "ema50_pente_pct": round(pente_e50_pct, 3) if pente_e50_pct is not None else None,
        "adx_14": round(adx, 2) if adx is not None else None,
        "atr_pct": round(atr_p, 3) if atr_p is not None else None,
        "bb_width": round(bbw_now, 5) if bbw_now is not None else None,
        "bb_squeeze": bb_squeeze,
    }


# =====================================================================
# Combinaison multi-TF -> decision
# =====================================================================
def combiner_regimes(macro, meso, micro, rsi_micro):
    """Applique l'arbre de decision. Retourne (decision, strategie, raison)."""
    if not macro or not meso or not micro:
        return ("NO_TRADE", "NONE", "Donnees insuffisantes sur au moins un TF")

    rm = macro["regime"]
    rs = meso["regime"]
    ri = micro["regime"]

    # 1) Filtres de securite -> NO_TRADE
    if rm == "CHAOS" or rs == "CHAOS" or ri == "CHAOS":
        return ("NO_TRADE", "NONE",
                f"CHAOS detecte (4h={rm}, 1h={rs}, 15m={ri})")
    if rm == "TREND_BAISSIERE":
        return ("NO_TRADE", "NONE",
                "Tendance 4h baissiere : pas de short en spot")

    # 2) Bias macro haussier -> on cherche une entree LONG
    if rm == "TREND_HAUSSIERE":
        if rs == "TREND_HAUSSIERE":
            # Conviction : tendance alignee sur 4h ET 1h
            if ri == "TREND_HAUSSIERE":
                return ("TRADE_LONG", "TREND_FOLLOW",
                        "Tendance haussiere alignee 4h+1h+15m")
            if ri in ("RANGE", "TRANSITION") and rsi_micro is not None and rsi_micro <= RSI_PULLBACK_MAX:
                return ("TRADE_LONG", "PULLBACK",
                        f"Tendance 4h+1h haussiere, pullback 15m (RSI={rsi_micro:.1f})")
            if ri == "TREND_BAISSIERE":
                return ("ATTENTE", "PULLBACK",
                        "Tendance 4h+1h haussiere, mais correction 15m en cours - attendre rebond")
            if ri == "SQUEEZE":
                return ("ATTENTE", "BREAKOUT",
                        "Tendance haussiere, compression 15m - guetter breakout")
            return ("ATTENTE", "NONE",
                    f"Tendance haussiere mais 15m={ri} ambigu")

        if rs == "RANGE":
            if ri == "TREND_HAUSSIERE":
                return ("TRADE_LONG", "BREAKOUT",
                        "4h haussier, 1h en range qui se resout en haussier 15m")
            return ("ATTENTE", "NONE",
                    "4h haussier mais 1h en range - manque de momentum")

        if rs == "SQUEEZE":
            return ("ATTENTE", "BREAKOUT",
                    "4h haussier, 1h en compression - guetter breakout")

        if rs == "TREND_BAISSIERE":
            return ("ATTENTE", "PULLBACK",
                    "4h haussier mais 1h baissier - correction de fond, attendre rebond")

        return ("ATTENTE", "NONE",
                f"4h haussier mais 1h={rs} ambigu")

    # 3) Macro range : trade range possible mais risque
    if rm == "RANGE":
        if rs == "RANGE" and ri in ("RANGE", "TRANSITION"):
            return ("RANGE_TRADE", "MEAN_REVERSION",
                    "Range stable sur 4h+1h - mean reversion possible")
        if rs == "TREND_HAUSSIERE":
            return ("ATTENTE", "BREAKOUT",
                    "4h en range mais 1h pousse haussier - debut de breakout potentiel")
        return ("ATTENTE", "NONE",
                f"4h range, 1h={rs} - signaux non alignes")

    # 4) Macro squeeze : on attend
    if rm == "SQUEEZE":
        return ("ATTENTE", "BREAKOUT",
                "4h en compression - guetter le breakout 4h")

    # 5) Macro transition / autre
    return ("ATTENTE", "NONE",
            f"Pas de bias macro clair (4h={rm})")


# =====================================================================
# Analyse multi-TF d'un symbole
# =====================================================================
def analyser_multi_tf(symbole):
    """Fetch les 3 TFs et retourne le diagnostic complet."""
    log_v(f"  -> fetch klines {symbole} (4h/1h/15m)")
    kl_4h  = obtenir_klines(symbole, TF_MACRO[0], TF_MACRO[1])
    time.sleep(PAUSE_ENTRE_PAIRES)
    kl_1h  = obtenir_klines(symbole, TF_MESO[0],  TF_MESO[1])
    time.sleep(PAUSE_ENTRE_PAIRES)
    kl_15m = obtenir_klines(symbole, TF_MICRO[0], TF_MICRO[1])
    time.sleep(PAUSE_ENTRE_PAIRES)

    if not kl_4h or not kl_1h or not kl_15m:
        return {
            "symbol": symbole,
            "erreur": "fetch klines partiel",
            "decision": "NO_TRADE",
            "strategie": "NONE",
        }

    macro = detecter_regime_tf(kl_4h,  "4h")
    meso  = detecter_regime_tf(kl_1h,  "1h")
    micro = detecter_regime_tf(kl_15m, "15m")

    rsi_micro = rsi_dernier([k["c"] for k in kl_15m], 14)
    decision, strategie, raison = combiner_regimes(macro, meso, micro, rsi_micro)

    return {
        "symbol": symbole,
        "decision": decision,
        "strategie": strategie,
        "raison": raison,
        "rsi_15m": round(rsi_micro, 1) if rsi_micro is not None else None,
        "macro_4h":  macro,
        "meso_1h":   meso,
        "micro_15m": micro,
    }


# =====================================================================
# Recuperation klines (delegue a scanner.http_get_json)
# =====================================================================
def obtenir_klines(symbole, intervalle, limite):
    url = (f"{scanner.BASE_URL}/api/v3/klines"
           f"?symbol={symbole}&interval={intervalle}&limit={limite}")
    data = scanner.http_get_json(url)
    if not data:
        return None
    out = []
    for b in data:
        try:
            out.append({
                "t": int(b[0]),
                "o": float(b[1]),
                "h": float(b[2]),
                "l": float(b[3]),
                "c": float(b[4]),
                "v": float(b[5]),
                "qv": float(b[7]),
            })
        except (ValueError, IndexError):
            return None
    return out


# =====================================================================
# Orchestration : analyser le Top du jour
# =====================================================================
def lire_top_du_jour():
    if not os.path.exists(FICHIER_TOP):
        return None
    try:
        with open(FICHIER_TOP, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [r["symbol"] for r in data.get("top", [])]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        log(f"ERREUR lecture {FICHIER_TOP}: {e}")
        return None


def analyser_paires(symboles):
    log("=" * 70)
    log(f"DETECTEUR DE REGIME - METABOT - Phase 2")
    log(f"Analyse de {len(symboles)} paire(s) sur 3 TF (4h, 1h, 15m)")
    log("=" * 70)

    resultats = []
    for sym in symboles:
        log(f"Analyse {sym}...")
        r = analyser_multi_tf(sym)
        resultats.append(r)
        if "erreur" in r:
            log(f"  ! {r['erreur']}")
            continue
        log(f"  4h={r['macro_4h']['regime']:<18} "
            f"1h={r['meso_1h']['regime']:<18} "
            f"15m={r['micro_15m']['regime']:<18} "
            f"RSI15m={r['rsi_15m']}")
        log(f"  -> {r['decision']:<11} ({r['strategie']}) : {r['raison']}")

    sortie = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "tf_macro": TF_MACRO[0],
            "tf_meso": TF_MESO[0],
            "tf_micro": TF_MICRO[0],
            "adx_trend_min": ADX_TREND_MIN,
            "adx_range_max": ADX_RANGE_MAX,
            "atr_chaos_par_tf": ATR_CHAOS_MIN,
            "squeeze_percentile": SQUEEZE_PERCENTILE,
            "rsi_pullback_max": RSI_PULLBACK_MAX,
        },
        "resultats": resultats,
    }

    try:
        tmp = FICHIER_REGIMES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sortie, f, indent=2, ensure_ascii=False)
        os.replace(tmp, FICHIER_REGIMES)
        log(f"Ecrit : {FICHIER_REGIMES}")
    except OSError as e:
        log(f"ERREUR ecriture {FICHIER_REGIMES}: {e}")

    # Recap
    log("=" * 70)
    log("RECAPITULATIF")
    log("=" * 70)
    for r in resultats:
        if "erreur" in r:
            log(f"  {r['symbol']:<12} ERREUR : {r['erreur']}")
            continue
        log(f"  {r['symbol']:<12} {r['decision']:<11} {r['strategie']:<14}  "
            f"4h:{r['macro_4h']['regime']:<16} 1h:{r['meso_1h']['regime']:<16} "
            f"15m:{r['micro_15m']['regime']}")
    log("=" * 70)
    return sortie


# =====================================================================
# CLI
# =====================================================================
def main():
    global VERBOSE
    args = sys.argv[1:]
    if "--verbose" in args or "-v" in args:
        VERBOSE = True
        args = [a for a in args if a not in ("--verbose", "-v")]

    if args:
        # Symbole explicite
        symboles = [a.upper() for a in args]
    else:
        # Lecture du top_du_jour.json
        symboles = lire_top_du_jour()
        if not symboles:
            log(f"ERREUR : {FICHIER_TOP} introuvable ou invalide.")
            log("Lance d'abord 'python3 scanner.py' ou specifie un symbole : "
                "'python3 regime.py BTCUSDT'")
            sys.exit(1)

    res = analyser_paires(symboles)
    sys.exit(0 if res else 1)


if __name__ == "__main__":
    main()
