#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
taux_change.py - Alerte e-mail quand le cours EUR -> TND franchit un seuil.

SOURCES (gratuites, sans clé, JSON, mises à jour UNE FOIS PAR JOUR vers minuit UTC)
    1. https://open.er-api.com/v6/latest/EUR                          (ExchangeRate-API, accès libre)
    2. https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/... (secours si la 1re ne répond pas)
    Le dinar est fixé chaque jour sur le marché interbancaire (Banque centrale de Tunisie) : un cours par jour
    suffit. C'est un cours MOYEN de marché : le taux réel d'un transfert (banque, Wise, Western Union...) est
    différent et inclut des frais.

FONCTIONNEMENT (une exécution par jour, déclenchée par crontab.org via GitHub Actions)
    - lit le cours, le compare aux seuils de config.json ;
    - envoie UN e-mail au moment du franchissement, puis plus rien tant que le cours reste au-delà du seuil ;
      le seuil est "réarmé" quand le cours revient de l'autre côté (avec une petite marge d'hystérésis) ;
    - option : rapport hebdomadaire même sans franchissement ;
    - mémorise l'état et un historique dans etat.json (commité par le workflow).

UTILISATION
    python taux_change.py              # normal
    python taux_change.py --dry-run    # affiche l'e-mail au lieu de l'envoyer, ne modifie pas etat.json
"""

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import date, datetime, timezone
from email.mime.text import MIMEText

import requests

log = logging.getLogger("taux_change")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 30
LIEN_VERIF = "https://www.xe.com/fr/currencyconverter/convert/?Amount=1&From={src}&To={dst}"


class ErreurTaux(Exception):
    pass


# ----------------------------------------------------------------------------- lecture du cours

def _get_json(session, url):
    """GET avec 3 essais (pauses 2 s, 6 s)."""
    derniere = None
    for n, pause in enumerate((2, 6, 0), 1):
        try:
            r = session.get(url, timeout=TIMEOUT, headers={"User-Agent": "taux-change/1.0"})
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            derniere = e
            log.warning("%s : essai %d/3 échoué (%s)", url, n, e)
            if pause:
                time.sleep(pause)
    raise ErreurTaux(str(derniere))


def source_er_api(session, src, dst):
    data = _get_json(session, f"https://open.er-api.com/v6/latest/{src}")
    if data.get("result") != "success" or dst not in (data.get("rates") or {}):
        raise ErreurTaux(f"réponse inattendue : {str(data)[:200]}")
    return float(data["rates"][dst]), data.get("time_last_update_utc", "?"), "open.er-api.com"


def source_jsdelivr(session, src, dst):
    s, d = src.lower(), dst.lower()
    data = _get_json(session, f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{s}.json")
    try:
        return float(data[s][d]), data.get("date", "?"), "currency-api (jsDelivr)"
    except (KeyError, TypeError, ValueError):
        raise ErreurTaux(f"réponse inattendue : {str(data)[:200]}")


def lire_cours(session, src, dst):
    """Essaie les sources dans l'ordre ; lève ErreurTaux si aucune ne répond."""
    erreurs = []
    for source in (source_er_api, source_jsdelivr):
        try:
            taux, maj, nom = source(session, src, dst)
            if not 0 < taux < 1000:
                raise ErreurTaux(f"valeur aberrante : {taux}")
            log.info("Cours %s -> %s : %.5f (source : %s, mise à jour : %s)", src, dst, taux, nom, maj)
            return taux, maj, nom
        except ErreurTaux as e:
            log.warning("Source %s indisponible : %s", source.__name__, e)
            erreurs.append(f"{source.__name__}: {e}")
    raise ErreurTaux("aucune source disponible : " + " | ".join(erreurs))


# ----------------------------------------------------------------------------- état

def charger_etat(chemin):
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            log.warning("etat.json illisible : on repart de zéro")
    return {"alerte_haut_envoyee": False, "alerte_bas_envoyee": False, "dernier_rapport": None, "historique": []}


def enregistrer_etat(chemin, etat):
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


# ----------------------------------------------------------------------------- décision

def decider(cfg, etat, taux, aujourdhui):
    """
    Retourne la liste des motifs d'e-mail ("haut", "bas", "rapport") et met à jour les drapeaux de l'état.
    Une alerte n'est envoyée qu'au FRANCHISSEMENT ; elle est réarmée quand le cours revient de l'autre côté
    du seuil d'au moins 'hysteresis'.
    """
    motifs = []
    h = float(cfg.get("hysteresis", 0) or 0)
    haut, bas = cfg.get("seuil_haut"), cfg.get("seuil_bas")

    if haut is not None:
        if taux >= haut and not etat.get("alerte_haut_envoyee"):
            motifs.append("haut")
            etat["alerte_haut_envoyee"] = True
        elif taux < haut - h and etat.get("alerte_haut_envoyee"):
            etat["alerte_haut_envoyee"] = False
            log.info("Seuil haut réarmé (cours %.5f < %.5f)", taux, haut - h)

    if bas is not None:
        if taux <= bas and not etat.get("alerte_bas_envoyee"):
            motifs.append("bas")
            etat["alerte_bas_envoyee"] = True
        elif taux > bas + h and etat.get("alerte_bas_envoyee"):
            etat["alerte_bas_envoyee"] = False
            log.info("Seuil bas réarmé (cours %.5f > %.5f)", taux, bas + h)

    if cfg.get("rapport_hebdo") and aujourdhui.weekday() == int(cfg.get("jour_rapport", 0)) \
            and etat.get("dernier_rapport") != aujourdhui.isoformat() and not motifs:
        motifs.append("rapport")
        etat["dernier_rapport"] = aujourdhui.isoformat()
    return motifs


def ajouter_historique(cfg, etat, taux, aujourdhui):
    """Une valeur par jour (la dernière lecture du jour remplace la précédente)."""
    hist = [x for x in etat.get("historique", []) if x.get("date") != aujourdhui.isoformat()]
    hist.append({"date": aujourdhui.isoformat(), "taux": round(taux, 6)})
    etat["historique"] = hist[-int(cfg.get("historique_jours", 90)):]


# ----------------------------------------------------------------------------- e-mail

def construire_email(cfg, etat, motifs, taux, maj, source):
    src, dst = cfg["devise_source"], cfg["devise_cible"]
    hist = etat.get("historique", [])
    titres = {"haut": f"⬆ 1 {src} = {taux:.4f} {dst} : seuil haut {cfg.get('seuil_haut')} atteint",
              "bas": f"⬇ 1 {src} = {taux:.4f} {dst} : seuil bas {cfg.get('seuil_bas')} atteint",
              "rapport": f"Rapport hebdomadaire : 1 {src} = {taux:.4f} {dst}"}
    sujet = "[Taux de change] " + " | ".join(titres[m] for m in motifs)

    lignes = [f"1 {src} = {taux:.5f} {dst}", ""]
    if len(hist) >= 2:
        prec = hist[-2]
        diff = taux - prec["taux"]
        lignes.append(f"Variation depuis le {prec['date']} : {diff:+.5f} {dst} ({diff / prec['taux'] * 100:+.2f} %)")
    derniers = [x["taux"] for x in hist[-30:]]
    if len(derniers) >= 2:
        lignes.append(f"Sur les {len(derniers)} derniers jours relevés : min {min(derniers):.4f} / max {max(derniers):.4f}")
    lignes += ["",
               f"Seuil haut : {cfg.get('seuil_haut') if cfg.get('seuil_haut') is not None else 'désactivé'}",
               f"Seuil bas  : {cfg.get('seuil_bas') if cfg.get('seuil_bas') is not None else 'désactivé'}",
               "",
               f"Source : {source} (cours moyen de marché, mis à jour le {maj})",
               "Attention : le taux réel d'un transfert (banque, Wise, Western Union...) est différent et inclut des frais.",
               f"Vérification : {LIEN_VERIF.format(src=src, dst=dst)}",
               "",
               "Prochaine alerte : seulement si le cours repasse de l'autre côté du seuil puis le franchit à nouveau."]
    return sujet, "\n".join(lignes)


def envoyer_email(sujet, corps):
    adresse = os.environ.get("GMAIL_ADDRESS")
    mdp = os.environ.get("GMAIL_APP_PASSWORD")
    dest = [a.strip() for a in (os.environ.get("GMAIL_TO") or "").split(",") if a.strip()]
    if not (adresse and mdp and dest):
        raise ErreurTaux("secrets GMAIL_ADDRESS / GMAIL_APP_PASSWORD / GMAIL_TO manquants")
    msg = MIMEText(corps, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = sujet, adresse, ", ".join(dest)
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=TIMEOUT) as s:
        s.starttls()
        s.login(adresse, mdp)
        s.sendmail(adresse, dest, msg.as_string())
    log.info("E-mail envoyé à %s : %s", ", ".join(dest), sujet)


# ----------------------------------------------------------------------------- principal

def main(argv=None, session=None, aujourdhui=None):
    ap = argparse.ArgumentParser(description="Alerte e-mail sur le cours EUR -> TND.")
    ap.add_argument("--dry-run", action="store_true", help="affiche l'e-mail au lieu de l'envoyer ; etat.json inchangé")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    chemin_etat = os.path.join(BASE_DIR, cfg.get("fichier_etat", "etat.json"))
    aujourdhui = aujourdhui or datetime.now(timezone.utc).date()

    taux, maj, source = lire_cours(session or requests.Session(), cfg["devise_source"], cfg["devise_cible"])
    etat = charger_etat(chemin_etat)
    ajouter_historique(cfg, etat, taux, aujourdhui)
    motifs = decider(cfg, etat, taux, aujourdhui)
    etat["derniere_lecture"] = {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "taux": taux, "source": source, "maj_source": maj}

    if motifs:
        sujet, corps = construire_email(cfg, etat, motifs, taux, maj, source)
        if args.dry_run:
            print(f"\n--- APERÇU (dry-run) ---\nObjet : {sujet}\n\n{corps}\n")
        else:
            envoyer_email(sujet, corps)   # en cas d'échec : exception -> état NON enregistré -> nouvel essai demain
    else:
        log.info("Pas d'alerte (seuil haut : %s, seuil bas : %s)", cfg.get("seuil_haut"), cfg.get("seuil_bas"))

    if not args.dry_run:
        enregistrer_etat(chemin_etat, etat)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ErreurTaux, smtplib.SMTPException, OSError) as err:
        log.error("ARRÊT : %s", err)
        sys.exit(1)
