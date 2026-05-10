#!/usr/bin/env python3
"""
METABOT - Module 1 : Scanner Radar.

Scanne l'univers des paires Binance USDT spot, applique des filtres durs,
puis classe les survivants selon un score pondere multi-criteres.

Sortie : top_du_jour.json contenant le Top 5 quotidien avec metriques detaillees.

Usage :
    python scanner.py              # un scan, ecrit top_du_jour.json
    python scanner.py --verbose    # affiche aussi le detail des candidats rejetes

Conception : stdlib uniquement, comme bot.py. Aucune dependance externe.
"""
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# =====================================================================
# Configuration
# =====================================================================
BASE_URL     = "https://api.binance.com"
HTTP_TIMEOUT = 10
USER_AGENT   = "metabot-scanner/1.0"

# --- Filtres durs : si non respecte, rejet immediat ---
MIN_VOLUME_24H_USDT  = 20_000_000   # 20 M$ : compromis liquidite / nb candidats
MAX_SPREAD_PCT       = 0.10         # spread bid-ask max (%)
MIN_AGE_DAYS         = 90           # paire listee depuis au moins 90 jours
QUOTE_ASSET          = "USDT"

# Filtres anti-piege (sur metriques calculees) :
# - rejette les coins trop volatils (chaos, news, illiquidite)
# - rejette les coins en pump tardif (achat au sommet)
# - rejette les coins sans tendance claire
MAX_ATR_PCT_DAILY    = 8.0          # rejet si ATR daily > 8%
MAX_PERF_7J_PCT      = 40.0         # rejet si deja +40% sur 7j (pump terminal)
MIN_ADX_14D          = 18.0         # rejet si ADX < 18 (pas de tendance)

# Stablecoins / paires "USDT-USDT" a exclure (ne bougent pas)
STABLE_BASES = {
    "BUSD", "USDC", "FDUSD", "TUSD", "DAI", "USDP", "PAX", "GUSD",
    "EUR", "EURI", "EURT", "GBP", "AUD", "TRY", "BRL", "ARS", "RUB",
    "JPY", "ZAR", "MXN", "PLN", "RON", "UAH", "IDR", "NGN", "VAI",
    "USDS", "USDD", "USDE", "PYUSD", "AEUR", "USTC", "USD1",
}

# Tokens "leveraged" (3x, etc.) a exclure
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

# --- Poids du score (somme = 100) ---
POIDS = {
    "volatilite":      25,   # ATR% journalier
    "tendance":        20,   # ADX 14j
    "force_relative":  20,   # perf 7j vs BTC
    "volume_growth":   15,   # vol 24h vs vol moyen 7j
    "liquidite":       10,   # spread
    "distance_haut":   10,   # distance au plus haut 30j
}

# --- Sortie ---
NB_TOP            = 5
FICHIER_SORTIE    = "top_du_jour.json"
KLINES_LIMITE_1D  = 95           # 95 jours pour permettre le check d'age (>= 90)
KLINES_INTERVALLE = "1d"

# --- Anti rate-limit : courte pause entre les requetes par paire ---
PAUSE_ENTRE_REQUETES = 0.05


# =====================================================================
# Logging minimal
# =====================================================================
VERBOSE = False


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def log_v(msg):
    if VERBOSE:
        log(msg)


# =====================================================================
# Helpers HTTP
# =====================================================================
def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        log(f"ERREUR HTTP {e.code} sur {url}: {body}")
    except (urllib.error.URLError, socket.timeout, json.JSONDecodeError) as e:
        log(f"ERREUR GET {url}: {e}")
    return None


# =====================================================================
# Indicateurs techniques
# =====================================================================
def atr_pct_journalier(klines, periode=14):
    """Average True Range exprime en pourcentage du dernier prix."""
    if len(klines) < periode + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]["h"], klines[i]["l"], klines[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < periode:
        return None
    a = sum(trs[:periode]) / periode
    for tr in trs[periode:]:
        a = (a * (periode - 1) + tr) / periode
    return (a / klines[-1]["c"]) * 100 if klines[-1]["c"] > 0 else None


def adx_wilder(klines, periode=14):
    """ADX selon la methode de Wilder. Retourne la derniere valeur ou None."""
    if len(klines) < periode * 2 + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(klines)):
        h, l = klines[i]["h"], klines[i]["l"]
        ph, pl, pc = klines[i - 1]["h"], klines[i - 1]["l"], klines[i - 1]["c"]
        up_move = h - ph
        down_move = pl - l
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    if len(trs) < periode:
        return None

    # Lissage Wilder initial
    atr_s = sum(trs[:periode])
    plus_s = sum(plus_dm[:periode])
    minus_s = sum(minus_dm[:periode])

    dx_values = []
    for i in range(periode, len(trs)):
        atr_s   = atr_s   - atr_s / periode   + trs[i]
        plus_s  = plus_s  - plus_s / periode  + plus_dm[i]
        minus_s = minus_s - minus_s / periode + minus_dm[i]
        if atr_s <= 0:
            continue
        plus_di  = 100 * plus_s / atr_s
        minus_di = 100 * minus_s / atr_s
        somme = plus_di + minus_di
        if somme <= 0:
            continue
        dx = 100 * abs(plus_di - minus_di) / somme
        dx_values.append(dx)

    if len(dx_values) < periode:
        return None

    adx = sum(dx_values[:periode]) / periode
    for dx in dx_values[periode:]:
        adx = (adx * (periode - 1) + dx) / periode
    return adx


# =====================================================================
# Fonctions de scoring (chaque fonction retourne 0..100)
# =====================================================================
def score_volatilite(atr_pct):
    """Sweet spot ATR journalier 2-5%. Trop bas = pas de mouvement.
       Trop haut = chaos / news / illiquidite."""
    if atr_pct is None:
        return 0
    if atr_pct < 1.0:
        return max(0, atr_pct * 20)            # 0..20 lineaire
    if atr_pct <= 2.0:
        return 20 + (atr_pct - 1.0) * 60       # 20..80
    if atr_pct <= 5.0:
        return 100                              # plateau
    if atr_pct <= 8.0:
        return 100 - (atr_pct - 5.0) * 13.33    # 100..60
    if atr_pct <= 15.0:
        return max(20, 60 - (atr_pct - 8.0) * 5.7)
    return 10                                   # > 15% : trop dangereux


def score_tendance(adx):
    """ADX < 20 = pas de tendance. 25-40 = tendance saine. > 40 = mature/late."""
    if adx is None:
        return 0
    if adx < 15:
        return adx * (20 / 15)                  # 0..20
    if adx < 25:
        return 20 + (adx - 15) * 5              # 20..70
    if adx <= 40:
        return 70 + (adx - 25) * 2              # 70..100
    if adx <= 60:
        return 100 - (adx - 40) * 0.5           # 100..90
    return 80                                   # tres mature, attention au retournement


def score_force_relative(perf_7j_pct, perf_btc_7j_pct):
    """Performance relative au BTC. Centree sur 0% = 50."""
    if perf_7j_pct is None or perf_btc_7j_pct is None:
        return 50
    diff = perf_7j_pct - perf_btc_7j_pct
    # +10% vs BTC -> 100, 0% -> 50, -10% vs BTC -> 0, lineaire au-dela
    score = 50 + diff * 5
    return max(0, min(100, score))


def score_volume_growth(ratio):
    """ratio = volume 24h / volume moyen 7j (en USDT)."""
    if ratio is None or ratio <= 0:
        return 50
    if ratio < 0.5:
        return 10
    if ratio < 1.0:
        return 10 + (ratio - 0.5) * 80          # 10..50
    if ratio < 1.5:
        return 50 + (ratio - 1.0) * 60          # 50..80
    if ratio <= 2.5:
        return 80 + (ratio - 1.5) * 20          # 80..100
    return 100                                  # > 2.5x : volume explose


def score_liquidite(spread_pct):
    """Spread bid-ask en %. < 0.02% = excellent, > 0.10% = rejete avant."""
    if spread_pct is None:
        return 50
    if spread_pct <= 0.02:
        return 100
    if spread_pct <= 0.05:
        return 100 - (spread_pct - 0.02) * (40 / 0.03)   # 100..60
    if spread_pct <= 0.10:
        return 60 - (spread_pct - 0.05) * (30 / 0.05)    # 60..30
    return 0


def score_distance_haut(dist_pct):
    """Distance au plus haut sur 30j (% en dessous).
       0-5% = proche du sommet, risque pump fini.
       5-15% = sweet spot : marge a parcourir + tendance encore fraiche.
       Au-dela = soit consolidation profonde, soit tendance baissiere."""
    if dist_pct is None:
        return 50
    if dist_pct < 0:
        return 60                               # nouveau plus haut atteint aujourd'hui
    if dist_pct < 5:
        return 30 + dist_pct * 14               # 30..100 (sweet spot demarre)
    if dist_pct <= 15:
        return 100                              # plateau optimal
    if dist_pct <= 30:
        return 100 - (dist_pct - 15) * 2        # 100..70
    if dist_pct <= 50:
        return 70 - (dist_pct - 30) * 1.5       # 70..40
    return 25                                   # bear market probable


# =====================================================================
# Recuperation des donnees
# =====================================================================
def fetch_exchange_info():
    log("Recuperation /exchangeInfo...")
    return http_get_json(f"{BASE_URL}/api/v3/exchangeInfo")


def fetch_ticker_24h():
    log("Recuperation /ticker/24hr...")
    return http_get_json(f"{BASE_URL}/api/v3/ticker/24hr")


def fetch_book_ticker():
    """Tous les meilleurs bid/ask en un appel -> calcul du spread."""
    log("Recuperation /ticker/bookTicker (spreads)...")
    return http_get_json(f"{BASE_URL}/api/v3/ticker/bookTicker")


def fetch_klines_jour(symbole, limite=KLINES_LIMITE_1D):
    url = (f"{BASE_URL}/api/v3/klines"
           f"?symbol={symbole}&interval={KLINES_INTERVALLE}&limit={limite}")
    data = http_get_json(url)
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
                "v": float(b[5]),         # volume base asset
                "qv": float(b[7]),        # volume USDT (quote asset)
            })
        except (ValueError, IndexError):
            return None
    return out


# =====================================================================
# Filtres durs
# =====================================================================
def construire_set_symboles_spot(exchange_info):
    """Renvoie l'ensemble des symboles USDT spot eligibles (status TRADING,
       spot autorise, pas leveraged, pas stablecoin)."""
    if not exchange_info or "symbols" not in exchange_info:
        return set()
    elig = set()
    for s in exchange_info["symbols"]:
        sym = s["symbol"]
        if s.get("status") != "TRADING":
            continue
        if not s.get("isSpotTradingAllowed", False):
            continue
        if s.get("quoteAsset") != QUOTE_ASSET:
            continue
        base = s.get("baseAsset", "")
        if base in STABLE_BASES:
            continue
        if any(sym.endswith(suf) for suf in LEVERAGED_SUFFIXES):
            continue
        elig.add(sym)
    return elig


def appliquer_filtres_volume(tickers, symboles_eligibles):
    """Filtre sur volume 24h + perf 24h + extraction des metriques de base."""
    candidats = []
    rejets = {"non_eligible": 0, "volume_bas": 0, "parse": 0}
    for t in tickers:
        sym = t.get("symbol", "")
        if sym not in symboles_eligibles:
            rejets["non_eligible"] += 1
            continue
        try:
            vol = float(t["quoteVolume"])
            pct_24h = float(t["priceChangePercent"])
            prix = float(t["lastPrice"])
            count = int(t.get("count", 0))
        except (KeyError, ValueError):
            rejets["parse"] += 1
            continue
        if vol < MIN_VOLUME_24H_USDT:
            rejets["volume_bas"] += 1
            continue
        candidats.append({
            "symbol": sym,
            "volume_24h_usdt": vol,
            "pct_change_24h": pct_24h,
            "prix": prix,
            "trade_count_24h": count,
        })
    log(f"Filtre volume : {len(candidats)} candidats | "
        f"rejets : {rejets['volume_bas']} vol_bas, "
        f"{rejets['non_eligible']} non_spot/USDT")
    return candidats


def appliquer_filtre_spread(candidats, book_tickers):
    """Calcule le spread % pour chaque candidat ; filtre ceux > MAX_SPREAD_PCT."""
    bt_map = {b["symbol"]: b for b in book_tickers} if book_tickers else {}
    survivants = []
    rejets = 0
    for c in candidats:
        bt = bt_map.get(c["symbol"])
        if not bt:
            rejets += 1
            continue
        try:
            bid = float(bt["bidPrice"])
            ask = float(bt["askPrice"])
        except (KeyError, ValueError):
            rejets += 1
            continue
        if bid <= 0 or ask <= 0:
            rejets += 1
            continue
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        if spread_pct > MAX_SPREAD_PCT:
            log_v(f"  REJET spread {c['symbol']}: {spread_pct:.3f}%")
            rejets += 1
            continue
        c["spread_pct"] = spread_pct
        survivants.append(c)
    log(f"Filtre spread : {len(survivants)} candidats (rejets : {rejets})")
    return survivants


# =====================================================================
# Analyse approfondie + scoring
# =====================================================================
def calculer_metriques(symbole, klines, btc_perf_7j):
    """A partir des klines journalieres, calcule toutes les metriques.
       Retourne un dict ou None si donnees insuffisantes (filtre age inclus)."""
    if not klines or len(klines) < MIN_AGE_DAYS:
        return None  # paire trop jeune
    closes = [k["c"] for k in klines]
    highs = [k["h"] for k in klines]
    quote_vols = [k["qv"] for k in klines]

    last = closes[-1]
    if last <= 0:
        return None

    # Performance 7j (= last vs close il y a 7 bougies daily)
    if len(closes) >= 8:
        c7 = closes[-8]
        perf_7j = (last - c7) / c7 * 100 if c7 > 0 else 0
    else:
        perf_7j = 0

    # Plus haut 30j (haut, pas close)
    fenetre_30j = highs[-30:] if len(highs) >= 30 else highs
    plus_haut_30j = max(fenetre_30j)
    dist_haut_pct = (plus_haut_30j - last) / plus_haut_30j * 100 if plus_haut_30j > 0 else None

    # Volume ratio = vol 24h (= qv derniere bougie close-1, plus stable que ticker)
    # On compare la derniere bougie complete au moyen des 7 d'avant
    if len(quote_vols) >= 8:
        vol_dernier = quote_vols[-1]
        vol_moyen_7j = sum(quote_vols[-8:-1]) / 7
        ratio_vol = vol_dernier / vol_moyen_7j if vol_moyen_7j > 0 else None
    else:
        ratio_vol = None

    return {
        "atr_pct_daily": atr_pct_journalier(klines, 14),
        "adx_14d": adx_wilder(klines, 14),
        "perf_7j_pct": perf_7j,
        "perf_relative_pct": perf_7j - btc_perf_7j if btc_perf_7j is not None else None,
        "ratio_volume_7j": ratio_vol,
        "dist_haut_30j_pct": dist_haut_pct,
        "plus_haut_30j": plus_haut_30j,
        "klines_recus": len(klines),
    }


def calculer_score(metriques, spread_pct, btc_perf_7j):
    """Combine les sous-scores ponderes."""
    sv = score_volatilite(metriques["atr_pct_daily"])
    st = score_tendance(metriques["adx_14d"])
    sf = score_force_relative(metriques["perf_7j_pct"], btc_perf_7j)
    sg = score_volume_growth(metriques["ratio_volume_7j"])
    sl = score_liquidite(spread_pct)
    sd = score_distance_haut(metriques["dist_haut_30j_pct"])

    sous_scores = {
        "volatilite":     round(sv, 1),
        "tendance":       round(st, 1),
        "force_relative": round(sf, 1),
        "volume_growth":  round(sg, 1),
        "liquidite":      round(sl, 1),
        "distance_haut":  round(sd, 1),
    }
    total = sum(sous_scores[k] * POIDS[k] for k in POIDS) / 100.0
    return round(total, 2), sous_scores


# =====================================================================
# Orchestration principale
# =====================================================================
def lancer_scan():
    t_debut = time.time()
    log("=" * 70)
    log("SCANNER RADAR - METABOT - Phase 1")
    log("=" * 70)

    # 1) Univers
    info = fetch_exchange_info()
    if not info:
        log("ECHEC fetch exchange_info, abandon.")
        return None
    eligibles = construire_set_symboles_spot(info)
    log(f"Univers spot USDT eligible : {len(eligibles)} paires")

    # 2) Tickers 24h + filtre volume
    tickers = fetch_ticker_24h()
    if not tickers:
        log("ECHEC fetch ticker 24h, abandon.")
        return None
    candidats = appliquer_filtres_volume(tickers, eligibles)
    if not candidats:
        log("Aucun candidat apres filtre volume. Abandon.")
        return None

    # 3) Spread
    book = fetch_book_ticker()
    if book:
        candidats = appliquer_filtre_spread(candidats, book)
    else:
        log("WARN : pas de book_ticker, filtre spread saute.")
        for c in candidats:
            c["spread_pct"] = None

    if not candidats:
        log("Aucun candidat apres filtre spread. Abandon.")
        return None

    # 4) BTC reference pour perf 7j
    log("Recuperation BTCUSDT pour reference de force relative...")
    btc_kl = fetch_klines_jour("BTCUSDT", limite=KLINES_LIMITE_1D)
    btc_perf_7j = None
    if btc_kl and len(btc_kl) >= 8:
        c0 = btc_kl[-8]["c"]
        c1 = btc_kl[-1]["c"]
        if c0 > 0:
            btc_perf_7j = (c1 - c0) / c0 * 100
        log(f"BTC perf 7j = {btc_perf_7j:.2f}%")

    # 5) Klines + scoring pour chaque candidat
    log(f"Analyse approfondie de {len(candidats)} candidats "
        f"(klines daily, ATR, ADX...)")
    resultats = []
    rejets_age = 0
    rejets_klines = 0
    rejets_atr_haut = 0
    rejets_perf_haute = 0
    rejets_adx_bas = 0
    for i, c in enumerate(candidats):
        if i and i % 25 == 0:
            log(f"  ... {i}/{len(candidats)} analyses")
        kl = fetch_klines_jour(c["symbol"])
        time.sleep(PAUSE_ENTRE_REQUETES)
        if not kl:
            rejets_klines += 1
            continue
        if len(kl) < MIN_AGE_DAYS:
            log_v(f"  REJET age {c['symbol']}: {len(kl)} jours seulement")
            rejets_age += 1
            continue
        m = calculer_metriques(c["symbol"], kl, btc_perf_7j)
        if not m:
            rejets_klines += 1
            continue
        # ADX et ATR doivent etre calculables ; sinon on skip
        if m["atr_pct_daily"] is None or m["adx_14d"] is None:
            rejets_klines += 1
            continue

        # Filtres anti-piege calibres
        if m["atr_pct_daily"] > MAX_ATR_PCT_DAILY:
            log_v(f"  REJET ATR haut {c['symbol']}: {m['atr_pct_daily']:.2f}%")
            rejets_atr_haut += 1
            continue
        if m["perf_7j_pct"] > MAX_PERF_7J_PCT:
            log_v(f"  REJET perf 7j {c['symbol']}: +{m['perf_7j_pct']:.1f}%")
            rejets_perf_haute += 1
            continue
        if m["adx_14d"] < MIN_ADX_14D:
            log_v(f"  REJET ADX bas {c['symbol']}: {m['adx_14d']:.1f}")
            rejets_adx_bas += 1
            continue

        score_total, sous = calculer_score(m, c["spread_pct"], btc_perf_7j)
        resultats.append({
            "symbol": c["symbol"],
            "score_total": score_total,
            "sous_scores": sous,
            "metriques": {
                "prix": c["prix"],
                "volume_24h_usdt": c["volume_24h_usdt"],
                "trade_count_24h": c["trade_count_24h"],
                "spread_pct": round(c["spread_pct"], 4) if c["spread_pct"] is not None else None,
                "atr_pct_daily": round(m["atr_pct_daily"], 3),
                "adx_14d": round(m["adx_14d"], 2),
                "perf_7j_pct": round(m["perf_7j_pct"], 2),
                "perf_relative_vs_btc_pct": (round(m["perf_relative_pct"], 2)
                                             if m["perf_relative_pct"] is not None else None),
                "ratio_volume_24h_vs_7j": (round(m["ratio_volume_7j"], 2)
                                            if m["ratio_volume_7j"] is not None else None),
                "dist_haut_30j_pct": (round(m["dist_haut_30j_pct"], 2)
                                       if m["dist_haut_30j_pct"] is not None else None),
                "klines_recus": m["klines_recus"],
            },
        })

    log(f"Analyses completes : {len(resultats)} | "
        f"rejets age : {rejets_age} | klines : {rejets_klines} | "
        f"ATR haut : {rejets_atr_haut} | perf 7j : {rejets_perf_haute} | "
        f"ADX bas : {rejets_adx_bas}")

    if not resultats:
        log("Aucun resultat scorable.")
        return None

    # 6) Tri et selection du Top
    resultats.sort(key=lambda r: r["score_total"], reverse=True)
    for rang, r in enumerate(resultats, start=1):
        r["rang"] = rang
    top = resultats[:NB_TOP]

    # 7) Construction du JSON final
    duree = time.time() - t_debut
    sortie = {
        "scan_timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scan_date": time.strftime("%Y-%m-%d", time.gmtime()),
        "duree_scan_secondes": round(duree, 1),
        "config": {
            "min_volume_24h_usdt": MIN_VOLUME_24H_USDT,
            "max_spread_pct": MAX_SPREAD_PCT,
            "min_age_jours": MIN_AGE_DAYS,
            "max_atr_pct_daily": MAX_ATR_PCT_DAILY,
            "max_perf_7j_pct": MAX_PERF_7J_PCT,
            "min_adx_14d": MIN_ADX_14D,
            "poids_score": POIDS,
        },
        "stats": {
            "univers_eligible": len(eligibles),
            "passe_filtre_volume": len(candidats),
            "passe_filtre_spread": len(candidats),
            "rejets_age": rejets_age,
            "rejets_atr_haut": rejets_atr_haut,
            "rejets_perf_haute": rejets_perf_haute,
            "rejets_adx_bas": rejets_adx_bas,
            "scores_calcules": len(resultats),
            "btc_perf_7j_pct": round(btc_perf_7j, 2) if btc_perf_7j is not None else None,
        },
        "top": top,
        "tous_resultats": resultats,    # garde la liste complete pour debug
    }

    # 8) Ecriture sur disque (atomique)
    try:
        tmp = FICHIER_SORTIE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sortie, f, indent=2, ensure_ascii=False)
        os.replace(tmp, FICHIER_SORTIE)
        log(f"Ecrit : {FICHIER_SORTIE}")
    except OSError as e:
        log(f"ERREUR ecriture {FICHIER_SORTIE}: {e}")
        return None

    # 9) Affichage Top
    log("=" * 70)
    log(f"TOP {NB_TOP} DU JOUR (scan {duree:.1f}s)")
    log("=" * 70)
    for r in top:
        m = r["metriques"]
        log(f"  #{r['rang']} {r['symbol']:<12} score={r['score_total']:5.1f}  "
            f"ATR={m['atr_pct_daily']:.2f}%  ADX={m['adx_14d']:.1f}  "
            f"perf7j={m['perf_7j_pct']:+.1f}%  vol={m['volume_24h_usdt']/1e6:.0f}M$  "
            f"spread={m['spread_pct']:.3f}%")
    log("=" * 70)
    return sortie


def main():
    global VERBOSE
    if "--verbose" in sys.argv or "-v" in sys.argv:
        VERBOSE = True
    res = lancer_scan()
    sys.exit(0 if res else 1)


if __name__ == "__main__":
    main()
