import urllib.request
import urllib.parse
import json
import time
import os
import hmac
import hashlib
import math

# ==============================================================================
# 🎚️ L'INTERRUPTEUR PRINCIPAL (LE MODE DU BOT)
# ==============================================================================
# True  = Argent Virtuel (Mais graphiques et prix 100% RÉELS en direct)
# False = Argent Réel (Nécessite tes vraies clés API Binance ci-dessous)
PAPER_TRADING = True

# ==============================================================================
# 🔑 TES CLÉS API BINANCE (VRAI COMPTE)
# ==============================================================================
API_KEY = "TES_VRAIES_CLES_ICI_QUAND_TU_SERAS_PRET"
API_SECRET = "TON_VRAI_SECRET_ICI"

# Le bot est maintenant branché directement sur la salle des marchés de Wall Street
BASE_URL = "https://api.binance.com"

# ==============================================================================
# 📱 CONFIGURATION TELEGRAM
# ==============================================================================
TELEGRAM_TOKEN = "8622927710:AAGl3VA41cOZZ_XyhsLBF48z_9ANJ6iCVY0"
TELEGRAM_CHAT_ID = "5884257994"

# ==============================================================================
# ⚙️ CONFIGURATION ET MONEY MANAGEMENT
# ==============================================================================
ALLOCATION_PAR_CRYPTO = 0.10
TAUX_REINVESTISSEMENT = 0.80
MAX_CANDIDATS_SCAN = 15
MAX_PALIERS = 4
STOP_LOSS_GLOBAL = 0.98
DECLENCHEMENT_TRAILING_STOP = 1.01
SEUIL_BREAK_EVEN = 1.015
FICHIER_SAUVEGARDE = "etat_bot.json"
FICHIER_TRADES = "historique_trades.txt"
MIN_ORDER_USDT = 11.0
CAPITAL_INITIAL = 500.0

def envoyer_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({'chat_id': TELEGRAM_CHAT_ID, 'text': message}).encode('utf-8')
        urllib.request.urlopen(url, data=data)
    except: pass

def log_trade(message):
    print(f"\n{message}\n")
    try:
        with open(FICHIER_TRADES, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except: pass
    envoyer_telegram(message)

def obtenir_prix_actuels():
    try:
        url = f"{BASE_URL}/api/v3/ticker/price"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read().decode())
            return {item['symbol']: float(item['price']) for item in data}
    except: return {}

def arrondir_lot_size(symbole, quantite):
    try:
        url = f"{BASE_URL}/api/v3/exchangeInfo?symbol={symbole}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as r:
            info = json.loads(r.read().decode())
            for filtre in info['symbols'][0]['filters']:
                if filtre['filterType'] == 'LOT_SIZE':
                    step_size = float(filtre['stepSize'])
                    quantite_nette = math.floor(quantite / step_size) * step_size
                    if step_size.is_integer(): return f"{int(quantite_nette)}"
                    else:
                        str_step = f"{step_size:f}".rstrip('0')
                        decimals = len(str_step.split('.')[1]) if '.' in str_step else 0
                        return f"{quantite_nette:.{decimals}f}"
    except: pass
    return f"{int(quantite)}"

def passer_ordre(symbole, side, quantite=None, montant_usdt=None):
    # 🛑 BLOCAGE PAPER TRADING 🛑
    if PAPER_TRADING:
        # En mode Paper Trading, on simule que l'ordre Binance est passé avec succès
        return True

    # 🟢 VRAI TRADING (Mode Live) 🟢
    endpoint = "/api/v3/order"
    timestamp = int(time.time() * 1000)
    params = {"symbol": symbole, "side": side, "type": "MARKET", "timestamp": timestamp}
    if side == "BUY" and montant_usdt: params["quoteOrderQty"] = f"{montant_usdt:.2f}"
    elif side == "SELL" and quantite: params["quantity"] = arrondir_lot_size(symbole, quantite)

    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(API_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
    query_string += f"&signature={signature}"
    url = f"{BASE_URL}{endpoint}"
    requete = urllib.request.Request(url, data=query_string.encode('utf-8'), method="POST")
    requete.add_header('X-MBX-APIKEY', API_KEY)

    try:
        with urllib.request.urlopen(requete) as reponse: return True
    except Exception as e:
        print(f"❌ Erreur API Binance : {e}")
        return False

def charger_etat():
    if os.path.exists(FICHIER_SAUVEGARDE):
        try:
            with open(FICHIER_SAUVEGARDE, 'r') as f:
                d = json.load(f)
                return (
                    d.get('solde_usdt', CAPITAL_INITIAL),
                    d.get('positions', {}),
                    d.get('plus_haut_capital', CAPITAL_INITIAL),
                    d.get('capital_coffre', 0.0)
                )
        except: pass
    return CAPITAL_INITIAL, {}, CAPITAL_INITIAL, 0.0

def sauvegarder_etat(solde, pos, highest_cap, coffre):
    try:
        with open(FICHIER_SAUVEGARDE, 'w') as f:
            json.dump({
                'solde_usdt': solde,
                'positions': pos,
                'plus_haut_capital': highest_cap,
                'capital_coffre': coffre
            }, f, indent=4)
    except: pass

def obtenir_klines_completes(symbole, intervalle="1m", limite=60):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbole}&interval={intervalle}&limit={limite}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read().decode())
            return [{'h': float(b[2]), 'l': float(b[3]), 'c': float(b[4]), 'v': float(b[7])} for b in data]
    except: return None

def evaluer_tendance(prix):
    if not prix or len(prix) < 25: return False
    return (sum(prix[-7:]) / 7) > (sum(prix[-25:]) / 25)

def calculer_rsi(prix, p=14):
    if not prix or len(prix) < p + 1: return 50
    gains = [max(0, prix[i] - prix[i-1]) for i in range(1, len(prix))]
    pertes = [max(0, prix[i-1] - prix[i]) for i in range(1, len(prix))]
    ag, al = sum(gains[:p]) / p, sum(pertes[:p]) / p
    for i in range(p, len(gains)):
        ag, al = (ag * (p - 1) + gains[i]) / p, (al * (p - 1) + pertes[i]) / p
    return 100 - (100 / (1 + (ag / al))) if al != 0 else 100

def analyser_volatilite_atr(klines, period=14):
    if not klines or len(klines) < period + 15: return 1.0, False
    trs = [max(klines[i]['h'] - klines[i]['l'], abs(klines[i]['h'] - klines[i-1]['c']), abs(klines[i]['l'] - klines[i-1]['c'])) for i in range(1, len(klines))]
    atrs = []
    for i in range(period, len(trs) + 1): atrs.append(sum(trs[i-period:i]) / period)
    current_atr_pct = (atrs[-1] / klines[-1]['c']) * 100
    recent_atrs = atrs[-15:]
    max_a, min_a = max(recent_atrs), min(recent_atrs)
    variation_atr = (max_a - min_a) / min_a if min_a > 0 else 0
    est_en_range_atr = (variation_atr < 0.15) and (current_atr_pct < 0.10)
    return current_atr_pct, est_en_range_atr

def obtenir_top_opportunites():
    try:
        url = f"{BASE_URL}/api/v3/ticker/24hr"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read().decode())

        candidats_base = []
        for item in data:
            sym = item['symbol']
            if sym.endswith('USDT') and "UPUSDT" not in sym and "DOWNUSDT" not in sym:
                vol, pct = float(item['quoteVolume']), float(item['priceChangePercent'])
                # FILTRE RÉEL BINANCE : 15 Millions de $ de liquidité minimum !
                if vol > 15000000 and pct > 0:
                    candidats_base.append({'symbol': sym, 'volume': vol, 'pct': pct})

        candidats_base.sort(key=lambda x: x['volume'], reverse=True)
        top_candidats = candidats_base[:40]

        if not top_candidats:
            return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

        candidats_rvol = []
        for c in top_candidats:
            kl = obtenir_klines_completes(c['symbol'], "1d", limite=15)
            if kl and len(kl) > 5:
                vols_passes = [k['v'] for k in kl[:-1]]
                vol_moy = sum(vols_passes) / len(vols_passes) if vols_passes else 1
                rvol = c['volume'] / vol_moy
                # Filtre d'explosion : RVOL > 1.2x (20% plus de volume que la normale)
                if rvol >= 1.2:
                    c['rvol'] = rvol
                    candidats_rvol.append(c)

        candidats_rvol.sort(key=lambda x: x.get('rvol', 0), reverse=True)
        return [c['symbol'] for c in candidats_rvol[:MAX_CANDIDATS_SCAN]]
    except: return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# ==============================================================================
# 🚀 BOUCLE PRINCIPALE HYBRIDE
# ==============================================================================
solde_usdt, positions, plus_haut_capital, capital_coffre = charger_etat()
derniere_analyse_radar = 0
compteur_rapport_horaire = 0

print("\n" + "="*80)
if PAPER_TRADING:
    print("🟢 MODE PAPER TRADING ACTIVÉ : Données 100% Réelles, Argent Virtuel.")
    print("   (Les frais de 0.1% de Binance seront simulés pour être réaliste).")
else:
    print("🔴 DANGER : MODE LIVE ACTIVÉ. LE BOT TRADE AVEC VOTRE VRAI ARGENT !")
print("="*80 + "\n")

while True:
    heure_actuelle = time.time()
    heure_str = time.strftime('%H:%M:%S')

    # 🛡️ MOTEUR 1 : LE BOUCLIER HAUTE FRÉQUENCE (3s)
    if positions:
        prix_directs = obtenir_prix_actuels()
        for symbole, t in list(positions.items()):
            if symbole not in prix_directs: continue
            px_actuel = prix_directs[symbole]

            if px_actuel > t['prix_max']: t['prix_max'] = px_actuel

            if not t.get('break_even', False) and px_actuel >= t['prix_moyen'] * SEUIL_BREAK_EVEN:
                t['break_even'] = True
                log_trade(f"  >>> 🛡️ [{heure_str}] BREAK-EVEN ACTIVÉ | {symbole} (+1.50%) | Risque Zéro !")

            sl_px = t['prix_moyen'] * 1.0025 if t.get('break_even') else t['prix_moyen'] * STOP_LOSS_GLOBAL
            m_trail = t.get('marge_trailing', 0.015)

            if px_actuel <= sl_px or (t['prix_max'] > t['prix_moyen'] * DECLENCHEMENT_TRAILING_STOP and px_actuel <= t['prix_max'] * (1.0 - m_trail)):
                if passer_ordre(symbole, "SELL", quantite=t['quantite_totale']):
                    valeur_revente = t['quantite_totale'] * px_actuel

                    # Simulation réaliste des frais Binance (0.1%)
                    if PAPER_TRADING: valeur_revente *= 0.999

                    solde_usdt += valeur_revente
                    prof = ((valeur_revente - t['montant_investi']) / t['montant_investi']) * 100

                    raison = "Frais remboursés" if t.get('break_even', False) and prof <= 0.5 else "Gain" if prof > 0 else "Stop-Loss / Sécurité Crash"

                    del positions[symbole]

                    capital_exact = solde_usdt + sum(pos['montant_investi'] for pos in positions.values())
                    dashboard_texte = f"🏦 Capital Réel: {capital_exact:.2f}$"

                    log_trade(f"  >>> 🔴 [{heure_str}] VENTE URGENCE ({raison}) | {symbole} | Final: {prof:+.2f}%\n{dashboard_texte}")
                    sauvegarder_etat(solde_usdt, positions, plus_haut_capital, capital_coffre)

    # 🔎 MOTEUR 2 : LE RADAR STRATÉGIQUE (60s)
    if heure_actuelle - derniere_analyse_radar >= 60:
        derniere_analyse_radar = heure_actuelle
        compteur_rapport_horaire += 1

        candidats_du_jour = obtenir_top_opportunites()
        symboles_a_scanner = sorted(list(set(list(positions.keys()) + candidats_du_jour)), key=lambda s: (0 if s in positions else 1, s))

        cap_reel = solde_usdt + sum(t['montant_investi'] for t in positions.values())

        if cap_reel > plus_haut_capital:
            nouveau_profit = cap_reel - plus_haut_capital
            ajout_coffre = nouveau_profit * (1 - TAUX_REINVESTISSEMENT)
            capital_coffre += ajout_coffre
            plus_haut_capital = cap_reel

        cap_travail = cap_reel - capital_coffre

        prix_actuels = obtenir_prix_actuels()
        val_latente = sum(t['quantite_totale'] * prix_actuels.get(s, t['prix_moyen']) for s, t in positions.items())
        cap_latent = solde_usdt + val_latente
        profit_latent_usd = cap_latent - cap_reel

        dashboard_texte = (
            f"📊 PORTFEUILLE ({'PAPER TRADING' if PAPER_TRADING else 'LIVE'}) | {heure_str}\n"
            f"🏦 Capital Réel: {cap_reel:.2f}$\n"
            f"📈 Latent: {cap_latent:.2f}$ ({profit_latent_usd:+.2f}$)\n"
            f"💵 Libre: {solde_usdt:.2f}$ | 🔒 Coffre: {capital_coffre:.2f}$"
        )

        print(f"\n" + "="*120)
        print(f"🕒 [{heure_str}] RADAR BINANCE RÉEL | {'PAPER TRADING 🟢' if PAPER_TRADING else 'LIVE TRADING 🔴'} | ⚡ RVOL > 1.2x | Vol > 15M$")
        print(f"🏦 Capital Réel: {cap_reel:.2f}$ | 📈 Latent: {cap_latent:.2f}$ ({profit_latent_usd:+.2f}$)")
        print(f"💵 Libre: {solde_usdt:.2f}$ | 🔒 Coffre (20%): {capital_coffre:.2f}$")
        print("="*120)

        if compteur_rapport_horaire >= 60:
            envoyer_telegram(f"⏱️ BILAN HORAIRE :\n{dashboard_texte}")
            compteur_rapport_horaire = 0

        for symbole in symboles_a_scanner:
            kl1m, kl5m, kl1h, kl1d = obtenir_klines_completes(symbole, "1m", limite=60), obtenir_klines_completes(symbole, "5m", limite=30), obtenir_klines_completes(symbole, "1h", limite=48), obtenir_klines_completes(symbole, "1d", limite=30)
            if not kl1m or not kl5m or not kl1h or not kl1d: continue

            px_actuel, px1m = kl1m[-1]['c'], [k['c'] for k in kl1m]
            tend1d, tend1h, tend5m = evaluer_tendance([k['c'] for k in kl1d]), evaluer_tendance([k['c'] for k in kl1h]), evaluer_tendance([k['c'] for k in kl1m])
            s7_1m, s25_1m = sum(px1m[-7:])/7, sum(px1m[-25:])/25
            s7_p, s25_p = sum(px1m[-8:-1])/7, sum(px1m[-26:-1])/25
            rsi_1m = calculer_rsi(px1m)

            atr_pct, atr_est_plat = analyser_volatilite_atr(kl1m)

            vols_passes_1d = [k['v'] for k in kl1d[:-1]]
            vol_moy_1d = sum(vols_passes_1d) / len(vols_passes_1d) if vols_passes_1d else 1
            vol_24h_actuel = sum([k['v'] for k in kl1h[-24:]]) if len(kl1h) >= 24 else kl1d[-1]['v']
            rvol_affichage = vol_24h_actuel / vol_moy_1d if vol_moy_1d > 0 else 1.0

            if not tend1d: mode_marche = "CRASH"
            elif atr_est_plat or (tend1d and not tend1h): mode_marche = "RANGE"
            else: mode_marche = "TENDANCE"

            alloc_pct, force_signal = 0.05, "Prudent (5%)"
            if 40 <= rsi_1m <= 55 and mode_marche == "TENDANCE":
                alloc_pct, force_signal = 0.10, "Standard (10%)"
            if alloc_pct == 0.10 and ((s7_1m - s25_1m) / s25_1m) * 100 > 0.08:
                alloc_pct, force_signal = 0.15, "Or (15%)"

            budget_dyn = cap_travail * alloc_pct
            palier_dyn = max(MIN_ORDER_USDT, budget_dyn / MAX_PALIERS)

            if symbole in positions:
                t = positions[symbole]
                t['marge_trailing'] = max(1.0, atr_pct * 1.5) / 100.0

                # Le profit tient compte virtuellement de ce qu'il resterait après les frais de revente
                valeur_actuelle_nette = t['quantite_totale'] * px_actuel * (0.999 if PAPER_TRADING else 1.0)
                prof = ((valeur_actuelle_nette - t['montant_investi']) / t['montant_investi']) * 100

                sl_px = t['prix_moyen'] * 1.0025 if t.get('break_even') else t['prix_moyen'] * STOP_LOSS_GLOBAL
                etat_risque = "🛡️ BE Actif" if t.get('break_even') else f"SL: {sl_px:.4f}$"

                print(f"🟢 [ACHETÉ] {symbole:<8} | Pos: {t['paliers']}/{MAX_PALIERS} | Inv: {t['montant_investi']:6.2f}$ | PRU: {t['prix_moyen']:8.4f}$ | Actuel: {px_actuel:8.4f}$ | Profit: {prof:>+6.2f}% | {etat_risque}")

                m_dca = max(2.0, atr_pct * 3) / 100.0
                if t['paliers'] < MAX_PALIERS and px_actuel <= t['dernier_prix_achat'] * (1.0 - m_dca) and solde_usdt >= t['taille_palier_fixee']:
                    if passer_ordre(symbole, "BUY", montant_usdt=t['taille_palier_fixee']):
                        solde_usdt -= t['taille_palier_fixee']

                        quantite_achetee = t['taille_palier_fixee'] / px_actuel
                        if PAPER_TRADING: quantite_achetee *= 0.999 # Simule les frais d'achat

                        t['quantite_totale'] += quantite_achetee
                        t['montant_investi'] += t['taille_palier_fixee']
                        t['dernier_prix_achat'], t['prix_moyen'] = px_actuel, t['montant_investi'] / t['quantite_totale']
                        t['paliers'] += 1
                        log_trade(f"  >>> 🟢 [{heure_str}] DCA | {symbole} | Palier {t['paliers']}")
            else:
                feu_1d, feu_1h, feu_5m, feu_1m = "🟢" if tend1d else "🔴", "🟢" if tend1h else "🔴", "🟢" if tend5m else "🔴", "🟢" if s7_1m > s25_1m else "🔴"
                croisement = (s7_p <= s25_p and s7_1m > s25_1m)
                flash_entry = (force_signal == "Or (15%)" and s7_1m > s25_1m and rsi_1m < 60)

                signal_trend = (mode_marche == "TENDANCE" and tend5m and rsi_1m < 65 and (croisement or flash_entry))
                signal_range = (mode_marche == "RANGE" and rsi_1m < 35 and s7_1m > s25_1m)

                if mode_marche == "CRASH": statut = "Bloqué (Macro 🔴)"
                elif mode_marche == "RANGE" and not signal_range: statut = "Range (Attente Support)"
                elif mode_marche == "TENDANCE" and not signal_trend: statut = "Attente (Pullback Trend)"
                else: statut = f"🔥 Gâchette armée ({'Rebond Range' if signal_range else force_signal})"

                etat_atr = "(Plat/Squeeze)" if atr_est_plat else ""
                print(f"🔎 [RADAR]  {symbole:<8} | MTF: 1D:{feu_1d} 1H:{feu_1h} 5m:{feu_5m} | RVOL: {rvol_affichage:4.1f}x | ATR: {atr_pct:.2f}% {etat_atr:<10} | Mode: {mode_marche:<8} | {statut}")

                if signal_trend or signal_range:
                    type_signal = force_signal if signal_trend else "Support Range (10%)"
                    alloc_utilisee = alloc_pct if signal_trend else 0.10
                    budget_final = cap_travail * alloc_utilisee
                    palier_final = max(MIN_ORDER_USDT, budget_final / MAX_PALIERS)

                    if solde_usdt >= palier_final:
                        if passer_ordre(symbole, "BUY", montant_usdt=palier_final):
                            solde_usdt -= palier_final

                            quantite_achetee = palier_final / px_actuel
                            if PAPER_TRADING: quantite_achetee *= 0.999 # Simule les frais d'achat (0.1%)

                            positions[symbole] = {'quantite_totale': quantite_achetee, 'montant_investi': palier_final, 'prix_moyen': palier_final / quantite_achetee, 'dernier_prix_achat': px_actuel, 'prix_max': px_actuel, 'paliers': 1, 'taille_palier_fixee': palier_final, 'break_even': False, 'marge_trailing': 0.015}
                            log_trade(f"  >>> 🚀 [{heure_str}] ACHAT {'FLASH ' if flash_entry and signal_trend else ''}| {symbole} ({type_signal})")

        sauvegarder_etat(solde_usdt, positions, plus_haut_capital, capital_coffre)

    time.sleep(3)
