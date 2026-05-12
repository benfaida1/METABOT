#!/usr/bin/env python3
"""
TELEGRAM_BOT - Interface interactive pour METABOT.

Tourne en parallele de metabot.py et permet de piloter / surveiller le
bot de trading depuis Telegram (donc depuis ton telephone via l'app
Telegram, deja installee sur iOS/Android).

Commandes disponibles :
  /start       - message d'accueil + liste des commandes
  /help        - liste des commandes
  /status      - equity, capital libre, coffre, positions, P&L jour
  /positions   - detail des positions ouvertes avec P&L en temps reel
  /trades [N]  - derniers N trades (defaut 10)
  /equity      - resume capital (initial, actuel, gain, drawdown)
  /coffre      - montant en coffre + part du capital protege
  /pause       - bloque l'ouverture de nouveaux trades (positions ouvertes
                 continuent d'etre suivies pour SL/TP)
  /resume      - reprend le trading normal
  /pid         - PID du metabot et uptime (utile pour debug)
  /ping        - test de connectivite

Securite :
  - Seul le compte Telegram avec TELEGRAM_CHAT_ID configure peut
    interagir. Tout autre utilisateur est ignore (et logge).
  - Aucun mot de passe en clair n'est echange : l'autorisation se
    fait via le chat_id Telegram, qui est lie au numero de telephone
    de l'utilisateur (non falsifiable cote Telegram).

Variables d'environnement requises :
  TELEGRAM_TOKEN     - token du bot (cree via @BotFather)
  TELEGRAM_CHAT_ID   - ID numerique de ton compte Telegram

Utilisation :
  python3 telegram_bot.py
  (peut tourner via nohup, screen, ou systemd ; voir doc)
"""
import json
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
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Fichiers partages avec metabot.py (doivent etre dans le meme dossier)
FICHIER_ETAT     = "etat_bot.json"
FICHIER_TRADES   = "historique_trades.txt"
FICHIER_PAUSE    = "bot_pause.flag"
FICHIER_LOG      = "telegram_bot.log"

POLL_TIMEOUT     = 30  # long polling
HTTP_TIMEOUT     = 35
MAX_TRADES_SHOW  = 30  # plafond pour /trades N

API_BASE         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# =====================================================================
# Logging
# =====================================================================
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    try:
        with open(FICHIER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# =====================================================================
# Telegram API
# =====================================================================
def tg_request(method, params=None, timeout=HTTP_TIMEOUT):
    url = f"{API_BASE}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, socket.timeout, json.JSONDecodeError) as e:
        log(f"tg_request {method}: {e}")
        return None


def send_message(chat_id, text, parse_mode="Markdown"):
    # Telegram limite : 4096 caracteres par message
    if len(text) > 4000:
        text = text[:3990] + "\n... (tronque)"
    return tg_request("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    })


def get_updates(offset):
    return tg_request("getUpdates", {
        "offset": offset,
        "timeout": POLL_TIMEOUT,
        "allowed_updates": json.dumps(["message"]),
    }, timeout=POLL_TIMEOUT + 5)


# =====================================================================
# Lecture des fichiers d'etat du metabot
# =====================================================================
def lire_etat():
    if not os.path.exists(FICHIER_ETAT):
        return None
    try:
        with open(FICHIER_ETAT, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def lire_trades_recents(n=10):
    """Renvoie les n dernieres lignes de l'historique de trades."""
    if not os.path.exists(FICHIER_TRADES):
        return []
    try:
        with open(FICHIER_TRADES, "r", encoding="utf-8") as f:
            lines = [l.rstrip() for l in f if l.strip()]
        return lines[-n:]
    except OSError:
        return []


def obtenir_prix_actuels(symboles):
    """Recupere les prix en temps reel depuis Binance pour les positions ouvertes."""
    if not symboles:
        return {}
    sym_param = json.dumps(list(symboles), separators=(",", ":"))
    url = ("https://api.binance.com/api/v3/ticker/price?symbols="
           + urllib.parse.quote(sym_param))
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, socket.timeout, json.JSONDecodeError) as e:
        log(f"obtenir_prix_actuels: {e}")
        return {}
    if isinstance(data, dict):
        return {data["symbol"]: float(data["price"])}
    return {item["symbol"]: float(item["price"]) for item in data}


def calcul_equity(etat):
    """Renvoie (equity, valeur_positions, prix_dict)."""
    if not etat:
        return 0.0, 0.0, {}
    positions = etat.get("positions", {})
    prix = obtenir_prix_actuels(list(positions.keys())) if positions else {}
    val_pos = sum(
        p["quantite_totale"] * prix.get(s, p["prix_moyen"])
        for s, p in positions.items()
    )
    equity = etat.get("solde_usdt", 0.0) + val_pos
    return equity, val_pos, prix


# =====================================================================
# Formatage des reponses
# =====================================================================
WELCOME = (
    "Bonjour, je suis le pilote de ton *METABOT* de trading.\n\n"
    "Commandes disponibles :\n"
    "/status - vue d'ensemble (equity, P&L, positions)\n"
    "/positions - detail des trades ouverts\n"
    "/trades [N] - derniers N trades (defaut 10)\n"
    "/equity - resume capital et drawdown\n"
    "/coffre - capital mis de cote (protege)\n"
    "/pause - stop d'ouvrir de nouveaux trades\n"
    "/resume - reprend le trading normal\n"
    "/pid - PID + uptime du metabot\n"
    "/ping - test de connectivite\n"
    "/help - revoir cette liste"
)


def fmt_status():
    etat = lire_etat()
    if not etat:
        return "Etat introuvable. Le metabot n'a peut-etre pas encore demarre."
    equity, val_pos, _ = calcul_equity(etat)
    base = etat.get("equity_debut_jour", equity) or equity
    pnl_jour = equity - base
    pnl_jour_pct = (pnl_jour / base * 100) if base > 0 else 0
    cap_init = etat.get("plus_haut_capital", equity)
    coffre = etat.get("capital_coffre", 0.0)
    nb_pos = len(etat.get("positions", {}))
    pause = "OUI" if os.path.exists(FICHIER_PAUSE) else "non"
    bloque = "OUI" if etat.get("trading_bloque_jour") else "non"
    dd = "OUI" if etat.get("drawdown_atteint") else "non"

    return (
        f"*Status du METABOT*\n"
        f"```\n"
        f"Equity      : {equity:.2f}$ ({pnl_jour_pct:+.2f}% jour)\n"
        f"Libre       : {etat['solde_usdt']:.2f}$\n"
        f"En position : {val_pos:.2f}$ ({nb_pos}/3)\n"
        f"Coffre      : {coffre:.2f}$\n"
        f"Plus haut   : {cap_init:.2f}$\n"
        f"```\n"
        f"Pause manuelle : {pause}\n"
        f"Bloque (jour)  : {bloque}\n"
        f"Drawdown max   : {dd}"
    )


def fmt_positions():
    etat = lire_etat()
    if not etat:
        return "Etat introuvable."
    positions = etat.get("positions", {})
    if not positions:
        return "Aucune position ouverte."
    _, _, prix = calcul_equity(etat)
    lignes = ["*Positions ouvertes*\n```"]
    for sym, p in positions.items():
        px = prix.get(sym, p["prix_moyen"])
        pnl_pct = (px - p["prix_moyen"]) / p["prix_moyen"] * 100
        pnl_usd = (px - p["prix_moyen"]) * p["quantite_totale"]
        duree_min = (time.time() - p["ts_entree"]) / 60
        be = " [BE]" if p.get("break_even") else ""
        lignes.append(
            f"{sym} ({p['strategie']}){be}\n"
            f"  PRU   : {p['prix_moyen']:.6f}\n"
            f"  Now   : {px:.6f}\n"
            f"  P&L   : {pnl_pct:+.2f}% ({pnl_usd:+.2f}$)\n"
            f"  SL    : -{p['sl_pct']*100:.2f}% | trail {p['trail_pct']*100:.2f}%\n"
            f"  Duree : {duree_min:.0f} min"
        )
    lignes.append("```")
    return "\n".join(lignes)


def fmt_trades(n=10):
    n = max(1, min(n, MAX_TRADES_SHOW))
    lines = lire_trades_recents(n)
    if not lines:
        return "Aucun trade dans l'historique."
    return f"*Derniers {len(lines)} trades*\n```\n" + "\n".join(lines) + "\n```"


def fmt_equity():
    etat = lire_etat()
    if not etat:
        return "Etat introuvable."
    equity, val_pos, _ = calcul_equity(etat)
    plus_haut = etat.get("plus_haut_capital", equity)
    coffre = etat.get("capital_coffre", 0.0)
    base_jour = etat.get("equity_debut_jour", equity) or equity
    pnl_jour = equity - base_jour
    pnl_jour_pct = (pnl_jour / base_jour * 100) if base_jour > 0 else 0
    dd = ((plus_haut - equity) / plus_haut * 100) if plus_haut > 0 else 0
    # Compter trades gagnants/perdants depuis l'historique
    lines = lire_trades_recents(500)
    wins = sum(1 for l in lines if "SELL" in l and "+" in l.split("SELL")[1].split("(")[0])
    losses = sum(1 for l in lines if "SELL" in l and "-" in l.split("SELL")[1].split("(")[0])
    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0
    return (
        f"*Resume Equity*\n```\n"
        f"Equity actuelle : {equity:.2f}$\n"
        f"  - libre       : {etat['solde_usdt']:.2f}$\n"
        f"  - en position : {val_pos:.2f}$\n"
        f"Coffre          : {coffre:.2f}$\n"
        f"Plus haut       : {plus_haut:.2f}$\n"
        f"Drawdown        : -{dd:.2f}%\n"
        f"\n"
        f"P&L jour        : {pnl_jour:+.2f}$ ({pnl_jour_pct:+.2f}%)\n"
        f"\n"
        f"Trades fermes   : {total}\n"
        f"  - gagnants    : {wins}\n"
        f"  - perdants    : {losses}\n"
        f"  - win rate    : {wr:.1f}%\n"
        f"```"
    )


def fmt_coffre():
    etat = lire_etat()
    if not etat:
        return "Etat introuvable."
    coffre = etat.get("capital_coffre", 0.0)
    equity, _, _ = calcul_equity(etat)
    pct = (coffre / equity * 100) if equity > 0 else 0
    return (
        f"*Coffre-fort*\n```\n"
        f"Capital protege : {coffre:.2f}$\n"
        f"Soit             : {pct:.1f}% de l'equity totale\n"
        f"```\n"
        f"_Le coffre est alimenté a chaque nouveau plus haut : "
        f"20% du gain est mis de cote et ne sera plus jamais expose au marche._"
    )


def fmt_pid():
    """Trouve le process metabot.py et renvoie PID + uptime."""
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").decode()
                if "metabot.py" in cmd:
                    with open(f"/proc/{entry}/stat", "r") as f:
                        starttime = int(f.read().split()[21])
                    with open("/proc/uptime", "r") as f:
                        uptime = float(f.read().split()[0])
                    clk = os.sysconf("SC_CLK_TCK")
                    process_uptime = uptime - (starttime / clk)
                    h = int(process_uptime // 3600)
                    m = int((process_uptime % 3600) // 60)
                    return (
                        f"*Metabot process*\n```\n"
                        f"PID    : {entry}\n"
                        f"Uptime : {h}h {m}min\n"
                        f"Cmd    : {cmd.strip()[:80]}\n"
                        f"```"
                    )
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass
    return "*Metabot* : process introuvable. Le bot tourne-t-il vraiment ?"


def cmd_pause():
    try:
        with open(FICHIER_PAUSE, "w") as f:
            f.write(f"paused at {_now()}\n")
        return ("Pause *activee*. Le bot ne va plus ouvrir de nouveaux trades.\n"
                "Les positions deja ouvertes restent surveillees (SL/TP/trail).\n"
                "Tape /resume pour reprendre.")
    except OSError as e:
        return f"Erreur lors de la pause : {e}"


def cmd_resume():
    if os.path.exists(FICHIER_PAUSE):
        try:
            os.remove(FICHIER_PAUSE)
            return "Reprise *activee*. Le bot va de nouveau scanner et ouvrir des trades."
        except OSError as e:
            return f"Erreur : {e}"
    return "Le bot n'etait pas en pause."


# =====================================================================
# Dispatcher de commandes
# =====================================================================
def traiter_message(msg):
    chat = msg.get("chat", {}) or {}
    chat_id = str(chat.get("id", ""))
    user = msg.get("from", {}) or {}
    user_name = user.get("username") or user.get("first_name") or "inconnu"
    text = (msg.get("text") or "").strip()

    # Filtre d'autorisation : seul le chat_id configure peut interagir
    if chat_id != str(TELEGRAM_CHAT_ID):
        log(f"REJET acces non autorise : chat_id={chat_id} user={user_name} text={text!r}")
        return

    log(f"Commande de {user_name}: {text!r}")

    if not text.startswith("/"):
        send_message(chat_id, "Envoie une commande commencant par /. Tape /help pour la liste.")
        return

    # Extrait commande + args, retire le @botname s'il est present
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    try:
        if cmd in ("/start", "/help"):
            send_message(chat_id, WELCOME)
        elif cmd == "/status":
            send_message(chat_id, fmt_status())
        elif cmd == "/positions":
            send_message(chat_id, fmt_positions())
        elif cmd == "/trades":
            n = 10
            if args:
                try:
                    n = int(args[0])
                except ValueError:
                    pass
            send_message(chat_id, fmt_trades(n))
        elif cmd == "/equity":
            send_message(chat_id, fmt_equity())
        elif cmd == "/coffre":
            send_message(chat_id, fmt_coffre())
        elif cmd == "/pause":
            send_message(chat_id, cmd_pause())
        elif cmd == "/resume":
            send_message(chat_id, cmd_resume())
        elif cmd == "/pid":
            send_message(chat_id, fmt_pid())
        elif cmd == "/ping":
            send_message(chat_id, "pong (bot Telegram operationnel)")
        else:
            send_message(chat_id, f"Commande inconnue : `{cmd}`\nTape /help.")
    except Exception as e:
        log(f"Erreur traitement commande {cmd}: {type(e).__name__}: {e}")
        send_message(chat_id, f"Erreur interne : {type(e).__name__}")


# =====================================================================
# Boucle principale (long polling)
# =====================================================================
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERREUR : TELEGRAM_TOKEN et TELEGRAM_CHAT_ID doivent etre definis.")
        print("Exemples :")
        print("  export TELEGRAM_TOKEN='123456:ABC-DEF...'")
        print("  export TELEGRAM_CHAT_ID='123456789'")
        sys.exit(1)

    log(f"Telegram bot demarre. chat_id autorise: {TELEGRAM_CHAT_ID}")

    # Test du token + drain des messages en attente
    me = tg_request("getMe")
    if not me or not me.get("ok"):
        log(f"Token Telegram invalide : {me}")
        sys.exit(1)
    bot_name = me["result"].get("username", "?")
    log(f"Connecte en tant que @{bot_name}")
    send_message(TELEGRAM_CHAT_ID,
                 f"*Bot pilote demarre* (@{bot_name})\nTape /help pour la liste des commandes.")

    offset = 0
    while True:
        try:
            updates = get_updates(offset)
            if not updates or not updates.get("ok"):
                time.sleep(3)
                continue
            for upd in updates.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if msg:
                    traiter_message(msg)
        except KeyboardInterrupt:
            log("Arret demande.")
            break
        except Exception as e:
            log(f"Boucle: {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
