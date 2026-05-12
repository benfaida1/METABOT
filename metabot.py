#!/usr/bin/env python3
"""
METABOT - Orchestrateur principal (Phase 3b).

Combine les 4 modules :
  - scanner.py     : Top 5 quotidien (lu via top_du_jour.json)
  - regime.py      : detecteur de regime multi-TF (cache 15 min)
  - strategy.py    : signaux d'entree concrets (TREND_FOLLOW, PULLBACK,
                     BREAKOUT, MEAN_REVERSION)

Boucle principale tournant en continu :
  - tick 3s   : surveille les positions ouvertes (SL / TP / trailing / time stop)
  - 30s       : pour chaque paire en TRADE_LONG, evalue un signal d'entree
  - 15 min    : refresh le regime de toutes les paires du Top
  - 24h       : relit le Top 5 du jour (mis a jour par le cron scanner)

Risk manager integre :
  - 10% du capital "travail" par trade
  - SL = 1.5 x ATR%, TP = 3.0 x ATR%, trailing = 2.0 x ATR%
  - Break-even apres +1R, time stop 4h, max 3 positions
  - Daily loss limit -3% (blocage de la journee)
  - Drawdown max -10% (arret total)
  - Cooldown 30 min apres un SL sur la meme paire

Coffre-fort :
  - A chaque nouveau plus haut de capital realise, 20% du gain est
    reserve dans un compteur "coffre" jamais utilise pour trader.

Modes (var d'env PAPER_TRADING) :
  - true  (defaut) : simulation, argent virtuel, prix Binance reels
  - false          : LIVE, requiert BINANCE_API_KEY + SECRET

Telegram (optionnel) via TELEGRAM_TOKEN + TELEGRAM_CHAT_ID :
  - log de chaque BUY / SELL
  - bilan toutes les heures
"""
import hashlib
import hmac
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import scanner
import regime
import strategy


# =====================================================================
# Configuration (variables d'environnement)
# =====================================================================
def _env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


PAPER_TRADING    = _env_bool("PAPER_TRADING", True)
API_KEY          = os.environ.get("BINANCE_API_KEY", "")
API_SECRET       = os.environ.get("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CAPITAL_INITIAL  = _env_float("CAPITAL_INITIAL", 500.0)

BASE_URL     = "https://api.binance.com"
RECV_WINDOW  = 5000
HTTP_TIMEOUT = 10

# Sizing & coffre
ALLOC_PAR_TRADE       = 0.10
MAX_POSITIONS         = 3
MIN_ORDER_USDT        = 11.0      # binance min ~10-11 USDT
TAUX_REINVESTISSEMENT = 0.80      # 80% reinvesti, 20% au coffre

# Frais & slippage paper
FEE_RATE            = 0.00075
SLIPPAGE_RATE_PAPER = 0.0005

# Risque
ATR_SL_MULT       = 1.5
ATR_TP_MULT       = 3.0
ATR_TRAIL_MULT    = 2.0
SL_FLOOR_PCT      = 0.015
BREAK_EVEN_BUFFER = 0.003
MAX_HOLD_SECONDS  = 4 * 3600

# Garde-fous
DAILY_LOSS_LIMIT_PCT    = 3.0
DRAWDOWN_MAX_PCT        = 10.0
COOLDOWN_AFTER_LOSS_SEC = 30 * 60

# Cadences (en secondes)
TICK_SECONDS            = 3
INTERVALLE_SIGNAL       = 30
INTERVALLE_REGIME       = 15 * 60
INTERVALLE_STATUS       = 60
INTERVALLE_RAPPORT      = 5 * 60     # rapport des motifs de rejet
INTERVALLE_TELEGRAM     = 3600
INTERVALLE_RELIRE_TOP   = 6 * 3600   # relire top_du_jour.json toutes les 6h

# Fichiers
FICHIER_TOP        = "top_du_jour.json"
FICHIER_ETAT       = "etat_bot.json"
FICHIER_TRADES     = "historique_trades.txt"
FICHIER_ERREURS    = "erreurs.log"
FICHIER_PAUSE      = "bot_pause.flag"  # cree/supprime via telegram_bot.py (/pause /resume)


# =====================================================================
# Logging
# =====================================================================
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log_info(msg):
    print(f"[{_now()}] {msg}", flush=True)


def log_trade(msg):
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    try:
        with open(FICHIER_TRADES, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    envoyer_telegram(line)


def log_error(msg):
    line = f"[{_now()}] ERROR: {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open(FICHIER_ERREURS, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# =====================================================================
# Telegram
# =====================================================================
def envoyer_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        }).encode("utf-8")
        urllib.request.urlopen(url, data=data, timeout=5)
    except (urllib.error.URLError, socket.timeout) as e:
        log_error(f"Telegram: {e}")


# =====================================================================
# Binance API (prix temps reel, et signed pour LIVE)
# =====================================================================
def obtenir_prix_actuels(symboles):
    if not symboles:
        return {}
    sym_list = list(symboles)
    sym_param = json.dumps(sym_list, separators=(",", ":"))
    url = f"{BASE_URL}/api/v3/ticker/price?symbols={urllib.parse.quote(sym_param)}"
    data = scanner.http_get_json(url)
    if not data:
        return {}
    if isinstance(data, dict):
        return {data["symbol"]: float(data["price"])}
    return {item["symbol"]: float(item["price"]) for item in data}


def signed_request(method, endpoint, params):
    if not API_KEY or not API_SECRET:
        log_error("Cles API manquantes pour requete signee")
        return None
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = RECV_WINDOW
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    qs += f"&signature={sig}"
    url = f"{BASE_URL}{endpoint}"
    if method == "GET":
        req = urllib.request.Request(url + "?" + qs, method="GET")
    else:
        req = urllib.request.Request(url, data=qs.encode(), method=method)
    req.add_header("X-MBX-APIKEY", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        log_error(f"{method} {endpoint}: HTTP {e.code} {body}")
    except (urllib.error.URLError, socket.timeout, json.JSONDecodeError) as e:
        log_error(f"{method} {endpoint}: {e}")
    return None


def arrondir_lot_size(symbole, quantite):
    info = scanner._exchange_info_cache.get("data") if hasattr(scanner, "_exchange_info_cache") else None
    sym_info = info.get(symbole) if info else None
    if not sym_info:
        return f"{quantite:.6f}"
    for f in sym_info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            qte = math.floor(quantite / step) * step
            if step >= 1:
                return f"{int(qte)}"
            decimals = max(0, -int(math.floor(math.log10(step))))
            return f"{qte:.{decimals}f}"
    return f"{quantite:.6f}"


# =====================================================================
# Passage d'ordre (PAPER ou LIVE)
# =====================================================================
def passer_ordre(symbole, side, montant_usdt=None, quantite=None, ref_price=None):
    """Retourne {filled_qty, avg_price, quote_amount} ou None.
       quote_amount = USDT depenses (BUY) ou recus net frais (SELL)."""
    if PAPER_TRADING:
        if not ref_price or ref_price <= 0:
            log_error("paper_trade: ref_price manquant")
            return None
        if side == "BUY":
            if not montant_usdt or montant_usdt <= 0:
                return None
            eff = ref_price * (1 + SLIPPAGE_RATE_PAPER)
            gross_qty = montant_usdt / eff
            net_qty = gross_qty * (1 - FEE_RATE)
            return {"filled_qty": net_qty, "avg_price": eff, "quote_amount": montant_usdt}
        if not quantite or quantite <= 0:
            return None
        eff = ref_price * (1 - SLIPPAGE_RATE_PAPER)
        net_quote = quantite * eff * (1 - FEE_RATE)
        return {"filled_qty": quantite, "avg_price": eff, "quote_amount": net_quote}

    # LIVE
    params = {"symbol": symbole, "side": side, "type": "MARKET"}
    if side == "BUY":
        params["quoteOrderQty"] = f"{montant_usdt:.2f}"
    else:
        params["quantity"] = arrondir_lot_size(symbole, quantite)
    res = signed_request("POST", "/api/v3/order", params)
    if not res or res.get("status") != "FILLED":
        log_error(f"Ordre non rempli: {res}")
        return None
    try:
        executed_qty = float(res["executedQty"])
        cum_quote = float(res["cummulativeQuoteQty"])
    except (KeyError, ValueError) as e:
        log_error(f"Parse reponse ordre: {e} | {res}")
        return None
    if executed_qty <= 0:
        return None
    avg = cum_quote / executed_qty
    base_asset = symbole.replace("USDT", "")
    if side == "BUY":
        commission_base = sum(
            float(f.get("commission", 0))
            for f in res.get("fills", [])
            if f.get("commissionAsset") == base_asset
        )
        executed_qty -= commission_base
    return {"filled_qty": executed_qty, "avg_price": avg, "quote_amount": cum_quote}


# =====================================================================
# Etat & persistance
# =====================================================================
def etat_par_defaut():
    return {
        "solde_usdt": CAPITAL_INITIAL,
        "positions": {},
        "plus_haut_capital": CAPITAL_INITIAL,
        "capital_coffre": 0.0,
        "cooldowns": {},
        "jour_courant": "",
        "equity_debut_jour": CAPITAL_INITIAL,
        "trading_bloque_jour": False,
        "drawdown_atteint": False,
    }


def charger_etat():
    if not os.path.exists(FICHIER_ETAT):
        return etat_par_defaut()
    try:
        with open(FICHIER_ETAT, "r") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log_error(f"charger_etat: {e}")
        return etat_par_defaut()
    base = etat_par_defaut()
    base.update(d)
    return base


def sauvegarder_etat(etat):
    try:
        tmp = FICHIER_ETAT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(etat, f, indent=2)
        os.replace(tmp, FICHIER_ETAT)
    except OSError as e:
        log_error(f"sauvegarder_etat: {e}")


# =====================================================================
# Coffre-fort & maj jour
# =====================================================================
def capital_realise(etat):
    """Capital total reel = USDT libre + cost basis des positions ouvertes."""
    invest = sum(p["montant_investi"] for p in etat["positions"].values())
    return etat["solde_usdt"] + invest


def maj_coffre(etat):
    """Si nouveau plus haut de capital realise, reserve 20% du gain au coffre."""
    cap = capital_realise(etat)
    if cap > etat["plus_haut_capital"]:
        gain = cap - etat["plus_haut_capital"]
        coffre_inc = gain * (1 - TAUX_REINVESTISSEMENT)
        etat["capital_coffre"] += coffre_inc
        etat["plus_haut_capital"] = cap
        if coffre_inc >= 0.10:
            log_info(f"COFFRE +{coffre_inc:.2f}$ (total coffre {etat['capital_coffre']:.2f}$)")


def cap_travail(etat):
    """Capital disponible pour trader = total realise - coffre."""
    return max(0.0, capital_realise(etat) - etat["capital_coffre"])


def maj_jour(etat, equity):
    aujourd = time.strftime("%Y-%m-%d", time.gmtime())
    if etat["jour_courant"] != aujourd:
        etat["jour_courant"] = aujourd
        etat["equity_debut_jour"] = equity
        etat["trading_bloque_jour"] = False
        log_info(f"Nouveau jour {aujourd}, equity de depart = {equity:.2f}$")
        return
    if etat["trading_bloque_jour"]:
        return
    baseline = etat["equity_debut_jour"]
    if baseline > 0:
        dd = (baseline - equity) / baseline * 100
        if dd >= DAILY_LOSS_LIMIT_PCT:
            etat["trading_bloque_jour"] = True
            log_trade(f"LIMITE PERTE JOURNALIERE ATTEINTE -{dd:.2f}% - "
                      f"pas de nouveaux trades aujourd'hui")


def check_drawdown(etat, equity):
    if etat["drawdown_atteint"]:
        return
    if etat["plus_haut_capital"] <= 0:
        return
    dd_total = (etat["plus_haut_capital"] - equity) / etat["plus_haut_capital"] * 100
    if dd_total >= DRAWDOWN_MAX_PCT:
        etat["drawdown_atteint"] = True
        log_trade(f"DRAWDOWN MAX ATTEINT -{dd_total:.2f}% - "
                  f"arret total du bot. Intervention manuelle requise.")


# =====================================================================
# Top du jour & cache regime
# =====================================================================
_top_cache = {"symbols": [], "ts": 0}
_regime_cache = {}        # symbol -> dict (regime.analyser_multi_tf)
_regime_cache_ts = 0

# Stats des evaluations (pour rapport 5 min)
# symbol -> {"strategie": str, "nb_eval": int, "dernier_motif": str, "ts_dernier": float}
_eval_stats = {}


def lire_top_du_jour():
    """Renvoie la liste des symboles du Top 5 + date du scan."""
    if not os.path.exists(FICHIER_TOP):
        return None, None
    try:
        with open(FICHIER_TOP, "r", encoding="utf-8") as f:
            d = json.load(f)
        syms = [r["symbol"] for r in d.get("top", [])]
        return syms, d.get("scan_date")
    except (OSError, json.JSONDecodeError, KeyError) as e:
        log_error(f"lire_top_du_jour: {e}")
        return None, None


def refresh_top():
    global _top_cache
    syms, scan_date = lire_top_du_jour()
    if not syms:
        log_error(f"{FICHIER_TOP} introuvable / invalide ; fallback BTC/ETH/SOL")
        syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    if syms != _top_cache["symbols"]:
        log_info(f"Top du jour ({scan_date or 'na'}) : {syms}")
    _top_cache = {"symbols": syms, "ts": time.time()}


def refresh_regime_cache():
    global _regime_cache, _regime_cache_ts
    syms = _top_cache.get("symbols") or []
    new_cache = {}
    for s in syms:
        r = regime.analyser_multi_tf(s)
        if r:
            new_cache[s] = r
    _regime_cache = new_cache
    _regime_cache_ts = time.time()

    # Recap concis
    actifs = [f"{s}={r['decision']}/{r['strategie']}"
              for s, r in _regime_cache.items() if "decision" in r]
    log_info(f"Regime refresh : {' | '.join(actifs) if actifs else 'rien'}")


# =====================================================================
# Scan signal d'entree
# =====================================================================
def scan_entry_signals(etat, now):
    """Pour chaque paire du Top avec une decision tradable, evalue un signal."""
    if etat["drawdown_atteint"] or etat["trading_bloque_jour"]:
        return
    if os.path.exists(FICHIER_PAUSE):
        return
    if len(etat["positions"]) >= MAX_POSITIONS:
        return

    cap_t = cap_travail(etat)
    montant = cap_t * ALLOC_PAR_TRADE
    if montant < MIN_ORDER_USDT:
        return

    for sym, reg in _regime_cache.items():
        if len(etat["positions"]) >= MAX_POSITIONS:
            break
        if sym in etat["positions"]:
            continue
        if etat["cooldowns"].get(sym, 0) > now:
            continue
        if etat["solde_usdt"] < montant:
            continue

        decision = reg.get("decision")
        strat = reg.get("strategie")
        if decision not in ("TRADE_LONG", "RANGE_TRADE"):
            continue
        if strat not in strategy.DISPATCHER:
            continue

        # Klines fraiches pour la verification finale
        kl15 = regime.obtenir_klines(sym, "15m", 200)
        kl1h = None
        if strat == "PULLBACK":
            kl1h = regime.obtenir_klines(sym, "1h", 100)
        sig = strategy.detecter_signal(strat, kl15, kl1h)

        # Tracking pour le rapport 5min
        st = _eval_stats.setdefault(sym, {"strategie": strat, "nb_eval": 0,
                                          "dernier_motif": "", "ts_dernier": 0})
        st["strategie"] = strat
        st["nb_eval"] += 1
        st["ts_dernier"] = now
        if not sig:
            st["dernier_motif"] = strategy.motif_rejet(strat, kl15, kl1h) or "?"
            continue
        st["dernier_motif"] = "SIGNAL"

        atr_p = sig["atr_pct"]
        sl_pct = max(SL_FLOOR_PCT, atr_p / 100 * ATR_SL_MULT)
        trail_pct = max(SL_FLOOR_PCT, atr_p / 100 * ATR_TRAIL_MULT)

        fill = passer_ordre(sym, "BUY", montant_usdt=montant, ref_price=sig["prix"])
        if not fill or fill["filled_qty"] <= 0:
            continue

        etat["solde_usdt"] -= fill["quote_amount"]
        etat["positions"][sym] = {
            "quantite_totale":  fill["filled_qty"],
            "montant_investi":  fill["quote_amount"],
            "prix_moyen":       fill["avg_price"],
            "prix_max":         fill["avg_price"],
            "ts_entree":        now,
            "sl_pct":           sl_pct,
            "trail_pct":        trail_pct,
            "atr_pct_entree":   atr_p,
            "strategie":        strat,
            "tp_target":        sig.get("tp_target"),
            "break_even":       False,
        }
        log_trade(
            f"BUY {sym} ({strat}) | PRU {fill['avg_price']:.6f} | "
            f"{fill['quote_amount']:.2f}$ | SL -{sl_pct*100:.2f}% | "
            f"trail {trail_pct*100:.2f}% | ATR {atr_p:.2f}% | "
            f"raison: {sig['raison']}"
        )
        time.sleep(0.5)


# =====================================================================
# Monitor positions (sorties)
# =====================================================================
def monitor_positions(etat, now):
    if not etat["positions"]:
        return
    prix = obtenir_prix_actuels(list(etat["positions"].keys()))
    if not prix:
        return

    for sym in list(etat["positions"].keys()):
        pos = etat["positions"][sym]
        px = prix.get(sym)
        if not px:
            continue

        if px > pos["prix_max"]:
            pos["prix_max"] = px

        entry = pos["prix_moyen"]
        sl_pct = pos["sl_pct"]
        trail_pct = pos["trail_pct"]
        sl_px = entry * (1 - sl_pct)

        # Break-even active a +1R
        if not pos.get("break_even") and px >= entry * (1 + sl_pct):
            pos["break_even"] = True
            log_trade(f"BREAK-EVEN {sym} (+{sl_pct*100:.2f}%)")
        if pos.get("break_even"):
            sl_px = max(sl_px, entry * (1 + BREAK_EVEN_BUFFER))

        # Trailing stop active a +2R
        if pos["prix_max"] >= entry * (1 + sl_pct * 2):
            sl_px = max(sl_px, pos["prix_max"] * (1 - trail_pct))

        # TP cible (par defaut R:R 1:2 via SL*TP_MULT/SL_MULT)
        tp_px = pos.get("tp_target") or entry * (1 + sl_pct * (ATR_TP_MULT / ATR_SL_MULT))
        hard_tp = px >= tp_px

        duree = now - pos["ts_entree"]
        time_out = duree >= MAX_HOLD_SECONDS

        if px <= sl_px or time_out or hard_tp:
            raison = "TIME" if time_out else ("TP" if hard_tp else "SL/TRAIL")
            fill = passer_ordre(sym, "SELL",
                                quantite=pos["quantite_totale"],
                                ref_price=px)
            if not fill:
                continue
            etat["solde_usdt"] += fill["quote_amount"]
            pnl = fill["quote_amount"] - pos["montant_investi"]
            pnl_pct = pnl / pos["montant_investi"] * 100
            duree_min = duree / 60
            del etat["positions"][sym]
            log_trade(
                f"SELL {sym} ({pos['strategie']}/{raison}) "
                f"{pnl_pct:+.2f}% ({pnl:+.2f}$) duree {duree_min:.0f}min"
            )
            if pnl_pct < 0:
                etat["cooldowns"][sym] = now + COOLDOWN_AFTER_LOSS_SEC


# =====================================================================
# Status & bilan
# =====================================================================
def print_status(etat):
    prix = obtenir_prix_actuels(list(etat["positions"].keys())) if etat["positions"] else {}
    val_pos_marche = sum(
        p["quantite_totale"] * prix.get(s, p["prix_moyen"])
        for s, p in etat["positions"].items()
    )
    equity = etat["solde_usdt"] + val_pos_marche
    pnl_jour = equity - etat["equity_debut_jour"]
    pnl_jour_pct = (pnl_jour / etat["equity_debut_jour"] * 100
                    if etat["equity_debut_jour"] > 0 else 0)
    mode = "PAPER" if PAPER_TRADING else "LIVE"

    log_info(f"{mode} | Equity {equity:.2f}$ ({pnl_jour_pct:+.2f}% jour) | "
             f"Libre {etat['solde_usdt']:.2f}$ | "
             f"Coffre {etat['capital_coffre']:.2f}$ | "
             f"Pos {len(etat['positions'])}/{MAX_POSITIONS}")
    if etat["positions"]:
        for s, p in etat["positions"].items():
            px = prix.get(s, p["prix_moyen"])
            pnl_p = (px - p["prix_moyen"]) / p["prix_moyen"] * 100
            log_info(f"  - {s} ({p['strategie']}) PRU {p['prix_moyen']:.6f} "
                     f"-> {px:.6f} ({pnl_p:+.2f}%)")
    if etat["trading_bloque_jour"]:
        log_info(f"  Trading bloque (limite -{DAILY_LOSS_LIMIT_PCT}%)")
    if etat["drawdown_atteint"]:
        log_info(f"  DRAWDOWN MAX ATTEINT - intervention manuelle")


def rapport_rejets():
    """Affiche un recap des evaluations recentes par paire."""
    if not _eval_stats:
        log_info("Rapport scans : aucune paire en TRADE_LONG/RANGE_TRADE evaluee")
        return
    items = []
    for sym, st in sorted(_eval_stats.items()):
        items.append(f"{sym}({st['strategie']}) x{st['nb_eval']} -> {st['dernier_motif']}")
    log_info("Rapport scans (5min) : " + " | ".join(items))
    # Reset des compteurs pour la fenetre suivante
    for st in _eval_stats.values():
        st["nb_eval"] = 0


def bilan_telegram(etat):
    prix = obtenir_prix_actuels(list(etat["positions"].keys())) if etat["positions"] else {}
    val_pos = sum(p["quantite_totale"] * prix.get(s, p["prix_moyen"])
                  for s, p in etat["positions"].items())
    equity = etat["solde_usdt"] + val_pos
    pnl_jour_pct = ((equity - etat["equity_debut_jour"]) / etat["equity_debut_jour"] * 100
                    if etat["equity_debut_jour"] > 0 else 0)
    mode = "PAPER" if PAPER_TRADING else "LIVE"
    msg = (f"Bilan {mode}\n"
           f"Equity: {equity:.2f}$ ({pnl_jour_pct:+.2f}% jour)\n"
           f"Libre: {etat['solde_usdt']:.2f}$ | Coffre: {etat['capital_coffre']:.2f}$\n"
           f"Positions: {len(etat['positions'])}/{MAX_POSITIONS}")
    envoyer_telegram(msg)


# =====================================================================
# Boucle principale
# =====================================================================
def main():
    if not PAPER_TRADING and (not API_KEY or not API_SECRET):
        log_error("Mode LIVE mais BINANCE_API_KEY / BINANCE_API_SECRET manquants. Arret.")
        sys.exit(1)

    etat = charger_etat()

    print("=" * 80)
    if PAPER_TRADING:
        print("METABOT - MODE PAPER (argent virtuel, prix Binance reels)")
    else:
        print("METABOT - MODE LIVE - ATTENTION argent reel")
    print(f"Capital initial    : {CAPITAL_INITIAL:.2f} USDT")
    print(f"Solde charge       : {etat['solde_usdt']:.2f} USDT")
    print(f"Coffre actuel      : {etat['capital_coffre']:.2f} USDT")
    print(f"Positions ouvertes : {len(etat['positions'])}")
    print(f"Max positions      : {MAX_POSITIONS}")
    print(f"Alloc par trade    : {ALLOC_PAR_TRADE*100:.0f}% capital travail")
    print(f"SL / TP / trail    : {ATR_SL_MULT}xATR / {ATR_TP_MULT}xATR / {ATR_TRAIL_MULT}xATR")
    print("=" * 80)

    refresh_top()
    refresh_regime_cache()
    t_signal   = 0
    t_regime   = time.time()
    t_status   = 0
    t_rapport  = time.time()
    t_tele     = 0
    t_top      = time.time()

    while True:
        try:
            now = time.time()

            # Position monitor a chaque tick
            monitor_positions(etat, now)

            # Maj coffre + jour + drawdown (cheap)
            equity_now = etat["solde_usdt"] + sum(
                p["quantite_totale"] * p["prix_moyen"]  # approx, ok pour les checks
                for p in etat["positions"].values()
            )
            maj_coffre(etat)
            maj_jour(etat, equity_now)
            check_drawdown(etat, equity_now)

            # Nettoyage cooldowns
            etat["cooldowns"] = {s: t for s, t in etat["cooldowns"].items() if t > now}

            # Refresh top (toutes les 6h)
            if now - t_top >= INTERVALLE_RELIRE_TOP:
                t_top = now
                refresh_top()

            # Refresh regime (toutes les 15 min)
            if now - t_regime >= INTERVALLE_REGIME:
                t_regime = now
                refresh_regime_cache()

            # Scan signaux d'entree (toutes les 30s)
            if now - t_signal >= INTERVALLE_SIGNAL:
                t_signal = now
                scan_entry_signals(etat, now)

            # Status console (toutes les minutes)
            if now - t_status >= INTERVALLE_STATUS:
                t_status = now
                print_status(etat)

            # Rapport des motifs de rejet (toutes les 5 minutes)
            if now - t_rapport >= INTERVALLE_RAPPORT:
                t_rapport = now
                rapport_rejets()

            # Bilan Telegram (toutes les heures)
            if now - t_tele >= INTERVALLE_TELEGRAM:
                t_tele = now
                bilan_telegram(etat)

            sauvegarder_etat(etat)
            time.sleep(TICK_SECONDS)

        except KeyboardInterrupt:
            log_info("Arret demande, sauvegarde de l'etat...")
            sauvegarder_etat(etat)
            break
        except Exception as e:
            log_error(f"Boucle principale: {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
