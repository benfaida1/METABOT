#!/usr/bin/env python3
"""
trend_d1_bot.py

Bot de Trend Following D1 sur 4 cryptos (BTC + ETH + SOL + BNB).

MODE PAPER TRADING : aucun ordre n'est passe sur Binance. Le bot ne fait
que detecter les signaux et envoyer des alertes Telegram. L'etat des
"positions virtuelles" et le P&L paper sont logges dans un fichier JSON.

Fonctionnement :
    - Une execution par jour (a lancer via cron a 00:30 UTC)
    - Pour chaque coin :
        * Telecharge les 250 dernieres bougies daily
        * Calcule MA50, MA200, plus haut des 20j precedents
        * Detecte signal BUY ou SELL
        * Met a jour l'etat paper (positions virtuelles)
        * Envoie une alerte Telegram si signal
    - A la fin : envoie un resume si au moins 1 signal detecte

Variables d'environnement requises :
    TELEGRAM_TOKEN     - token du bot Telegram
    TELEGRAM_CHAT_ID   - ID du chat ou recevoir les alertes

Lancement :
    python3 trend_d1_bot.py                # une execution
    python3 trend_d1_bot.py --dry-run      # pas de Telegram, juste log
    python3 trend_d1_bot.py --force-check  # force la verif meme si deja
                                              fait aujourd'hui

Installation cron (sur le VPS, 1 execution/jour a 00:30 UTC) :
    crontab -e
    30 0 * * * cd /root/trend_d1_bot && python3 trend_d1_bot.py >> trend_d1.log 2>&1
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


# =====================================================================
# Configuration
# =====================================================================
SYMBOLES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
CAPITAL_VIRTUEL_PAR_COIN = 1000.0   # USD virtuel alloue a chaque coin

BINANCE_BASE = "https://api.binance.com"

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FICHIER_ETAT = "trend_d1_state.json"
FICHIER_LOG  = "trend_d1.log"

# Frais paper trading (pour calcul P&L realiste)
FEE_RATE = 0.001    # 0.1%

# Parametres strategie
MA_RAPIDE     = 50
MA_LENTE      = 200
BREAKOUT_DAYS = 20
MIN_HISTORY   = MA_LENTE + 5  # marge de securite


# =====================================================================
# Logging
# =====================================================================
def log(msg):
    """Log avec timestamp. Affiche stdout + ecrit dans FICHIER_LOG."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{stamp}] {msg}"
    print(line)
    try:
        with open(FICHIER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# =====================================================================
# Telechargement klines Binance
# =====================================================================
def fetch_klines(symbole, limit=250):
    """Recupere les `limit` dernieres klines 1d. Renvoie liste de dict
    ou None en cas d'erreur."""
    url = f"{BINANCE_BASE}/api/v3/klines?symbol={symbole}&interval=1d&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        log(f"  ! erreur fetch {symbole}: {e}")
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
            })
        except (ValueError, IndexError):
            continue
    return out


# =====================================================================
# Telegram
# =====================================================================
def telegram_envoyer(message, dry_run=False):
    """Envoie un message Telegram. Si dry_run : juste log."""
    if dry_run:
        log(f"[DRY-RUN] Telegram : {message}")
        return True
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("  ! TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant. Skip envoi.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        log(f"  ! erreur Telegram : {e}")
        return False


# =====================================================================
# Etat persistant
# =====================================================================
def charger_etat():
    """Charge l'etat (positions paper, historique) ou en cree un neuf."""
    if not os.path.exists(FICHIER_ETAT):
        return {
            "positions": {},          # sym -> {entry_t, entry_price, units, capital_engage}
            "trades_clos": [],        # historique
            "capital_par_coin": CAPITAL_VIRTUEL_PAR_COIN,
            "derniere_verif": None,   # date ISO de la derniere execution
            "version": 1,
        }
    with open(FICHIER_ETAT, "r", encoding="utf-8") as f:
        return json.load(f)


def sauver_etat(etat):
    etat["derniere_verif"] = datetime.now(timezone.utc).isoformat()
    with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, indent=2)


# =====================================================================
# Strategie
# =====================================================================
def detecter_signal(klines):
    """Renvoie 'BUY', 'SELL', ou 'HOLD' selon les regles.

    On se base sur la DERNIERE bougie close (klines[-1]).
    Regles :
        BUY  : close > MA50 ET MA50 > MA200 ET close > max(high 20j precedents)
        SELL : close < MA50
        HOLD : sinon
    """
    if len(klines) < MIN_HISTORY:
        return "HOLD", "pas assez d'historique"

    closes = [k["c"] for k in klines]
    derniere = klines[-1]
    close = derniere["c"]
    ma50  = sum(closes[-MA_RAPIDE:]) / MA_RAPIDE
    ma200 = sum(closes[-MA_LENTE:]) / MA_LENTE
    # Plus haut des 20j PRECEDENTS (pas inclus le jour courant)
    high20 = max(k["h"] for k in klines[-(BREAKOUT_DAYS + 1):-1])

    details = (
        f"close={close:.2f} "
        f"MA50={ma50:.2f} "
        f"MA200={ma200:.2f} "
        f"high20={high20:.2f}"
    )

    if close > ma50 and ma50 > ma200 and close > high20:
        return "BUY", details
    if close < ma50:
        return "SELL", details
    return "HOLD", details


# =====================================================================
# Logique principale
# =====================================================================
def traiter_coin(sym, klines, etat, dry_run):
    """Traite un coin : detecte signal, met a jour etat, envoie alerte."""
    signal, details = detecter_signal(klines)
    derniere = klines[-1]
    close = derniere["c"]
    bougie_date = datetime.fromtimestamp(derniere["t"] / 1000, tz=timezone.utc)
    bougie_date_str = bougie_date.strftime("%Y-%m-%d")

    log(f"  {sym:10} signal={signal:5} {details}")

    position = etat["positions"].get(sym)

    if signal == "BUY" and position is None:
        # Ouverture position virtuelle
        capital = etat["capital_par_coin"]
        fee = capital * FEE_RATE
        units = (capital - fee) / close
        etat["positions"][sym] = {
            "entry_t": derniere["t"],
            "entry_date": bougie_date_str,
            "entry_price": close,
            "units": units,
            "capital_engage": capital,
        }
        msg = (
            f"🟢 <b>BUY {sym}</b>\n"
            f"Date    : {bougie_date_str}\n"
            f"Prix    : {close:.2f} $\n"
            f"Capital : {capital:.0f} $ (virtuel)\n"
            f"Units   : {units:.6f}\n"
            f"MA50    : {details.split('MA50=')[1].split()[0]}\n"
            f"MA200   : {details.split('MA200=')[1].split()[0]}\n"
            f"<i>Paper trade - aucun ordre passe</i>"
        )
        telegram_envoyer(msg, dry_run=dry_run)
        log(f"    -> OUVERTURE paper @ {close:.2f}$")
        return "OPENED"

    elif signal == "SELL" and position is not None:
        # Fermeture position virtuelle
        gross = position["units"] * close
        fee = gross * FEE_RATE
        capital_final = gross - fee
        pnl_abs = capital_final - position["capital_engage"]
        pnl_pct = (capital_final / position["capital_engage"] - 1) * 100
        duree_j = (derniere["t"] - position["entry_t"]) / 86400000

        etat["trades_clos"].append({
            "symbole": sym,
            "entry_date": position["entry_date"],
            "exit_date": bougie_date_str,
            "entry_price": position["entry_price"],
            "exit_price": close,
            "capital_engage": position["capital_engage"],
            "capital_final": capital_final,
            "pnl_abs": pnl_abs,
            "pnl_pct": pnl_pct,
            "duree_jours": duree_j,
        })
        del etat["positions"][sym]

        emoji = "🟢" if pnl_pct > 0 else "🔴"
        msg = (
            f"{emoji} <b>SELL {sym}</b>\n"
            f"Date     : {bougie_date_str}\n"
            f"Prix     : {close:.2f} $\n"
            f"Entree   : {position['entry_price']:.2f} $\n"
            f"P&amp;L     : {pnl_pct:+.2f}% ({pnl_abs:+.2f}$)\n"
            f"Duree    : {duree_j:.0f}j\n"
            f"<i>Paper trade - aucun ordre passe</i>"
        )
        telegram_envoyer(msg, dry_run=dry_run)
        log(f"    -> FERMETURE paper @ {close:.2f}$ "
            f"P&L {pnl_pct:+.2f}% ({pnl_abs:+.2f}$)")
        return "CLOSED"

    # HOLD ou signal qui ne change rien
    if position:
        # Mise a jour mark-to-market
        valeur_actuelle = position["units"] * close
        pnl_pct = (valeur_actuelle / position["capital_engage"] - 1) * 100
        log(f"    HOLD long, P&L courant {pnl_pct:+.2f}%")
    else:
        log(f"    HOLD cash")
    return "NOOP"


def resumer_etat(etat):
    """Renvoie un resume textuel pour Telegram."""
    nb_positions = len(etat["positions"])
    nb_trades = len(etat["trades_clos"])
    pnl_realise = sum(t["pnl_abs"] for t in etat["trades_clos"])

    # P&L non realise des positions ouvertes (on a besoin des prix actuels)
    positions_lines = []
    for sym, pos in etat["positions"].items():
        positions_lines.append(
            f"  {sym}: {pos['entry_date']} @ {pos['entry_price']:.2f}$"
        )

    msg = (
        f"📊 <b>Resume Trend D1 Bot</b>\n"
        f"Coins suivis : {', '.join(SYMBOLES)}\n"
        f"Positions ouvertes : {nb_positions}\n"
    )
    if positions_lines:
        msg += "\n".join(positions_lines) + "\n"
    msg += f"Trades clos : {nb_trades}\n"
    msg += f"P&amp;L realise total : {pnl_realise:+.2f}$\n"
    return msg


# =====================================================================
# Main
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Trend D1 Bot - Paper Trading.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Ne pas envoyer de Telegram, juste logger.")
    ap.add_argument("--force-check", action="store_true",
                    help="Force la verif meme si deja faite aujourd'hui.")
    ap.add_argument("--resume", action="store_true",
                    help="Envoie juste un resume de l'etat actuel.")
    args = ap.parse_args()

    log("=" * 60)
    log("Trend D1 Bot - debut execution")
    log(f"Symboles : {', '.join(SYMBOLES)}")
    log(f"Dry run  : {args.dry_run}")

    etat = charger_etat()

    if args.resume:
        msg = resumer_etat(etat)
        telegram_envoyer(msg, dry_run=args.dry_run)
        log("Resume envoye. Sortie.")
        return

    # Idempotence : si deja verifie aujourd'hui, on skip (sauf --force-check)
    if etat.get("derniere_verif") and not args.force_check:
        derniere = datetime.fromisoformat(etat["derniere_verif"])
        if derniere.date() == datetime.now(timezone.utc).date():
            log("Deja verifie aujourd'hui. Utilise --force-check pour relancer.")
            return

    nb_actions = 0
    actions_par_sym = {}
    for sym in SYMBOLES:
        log(f"\nTraitement {sym}...")
        klines = fetch_klines(sym, limit=250)
        if not klines or len(klines) < MIN_HISTORY:
            log(f"  ! Pas assez de donnees ({len(klines) if klines else 0}). Skip.")
            continue
        action = traiter_coin(sym, klines, etat, dry_run=args.dry_run)
        actions_par_sym[sym] = action
        if action != "NOOP":
            nb_actions += 1
        time.sleep(0.2)   # politesse rate-limit

    sauver_etat(etat)

    # Envoie un resume seulement s'il y a eu au moins 1 action ce jour
    if nb_actions > 0:
        msg = resumer_etat(etat)
        telegram_envoyer(msg, dry_run=args.dry_run)

    log(f"\nFin execution. {nb_actions} action(s) ce jour.")
    log("=" * 60)


if __name__ == "__main__":
    main()
