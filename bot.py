#!/usr/bin/env python3
"""
METABOT — bot de trading spot Binance.

Mode:
  PAPER_TRADING=true  -> simulation (par defaut, prix reels, argent virtuel)
  PAPER_TRADING=false -> trading reel (cles API requises)

Toutes les variables sensibles passent par les variables d'environnement.
Voir les instructions d'installation fournies separement.
"""
import urllib.request
import urllib.parse
import urllib.error
import json
import time
import os
import hmac
import hashlib
import math
import socket
import sys


# ---------------------------------------------------------------------
# Configuration (variables d'environnement)
# ---------------------------------------------------------------------
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

# Sizing
ALLOC_PAR_TRADE       = 0.10
MAX_POSITIONS         = 4
MIN_ORDER_USDT        = 11.0
TAUX_REINVESTISSEMENT = 0.80

# Frais et slippage (modele paper)
FEE_RATE            = 0.00075   # 0.075% avec discount BNB
SLIPPAGE_RATE_PAPER = 0.0005    # 5 bps simules sur ordre marche

# Risque ATR-based (R:R cible ~1:2)
ATR_SL_MULT       = 1.5
ATR_TP_MULT       = 3.0
ATR_TRAIL_MULT    = 2.0
SL_FLOOR_PCT      = 0.015      # SL min 1.5%
BREAK_EVEN_BUFFER = 0.003      # +30 bps au-dessus du PRU
MAX_HOLD_SECONDS  = 4 * 3600

# Garde-fous
DAILY_LOSS_LIMIT_PCT    = 3.0
COOLDOWN_AFTER_LOSS_SEC = 30 * 60

# Scanner (philosophie: pullback dans tendance, pas chasse au pump)
MAX_CANDIDATS_SCAN = 12
MIN_VOLUME_24H_USD = 20_000_000
MAX_24H_PUMP_PCT   = 8.0       # n'achete pas un coin deja +8% sur 24h
MIN_ATR_PCT        = 0.15      # filtre coins morts
MAX_DIST_EMA20_1H  = 0.04      # prix doit etre a <4% au-dessus de la EMA20 1h

# Fichiers
FICHIER_SAUVEGARDE = "etat_bot.json"
FICHIER_TRADES     = "historique_trades.txt"
FICHIER_ERREURS    = "erreurs.log"


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log_info(msg):
    print(f"[{_now()}] {msg}")


def log_trade(msg):
    line = f"[{_now()}] {msg}"
    print(line)
    try:
        with open(FICHIER_TRADES, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    envoyer_telegram(line)


def log_error(msg):
    line = f"[{_now()}] ERROR: {msg}"
    print(line, file=sys.stderr)
    try:
        with open(FICHIER_ERREURS, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------
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


# ---------------------------------------------------------------------
# Binance API helpers
# ---------------------------------------------------------------------
def _http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "metabot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        log_error(f"HTTP {e.code} {url}: {body}")
    except (urllib.error.URLError, socket.timeout, json.JSONDecodeError) as e:
        log_error(f"GET {url}: {e}")
    return None


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


def obtenir_prix_actuels(symboles=None):
    if symboles:
        sym_list = list(symboles)
        if not sym_list:
            return {}
        sym_param = json.dumps(sym_list, separators=(",", ":"))
        url = f"{BASE_URL}/api/v3/ticker/price?symbols={urllib.parse.quote(sym_param)}"
    else:
        url = f"{BASE_URL}/api/v3/ticker/price"
    data = _http_get_json(url)
    if not data:
        return {}
    if isinstance(data, dict):
        return {data["symbol"]: float(data["price"])}
    return {item["symbol"]: float(item["price"]) for item in data}


def obtenir_klines(symbole, intervalle, limite):
    url = f"{BASE_URL}/api/v3/klines?symbol={symbole}&interval={intervalle}&limit={limite}"
    data = _http_get_json(url)
    if not data:
        return None
    return [
        {"o": float(b[1]), "h": float(b[2]), "l": float(b[3]),
         "c": float(b[4]), "v": float(b[5])}
        for b in data
    ]


_exchange_info_cache = {"data": None, "ts": 0}


def obtenir_filtres_symbole(symbole):
    now = time.time()
    if not _exchange_info_cache["data"] or (now - _exchange_info_cache["ts"]) > 3600:
        info = _http_get_json(f"{BASE_URL}/api/v3/exchangeInfo")
        if info and "symbols" in info:
            _exchange_info_cache["data"] = {s["symbol"]: s for s in info["symbols"]}
            _exchange_info_cache["ts"] = now
    cache = _exchange_info_cache["data"]
    return cache.get(symbole) if cache else None


def arrondir_lot_size(symbole, quantite):
    info = obtenir_filtres_symbole(symbole)
    if not info:
        return f"{quantite:.6f}"
    for f in info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            qte = math.floor(quantite / step) * step
            if step >= 1:
                return f"{int(qte)}"
            decimals = max(0, -int(math.floor(math.log10(step))))
            return f"{qte:.{decimals}f}"
    return f"{quantite:.6f}"


# ---------------------------------------------------------------------
# Indicateurs
# ---------------------------------------------------------------------
def ema(values, period):
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi_series(values, period=14):
    if len(values) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out = []
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        rs = ag / al if al > 0 else float("inf")
        out.append(100 - 100 / (1 + rs))
    return out


def atr_pct(klines, period=14):
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]["h"], klines[i]["l"], klines[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return (a / klines[-1]["c"]) * 100


# ---------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------
def obtenir_top_opportunites():
    data = _http_get_json(f"{BASE_URL}/api/v3/ticker/24hr")
    if not data:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    candidats = []
    for item in data:
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if (sym.endswith("UPUSDT") or sym.endswith("DOWNUSDT")
                or sym.endswith("BULLUSDT") or sym.endswith("BEARUSDT")):
            continue
        try:
            vol = float(item["quoteVolume"])
            pct = float(item["priceChangePercent"])
        except (ValueError, KeyError):
            continue
        if vol < MIN_VOLUME_24H_USD:
            continue
        # On evite les coins deja en pump fort sur 24h (achat de tops)
        if pct > MAX_24H_PUMP_PCT:
            continue
        candidats.append((sym, vol))
    candidats.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in candidats[:MAX_CANDIDATS_SCAN]]


# ---------------------------------------------------------------------
# State
# ---------------------------------------------------------------------
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
    }


def charger_etat():
    if not os.path.exists(FICHIER_SAUVEGARDE):
        return etat_par_defaut()
    try:
        with open(FICHIER_SAUVEGARDE, "r") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log_error(f"charger_etat: {e}")
        return etat_par_defaut()
    base = etat_par_defaut()
    base.update(d)
    return base


def sauvegarder_etat(etat):
    try:
        tmp = FICHIER_SAUVEGARDE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(etat, f, indent=2)
        os.replace(tmp, FICHIER_SAUVEGARDE)
    except OSError as e:
        log_error(f"sauvegarder_etat: {e}")


# ---------------------------------------------------------------------
# Passage d'ordres avec reconciliation
# ---------------------------------------------------------------------
def passer_ordre(symbole, side, quantite=None, montant_usdt=None, ref_price=None):
    """
    Retourne {filled_qty, avg_price, quote_amount} ou None.
    quote_amount = USDT depenses (BUY) ou recus apres frais (SELL).
    """
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
        # SELL
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
    # Si frais payes en base asset (pas en BNB), deduire de la quantite
    base_asset = symbole.replace("USDT", "")
    if side == "BUY":
        commission_base = sum(
            float(f.get("commission", 0))
            for f in res.get("fills", [])
            if f.get("commissionAsset") == base_asset
        )
        executed_qty -= commission_base
    return {"filled_qty": executed_qty, "avg_price": avg, "quote_amount": cum_quote}


# ---------------------------------------------------------------------
# Strategie: pullback dans tendance haussiere
# ---------------------------------------------------------------------
def detecter_signal(kl1m, kl1h, kl1d):
    """Retourne {atr_pct, prix} si signal d'achat valide, sinon None."""
    if not kl1m or not kl1h or not kl1d:
        return None
    if len(kl1m) < 30 or len(kl1h) < 60 or len(kl1d) < 25:
        return None

    closes_1d = [k["c"] for k in kl1d]
    closes_1h = [k["c"] for k in kl1h]
    closes_1m = [k["c"] for k in kl1m]
    px = closes_1m[-1]

    # Filtre 1: tendance journaliere haussiere (prix > EMA20 1d en hausse)
    ema20_1d = ema(closes_1d, 20)
    ema20_1d_prev = ema(closes_1d[:-1], 20)
    if not ema20_1d or not ema20_1d_prev:
        return None
    if not (closes_1d[-1] > ema20_1d and ema20_1d > ema20_1d_prev):
        return None

    # Filtre 2: tendance horaire haussiere (prix > EMA50 1h)
    ema50_1h = ema(closes_1h, 50)
    ema20_1h = ema(closes_1h, 20)
    if not ema50_1h or not ema20_1h:
        return None
    if closes_1h[-1] <= ema50_1h:
        return None

    # Filtre 3: pas trop etendu au-dessus de la EMA20 1h (eviter d'acheter le top)
    if (px - ema20_1h) / ema20_1h > MAX_DIST_EMA20_1H:
        return None

    # Filtre 4: ATR exploitable
    a = atr_pct(kl1m, 14)
    if a is None or a < MIN_ATR_PCT:
        return None

    # Setup pullback: RSI 1m est descendu < 35 dans les 15 dernieres bougies
    rsis = rsi_series(closes_1m, 14)
    if len(rsis) < 16:
        return None
    if min(rsis[-15:]) >= 35:
        return None

    # Trigger: RSI courant remonte au-dessus de 45 + bougie verte de confirmation
    if not (rsis[-1] > 45 and rsis[-1] > rsis[-2]):
        return None
    if closes_1m[-1] <= closes_1m[-2]:
        return None

    return {"atr_pct": a, "prix": px}


# ---------------------------------------------------------------------
# Reset quotidien et limite de perte
# ---------------------------------------------------------------------
def maj_jour(etat, equity_courante):
    aujourd = time.strftime("%Y-%m-%d", time.gmtime())
    if etat["jour_courant"] != aujourd:
        etat["jour_courant"] = aujourd
        etat["equity_debut_jour"] = equity_courante
        etat["trading_bloque_jour"] = False
        return
    if etat["trading_bloque_jour"]:
        return
    baseline = etat["equity_debut_jour"]
    if baseline > 0:
        dd = (baseline - equity_courante) / baseline * 100
        if dd >= DAILY_LOSS_LIMIT_PCT:
            etat["trading_bloque_jour"] = True
            log_trade(f"LIMITE PERTE QUOTIDIENNE ATTEINTE ({dd:.2f}%) - pas de nouveaux trades aujourd'hui")


# ---------------------------------------------------------------------
# Boucle principale
# ---------------------------------------------------------------------
def main():
    if not PAPER_TRADING and (not API_KEY or not API_SECRET):
        log_error("Mode LIVE mais BINANCE_API_KEY / BINANCE_API_SECRET manquants. Arret.")
        sys.exit(1)

    etat = charger_etat()
    derniere_analyse = 0
    derniere_telegram = 0

    print("=" * 80)
    if PAPER_TRADING:
        print("MODE PAPER TRADING (argent virtuel, donnees reelles)")
    else:
        print("MODE LIVE - TRADING AVEC ARGENT REEL")
    print(f"Capital initial: {CAPITAL_INITIAL:.2f} USDT")
    print(f"Solde charge: {etat['solde_usdt']:.2f} USDT | "
          f"Positions: {len(etat['positions'])} | "
          f"Coffre: {etat['capital_coffre']:.2f} USDT")
    print("=" * 80)

    while True:
        try:
            now = time.time()
            heure_str = time.strftime("%H:%M:%S")

            # ---------- Bouclier haute frequence (3s) ----------
            if etat["positions"]:
                prix = obtenir_prix_actuels(list(etat["positions"].keys()))
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

                    # Time stop
                    duree = now - pos["ts_entree"]
                    time_out = duree >= MAX_HOLD_SECONDS
                    # Hard TP
                    hard_tp = px >= entry * (1 + sl_pct * (ATR_TP_MULT / ATR_SL_MULT))

                    if px <= sl_px or time_out or hard_tp:
                        raison = "TIME" if time_out else ("TP" if hard_tp else "SL/TRAIL")
                        fill = passer_ordre(sym, "SELL",
                                            quantite=pos["quantite_totale"],
                                            ref_price=px)
                        if fill:
                            etat["solde_usdt"] += fill["quote_amount"]
                            pnl = fill["quote_amount"] - pos["montant_investi"]
                            pnl_pct = pnl / pos["montant_investi"] * 100
                            del etat["positions"][sym]
                            log_trade(f"SELL {sym} ({raison}) {pnl_pct:+.2f}% ({pnl:+.2f}$)")
                            if pnl_pct < 0:
                                etat["cooldowns"][sym] = now + COOLDOWN_AFTER_LOSS_SEC
                            sauvegarder_etat(etat)

            # ---------- Radar (60s) ----------
            if now - derniere_analyse >= 60:
                derniere_analyse = now

                # Equity et coffre
                prix_pos = obtenir_prix_actuels(list(etat["positions"].keys())) if etat["positions"] else {}
                val_pos_marche = sum(
                    p["quantite_totale"] * prix_pos.get(s, p["prix_moyen"])
                    for s, p in etat["positions"].items()
                )
                val_pos_invest = sum(p["montant_investi"] for p in etat["positions"].values())
                equity = etat["solde_usdt"] + val_pos_marche
                cap_realise = etat["solde_usdt"] + val_pos_invest

                if cap_realise > etat["plus_haut_capital"]:
                    gain = cap_realise - etat["plus_haut_capital"]
                    etat["capital_coffre"] += gain * (1 - TAUX_REINVESTISSEMENT)
                    etat["plus_haut_capital"] = cap_realise

                cap_travail = max(0.0, cap_realise - etat["capital_coffre"])
                maj_jour(etat, equity)

                # Nettoyage cooldowns expires
                etat["cooldowns"] = {s: t for s, t in etat["cooldowns"].items() if t > now}

                pnl_jour = equity - etat["equity_debut_jour"]
                pnl_jour_pct = (pnl_jour / etat["equity_debut_jour"] * 100
                                if etat["equity_debut_jour"] > 0 else 0)

                print()
                mode = "PAPER" if PAPER_TRADING else "LIVE"
                print(f"[{heure_str}] {mode} | Equity: {equity:.2f}$ ({pnl_jour_pct:+.2f}% jour) | "
                      f"Libre: {etat['solde_usdt']:.2f}$ | "
                      f"Coffre: {etat['capital_coffre']:.2f}$ | "
                      f"Pos: {len(etat['positions'])}/{MAX_POSITIONS}")
                if etat["trading_bloque_jour"]:
                    print(f"  -> Trading bloque (limite quotidienne {DAILY_LOSS_LIMIT_PCT}%)")

                if now - derniere_telegram >= 3600:
                    derniere_telegram = now
                    envoyer_telegram(
                        f"Bilan {mode}\n"
                        f"Equity: {equity:.2f}$ ({pnl_jour_pct:+.2f}% jour)\n"
                        f"Libre: {etat['solde_usdt']:.2f}$ | Coffre: {etat['capital_coffre']:.2f}$\n"
                        f"Positions: {len(etat['positions'])}/{MAX_POSITIONS}"
                    )

                # Scan d'opportunites
                if not etat["trading_bloque_jour"] and len(etat["positions"]) < MAX_POSITIONS:
                    candidats = obtenir_top_opportunites()
                    for sym in candidats:
                        if sym in etat["positions"]:
                            continue
                        if len(etat["positions"]) >= MAX_POSITIONS:
                            break
                        if etat["cooldowns"].get(sym, 0) > now:
                            continue

                        montant = cap_travail * ALLOC_PAR_TRADE
                        if montant < MIN_ORDER_USDT:
                            continue
                        if etat["solde_usdt"] < montant:
                            continue

                        kl1m = obtenir_klines(sym, "1m", 60)
                        kl1h = obtenir_klines(sym, "1h", 100)
                        kl1d = obtenir_klines(sym, "1d", 30)
                        sig = detecter_signal(kl1m, kl1h, kl1d)
                        if not sig:
                            continue

                        a = sig["atr_pct"]
                        sl_pct = max(SL_FLOOR_PCT, a / 100 * ATR_SL_MULT)
                        trail_pct = max(SL_FLOOR_PCT, a / 100 * ATR_TRAIL_MULT)

                        fill = passer_ordre(sym, "BUY",
                                            montant_usdt=montant,
                                            ref_price=sig["prix"])
                        if not fill or fill["filled_qty"] <= 0:
                            continue

                        etat["solde_usdt"] -= fill["quote_amount"]
                        etat["positions"][sym] = {
                            "quantite_totale": fill["filled_qty"],
                            "montant_investi": fill["quote_amount"],
                            "prix_moyen": fill["avg_price"],
                            "prix_max": fill["avg_price"],
                            "ts_entree": now,
                            "sl_pct": sl_pct,
                            "trail_pct": trail_pct,
                            "atr_pct_entree": a,
                            "break_even": False,
                        }
                        log_trade(
                            f"BUY {sym} | PRU {fill['avg_price']:.6f} | "
                            f"{fill['quote_amount']:.2f}$ | "
                            f"SL -{sl_pct*100:.2f}% | trail {trail_pct*100:.2f}% | "
                            f"ATR {a:.2f}%"
                        )
                        time.sleep(0.5)

                sauvegarder_etat(etat)

            time.sleep(3)

        except KeyboardInterrupt:
            log_info("Arret demande, sauvegarde de l'etat...")
            sauvegarder_etat(etat)
            break
        except Exception as e:
            log_error(f"Boucle principale: {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
