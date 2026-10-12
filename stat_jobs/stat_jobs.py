#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stat_jobs.py - Statistique du marché : combien d'offres IT exigent un allemand C1 ou plus ?

BUT
    Argumenter auprès du conseiller (Agentur für Arbeit) pour financer un cours d'allemand C1 :
    sur les ~90 derniers jours, combien d'offres de mon domaine (administrateur système, Linux,
    IT-Support...) demandent du C1 ou plus, autour d'Oldenburg (+60 km) et dans toute
    l'Allemagne ?

CE SCRIPT EST INDÉPENDANT DE job_watcher.py (autres fichiers, autre workflow, autres secrets).

DÉROULEMENT
    1. Recherche (API Arbeitsagentur) pour chaque mot-clé de config.json, toute l'Allemagne, 90 jours.
    2. Téléchargement du texte complet de chaque offre.
    3. Filtre de dates (1re publication dans la fenêtre) puis dédoublonnage (même entreprise + même titre + même texte = une seule offre).
    4. Classification : niveau de langue (sprache.json), zone (GPS), thèmes (themes.json).
    5. Écriture dans le Google Sheet (6 onglets) - TOUT À LA FIN, une fois les données complètes.

RÈGLES DE SÉCURITÉ (re-exécution)
    - Si une recherche échoue après 3 essais  -> le script s'arrête, le Sheet n'est PAS touché.
    - Si trop de textes sont illisibles (erreurs réseau > 5 %) -> idem.
    - Si l'écriture dans le Sheet échoue -> les résultats restent dans stat_jobs/out/ (artefact GitHub).
    - Les onglets Résumé / Offres C1 / Expressions / Tendances / Données sont RECRÉÉS à chaque
      exécution (même résultat si on relance le même jour). L'onglet Historique n'est JAMAIS
      effacé : une ligne par jour (la ligne du jour est remplacée en cas de relance).

SECRETS GITHUB UTILISÉS (jamais dans un fichier du dépôt)
    GCP_SA_KEY           contenu JSON de la clé du compte de service Google
    STAT_JOBS_SHEET_ID   identifiant du Google Sheet

UTILISATION LOCALE (test sans écrire dans le Sheet) :
    python stat_jobs.py --sans-sheet
"""

import argparse
import base64
import csv
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger("stat_jobs")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")

API_SEARCH = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
API_DETAIL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{}"
API_HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "stat_jobs/1.0"}
LIEN_OFFRE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{}"

PAGE_SIZE = 100
MAX_PAGES = 60          # garde-fou : 6000 offres par mot-clé maximum
ESSAIS = 3
TEXTE_MIN = 80          # en dessous : on considère que l'offre n'a pas de vrai texte


# --------------------------------------------------------------------------- utilitaires

class StatJobsError(Exception):
    """Erreur bloquante : on s'arrête AVANT d'écrire dans le Sheet."""


def charger_json(nom):
    chemin = os.path.join(BASE_DIR, nom)
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise StatJobsError(f"Fichier {nom} illisible ou JSON invalide : {e}")


def haversine_km(lat1, lon1, lat2, lon2):
    """Distance à vol d'oiseau en km entre deux points GPS."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def avec_essais(fonction, description, essais=ESSAIS, pauses=(2, 6, 15)):
    """Exécute fonction() jusqu'à 3 fois (erreurs réseau, 429, 5xx). Lève la dernière erreur."""
    derniere = None
    for n in range(essais):
        try:
            return fonction()
        except (requests.RequestException, ValueError) as e:
            derniere = e
            attente = pauses[min(n, len(pauses) - 1)]
            log.warning("%s : essai %d/%d échoué (%s)%s", description, n + 1, essais, e,
                        f" - nouvelle tentative dans {attente}s" if n + 1 < essais else "")
            if n + 1 < essais:
                time.sleep(attente)
    raise derniere


def nettoyer_html(texte):
    texte = re.sub(r"<[^>]+>", " ", texte or "")
    texte = (texte.replace("&nbsp;", " ").replace("&amp;", "&").replace("&auml;", "ä")
             .replace("&ouml;", "ö").replace("&uuml;", "ü").replace("&szlig;", "ß"))
    return re.sub(r"\s+", " ", texte).strip()


def norm(s):
    return re.sub(r"[^a-z0-9äöüß]+", " ", (s or "").lower()).strip()


# --------------------------------------------------------------------------- 1. recherche

def lancer_recherche(session, cfg, mot_cle):
    """Toutes les pages d'un mot-clé (toute l'Allemagne). Retourne la liste brute d'offres."""
    resultats, page = [], 1
    while page <= MAX_PAGES:
        params = {
            "was": mot_cle,
            "size": PAGE_SIZE,
            "page": page,
            "veroeffentlichtseit": min(int(cfg["jours"]), 100),
            "angebotsart": cfg.get("angebotsart", 1),
            # IMPORTANT : booléen en minuscules ("true"), sinon l'API renvoie 0 résultat
            "zeitarbeit": str(bool(cfg.get("zeitarbeit", True))).lower(),
        }
        if cfg.get("berufsfeld"):
            params["berufsfeld"] = cfg["berufsfeld"]

        def appel():
            r = session.get(API_SEARCH, params=params, headers=API_HEADERS, timeout=40)
            if r.status_code >= 400:
                raise requests.HTTPError(f"HTTP {r.status_code} {r.text[:150]}")
            return r.json()

        data = avec_essais(appel, f"Recherche '{mot_cle}' page {page}")
        liste = data.get("ergebnisliste") or data.get("stellenangebote") or []
        total = data.get("maxErgebnisse")
        resultats.extend(liste)
        log.info("Recherche '%s' page %d : %d offres (total annoncé : %s)",
                 mot_cle, page, len(liste), total)
        if len(liste) < PAGE_SIZE:
            break
        if total and len(resultats) >= int(total):
            break
        page += 1
        time.sleep(cfg.get("pause_s", 0.15))
    return resultats


def mapper_offre(o):
    """Transforme une offre brute de l'API en dictionnaire simple."""
    lokas = o.get("stellenlokationen") or [{}]
    loc0 = lokas[0] or {}
    adr = loc0.get("adresse", {}) or {}
    refnr = o.get("referenznummer") or o.get("refnr") or ""
    return {
        "refnr": refnr,
        "titel": o.get("stellenangebotsTitel") or o.get("hauptberuf", "") or "",
        "firma": o.get("firma", "") or "",
        "ort": adr.get("ort", "") or "",
        "plz": adr.get("plz", "") or "",
        "region": adr.get("region", "") or "",
        "lat": loc0.get("breite"),
        "lon": loc0.get("laenge"),
        "datum": (o.get("datumErsteVeroeffentlichung") or "")[:10],
        "link": LIEN_OFFRE.format(urllib.parse.quote(refnr)) if refnr else "",
    }


# --------------------------------------------------------------------------- 2. textes

def telecharger_texte(session, refnr, pause):
    """
    Retourne (texte, statut). statut : 'ok' | 'leer' (offre sans texte / 404, pas une panne)
    | 'fehler' (panne réseau/serveur après 3 essais - compte dans le seuil d'échec).
    """
    time.sleep(pause)
    url = API_DETAIL.format(urllib.parse.quote(base64.b64encode(refnr.encode()).decode()))

    def appel():
        r = session.get(url, headers=API_HEADERS, timeout=40)
        if r.status_code in (404, 410):
            return None
        if r.status_code >= 400:
            raise requests.HTTPError(f"HTTP {r.status_code}")
        return r.json()

    try:
        data = avec_essais(appel, f"Détail {refnr}")
    except (requests.RequestException, ValueError):
        return "", "fehler"
    if data is None:
        return "", "leer"
    brut = data.get("stellenangebotsBeschreibung") or data.get("stellenbeschreibung") or ""
    texte = nettoyer_html(brut)
    if len(texte) < TEXTE_MIN:
        return texte, "leer"
    return texte, "ok"


# --------------------------------------------------------------------------- 3. doublons

def dedoublonner(offres):
    """Même entreprise + même titre + même texte = une seule offre (ex. FERCHAU republie)."""
    vues, uniques, doublons = set(), [], 0
    for o in offres:
        if o["statut"] == "ok":
            cle = (norm(o["firma"]), norm(o["titel"]),
                   hashlib.sha1(norm(o["text"])[:3000].encode()).hexdigest())
        else:
            cle = ("ref", o["refnr"])   # sans texte, on ne peut comparer que le numéro
        if cle in vues:
            doublons += 1
            continue
        vues.add(cle)
        uniques.append(o)
    log.info("Dédoublonnage : %d doublons retirés, %d offres uniques", doublons, len(uniques))
    return uniques, doublons


# --------------------------------------------------------------------------- 4. classification

def compiler_expressions(sprache):
    cats = sprache["kategorien"]
    ex = []
    for e in sprache["ausdruecke"]:
        if e["kategorie"] not in cats:
            raise StatJobsError(f"sprache.json : catégorie inconnue '{e['kategorie']}' pour '{e['name']}'")
        try:
            ex.append({
                "name": e["name"], "kategorie": e["kategorie"],
                "muster": [re.compile(m, re.IGNORECASE) for m in e["muster"]],
                "ausschluss": [re.compile(m, re.IGNORECASE) for m in e.get("ausschluss", [])],
                "vor": int(e.get("ausschluss_vor", 40)),
                "nach": int(e.get("ausschluss_nach", 40)),
                "explizit": bool(e.get("explizit", False)),
            })
        except re.error as err:
            raise StatJobsError(f"sprache.json : regex invalide dans '{e['name']}' : {err}")
    return ex


def extrait(texte, debut, fin, marge=70):
    a, b = max(0, debut - marge), min(len(texte), fin + marge)
    s = re.sub(r"\s+", " ", texte[a:b]).strip()
    return ("…" if a > 0 else "") + s + ("…" if b < len(texte) else "")


def classer_langue(titre, texte, expressions):
    """Retourne la liste des mentions trouvées : [{name, kategorie, phrase}], une par expression."""
    corpus = f"{titre}. {texte}"
    trouvees = []
    for e in expressions:
        for motif in e["muster"]:
            hit = None
            for m in motif.finditer(corpus):
                # 'ausschluss' : ignore la mention si le contexte proche l'infirme (ex. "Führerschein C1")
                # zone 'après' coupée au prochain point de liste : on ne lit pas l'exigence suivante
                apres = re.split(r"\*|•|\s-\s", corpus[m.end():m.end() + e["nach"]], maxsplit=1)[0]
                contexte = corpus[max(0, m.start() - e["vor"]):m.start()] + corpus[m.start():m.end()] + apres
                if any(x.search(contexte) for x in e["ausschluss"]):
                    continue
                hit = m
                break
            if hit:
                trouvees.append({"name": e["name"], "kategorie": e["kategorie"], "explizit": e["explizit"],
                                 "phrase": extrait(corpus, hit.start(), hit.end())})
                break
    return trouvees


def compiler_themes(cfg_themes):
    out = []
    try:
        for t in cfg_themes["themes"]:
            out.append({
                "name": t["name"],
                "alle_von": [[re.compile(r, re.I) for r in g] for g in t.get("alle_von", [])],
                "keines_von": [re.compile(r, re.I) for r in t.get("keines_von", [])],
                "titel": [re.compile(r, re.I) for r in t.get("titel_enthaelt_eins_von", [])],
            })
    except (re.error, KeyError) as e:
        raise StatJobsError(f"themes.json invalide : {e}")
    return out


def classer_themes(titre, texte, themes):
    corpus = f"{titre} {texte}"
    res = []
    for t in themes:
        if not all(any(r.search(corpus) for r in g) for g in t["alle_von"]):
            continue
        if any(r.search(corpus) for r in t["keines_von"]):
            continue
        if t["titel"] and not any(r.search(titre) for r in t["titel"]):
            continue
        res.append(t["name"])
    return res


_FORMES_JURIDIQUES = r"\b(gmbh|ag|se|kg|kgaa|mbh|co|ohg|ug|e\.?\s?v|haftungsbeschränkt|germany|deutschland)\b"


def firme_normalisee(nom):
    """'FERCHAU GmbH Niederlassung Bremen' et 'Ferchau GmbH' -> 'ferchau' (pour compter les entreprises distinctes)."""
    n = (nom or "").lower()
    n = re.split(r"\b(niederlassung|standort|filiale|zweigniederlassung|region)\b|[,(/|]", n)[0]
    n = re.sub(_FORMES_JURIDIQUES, " ", n)
    n = re.sub(r"[^a-z0-9äöüß]+", " ", n).strip()
    return n or (nom or "").strip().lower() or "(inconnue)"


def classifier(offres, cfg, sprache, themes_cfg):
    expressions = compiler_expressions(sprache)
    themes = compiler_themes(themes_cfg)
    cats = sprache["kategorien"]
    ref = cfg["lieu_reference"]
    rayon = float(cfg["rayon_km"])
    for o in offres:
        # zone
        if o["lat"] is not None and o["lon"] is not None:
            try:
                o["dist"] = round(haversine_km(ref["latitude"], ref["longitude"],
                                               float(o["lat"]), float(o["lon"])), 1)
            except (TypeError, ValueError):
                o["dist"] = None
        else:
            o["dist"] = None
        o["zone"] = "local" if (o["dist"] is not None and o["dist"] <= rayon) else "reste"
        # langue (seulement si on a un texte ; le titre seul est trop pauvre)
        o["hits"] = classer_langue(o["titel"], o["text"], expressions) if o["statut"] == "ok" else []
        o["cats"] = sorted({h["kategorie"] for h in o["hits"]})
        o["c1"] = any(cats[c]["zaehlt_als_c1"] for c in o["cats"])
        o["explizit"] = any(h["explizit"] for h in o["hits"])
        o["firma_norm"] = firme_normalisee(o["firma"])
        o["themes"] = classer_themes(o["titel"], o["text"], themes) if o["statut"] == "ok" else []
    return expressions, themes


# --------------------------------------------------------------------------- 5. agrégats

def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def lundi(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None
    return d - timedelta(days=d.weekday())


def agreger(offres, sprache, expressions, themes):
    """Calcule tous les chiffres pour les deux périmètres : 'local' (Oldenburg+rayon) et 'de' (Allemagne)."""
    cats = sprache["kategorien"]
    perimetres = {"local": [o for o in offres if o["zone"] == "local"], "de": list(offres)}
    agg = {}
    for cle, lst in perimetres.items():
        avec = [o for o in lst if o["statut"] == "ok"]
        a = {
            "total": len(lst),
            "avec_texte": len(avec),
            "vides": sum(1 for o in lst if o["statut"] == "leer"),
            "erreurs": sum(1 for o in lst if o["statut"] == "fehler"),
            "c1": sum(1 for o in avec if o["c1"]),
            "explizit": sum(1 for o in avec if o["explizit"]),
            "par_cat": {c: sum(1 for o in avec if c in o["cats"]) for c in cats},
            # catégorie dominante (la 1re de sprache.json présente) : chaque offre compte une seule fois
            "top": {c: sum(1 for o in avec if next((x for x in cats if x in o["cats"]), None) == c)
                    for c in cats},
            "par_expr": {e["name"]: sum(1 for o in avec if any(h["name"] == e["name"] for h in o["hits"]))
                         for e in expressions},
            "par_theme": {t["name"]: sum(1 for o in avec if t["name"] in o["themes"]) for t in themes},
        }
        # semaines de publication
        sem_tot, sem_theme = {}, {t["name"]: {} for t in themes}
        for o in lst:
            w = lundi(o["datum"])
            if not w:
                continue
            sem_tot[w] = sem_tot.get(w, 0) + 1
            for tn in o["themes"]:
                sem_theme[tn][w] = sem_theme[tn].get(w, 0) + 1
        a["sem_tot"], a["sem_theme"] = sem_tot, sem_theme
        # entreprises derrière les offres "C1 ou plus"
        firmes, noms = {}, {}
        for o in avec:
            if o["c1"]:
                firmes[o["firma_norm"]] = firmes.get(o["firma_norm"], 0) + 1
                noms.setdefault(o["firma_norm"], o["firma"])
        a["firmes"] = firmes
        a["distinct"] = len(firmes)
        if firmes:
            top = max(firmes, key=firmes.get)
            a["top_nom"], a["top_n"] = noms[top], firmes[top]
            a["avec_sans_top"] = sum(1 for o in avec if o["firma_norm"] != top)
            a["c1_sans_top"] = sum(1 for o in avec if o["c1"] and o["firma_norm"] != top)
        else:
            a["top_nom"], a["top_n"], a["avec_sans_top"], a["c1_sans_top"] = "-", 0, len(avec), 0
        agg[cle] = a
    return agg


def liste_semaines(agg):
    ws = set(agg["de"]["sem_tot"].keys())
    if not ws:
        return []
    w, fin, res = min(ws), max(ws), []
    while w <= fin:
        res.append(w)
        w += timedelta(days=7)
    return res


# --------------------------------------------------------------------------- 6. tableaux pour le Sheet

def cellule(v):
    """Neutralise les textes qui seraient pris pour une formule par Google Sheets."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def construire_tableaux(offres, agg, sprache, expressions, themes, cfg, aujourdhui, doublons, hors_fenetre=0):
    cats = sprache["kategorien"]
    ref = cfg["lieu_reference"]["nom"]
    L, D = agg["local"], agg["de"]
    lib_local = f"{ref} + {int(cfg['rayon_km'])} km"
    tab = {}

    # ---- Résumé
    lib_moins = "Moins que C1 ou non précisé"
    r = [["STATISTIQUE : offres IT exigeant un allemand C1 ou plus"],
         [f"Mise à jour : {aujourdhui} | Fenêtre : {cfg['jours']} derniers jours | "
          f"Mots-clés : {', '.join(cfg['mots_cles'])}"],
         ["Note : instantané des offres encore en ligne à cette date (pas une évolution réelle). "
          "'Allemagne' inclut aussi la zone locale."],
         [],
         ["Indicateur", lib_local, "Allemagne"]]

    def ligne(lib, k):
        r.append([lib, L[k], D[k]])

    moins = {k: X["avec_texte"] - X["c1"] for k, X in (("L", L), ("D", D))}
    ligne("Offres analysées (après dédoublonnage)", "total")
    ligne("dont avec texte exploitable", "avec_texte")
    ligne("dont sans texte (offre vide / introuvable)", "vides")
    ligne("dont texte illisible (erreur technique)", "erreurs")
    r.append([])
    r.append(["C1 OU PLUS (nombre d'offres)", L["c1"], D["c1"]])
    r.append(["C1 OU PLUS (% des offres avec texte)", pct(L["c1"], L["avec_texte"]), pct(D["c1"], D["avec_texte"])])
    r.append([f"{lib_moins} (nombre d'offres)", moins["L"], moins["D"]])
    r.append([f"{lib_moins} (%)", pct(moins["L"], L["avec_texte"]), pct(moins["D"], D["avec_texte"])])
    r.append([])
    r.append(["C1 ÉCRIT EXPLICITEMENT (C1 / C2 dans le texte) - nombre", L["explizit"], D["explizit"]])
    r.append(["C1 ÉCRIT EXPLICITEMENT - %", pct(L["explizit"], L["avec_texte"]), pct(D["explizit"], D["avec_texte"])])
    r.append([])
    r.append(["Entreprises distinctes derrière les offres C1 ou plus", L["distinct"], D["distinct"]])
    r.append(["Entreprise la plus représentée", L["top_nom"], D["top_nom"]])
    r.append(["... nombre de ses offres C1 ou plus", L["top_n"], D["top_n"]])
    r.append(["... part des offres C1 ou plus (%)", pct(L["top_n"], L["c1"]), pct(D["top_n"], D["c1"])])
    r.append(["C1 OU PLUS sans cette entreprise (% des autres offres)", pct(L["c1_sans_top"], L["avec_sans_top"]),
              pct(D["c1_sans_top"], D["avec_sans_top"])])
    r.append([])
    r.append([f"Doublons retirés : {doublons}. Offres écartées car publiées pour la première fois il y a plus de "
              f"{cfg['jours']} jours : {hors_fenetre}. Les % sont calculés sur les offres avec texte exploitable. "
              "Une offre compte une seule fois dans 'C1 ou plus', même avec plusieurs mentions (voir l'onglet Expressions)."])

    # -- données des graphiques (tableau visible sous le résumé ; les graphiques le lisent)
    r.append([])
    r.append(["DONNÉES DES GRAPHIQUES (ne pas modifier)"])
    h1 = len(r)
    r.append(["Niveau d'allemand exigé (% des offres avec texte)", "C1 ou plus", lib_moins])
    for lib, X, m in ((lib_local, L, moins["L"]), ("Allemagne", D, moins["D"])):
        r.append([lib, pct(X["c1"], X["avec_texte"]), pct(m, X["avec_texte"])])
    r.append([])
    h2 = len(r)
    r.append(["Comparaison (% des offres avec texte)", "C1 écrit explicitement", "C1 ou plus",
              "C1 ou plus sans l'entreprise principale"])
    for lib, X in ((lib_local, L), ("Allemagne", D)):
        r.append([lib, pct(X["explizit"], X["avec_texte"]), pct(X["c1"], X["avec_texte"]),
                  pct(X["c1_sans_top"], X["avec_sans_top"])])
    charts_r = [
        {"type": "COLUMN", "stacked": True, "titre": "Niveau d'allemand exigé (% des offres)",
         "header_row": h1, "last_row": h1 + 2, "domain_col": 0, "series_cols": [1, 2],
         "anchor_row": 3, "anchor_col": 7, "y_title": "% des offres"},
        {"type": "COLUMN", "stacked": False, "titre": "C1 explicite / C1 ou plus / sans l'entreprise principale (%)",
         "header_row": h2, "last_row": h2 + 2, "domain_col": 0, "series_cols": [1, 2, 3],
         "anchor_row": 22, "anchor_col": 7, "y_title": "% des offres"},
    ]
    tab["Résumé"] = {"values": r, "header_rows": [0, 4, h1, h2], "charts": charts_r, "col_widths": {0: 430}}

    # ---- Offres C1 ("C1 ou plus")
    en_tete = ["Zone", "Entreprise", "Offres C1 ou plus de cette entreprise (Allemagne)", "Titre", "Lieu",
               "Date de publication", "Lien", "C1 écrit explicitement", "Expression(s) déclenchante(s)", "Phrase"]
    c1 = [o for o in offres if o["c1"]]
    # local d'abord ; dans chaque zone, les plus récentes en premier
    loc = sorted([o for o in c1 if o["zone"] == "local"], key=lambda o: o["datum"], reverse=True)
    rest = sorted([o for o in c1 if o["zone"] != "local"], key=lambda o: o["datum"], reverse=True)
    rows = [en_tete]
    for o in loc + rest:
        hits = o["hits"]
        rows.append([cellule(x) for x in [
            lib_local if o["zone"] == "local" else "Reste de l'Allemagne",
            o["firma"], D["firmes"].get(o["firma_norm"], 0), o["titel"], f"{o['plz']} {o['ort']}".strip(),
            o["datum"], o["link"], "oui" if o["explizit"] else "non",
            " / ".join(h["name"] for h in hits),
            hits[0]["phrase"] if hits else ""]])
    tab["Offres C1"] = {"values": rows, "header_rows": [0]}

    # ---- Expressions
    rows = [["Expression", f"Offres {lib_local}", f"% {lib_local}", "Offres Allemagne", "% Allemagne"]]
    for e in expressions:
        n = e["name"]
        rows.append([cellule(n), L["par_expr"][n], pct(L["par_expr"][n], L["avec_texte"]),
                     D["par_expr"][n], pct(D["par_expr"][n], D["avec_texte"])])
    rows.append([])
    rows.append(["Une offre peut apparaître sous plusieurs expressions ; les lignes ne s'additionnent donc pas. "
                 "Les % sont calculés sur les offres avec texte. Toutes ces expressions comptent comme 'C1 ou plus'."])
    tab["Expressions"] = {"values": rows, "header_rows": [0], "col_widths": {0: 240},
                          "charts": [{"type": "BAR", "stacked": False,
                                      "titre": "Offres par expression (% des offres avec texte)",
                                      "header_row": 0, "last_row": len(expressions), "domain_col": 0,
                                      "series_cols": [2, 4], "anchor_row": 12, "anchor_col": 0}]}

    # ---- Tendances
    semaines = liste_semaines(agg)
    t = [["TENDANCES PAR THÈME (indépendantes du niveau de langue ; thèmes modifiables dans themes.json)"],
         ["Instantané des offres encore en ligne, classées par semaine de première publication."],
         [],
         ["Thème", f"Offres {lib_local}", f"% {lib_local}", "Offres Allemagne", "% Allemagne"],
         ["Toutes les offres", L["avec_texte"], 100.0 if L["avec_texte"] else 0.0,
          D["avec_texte"], 100.0 if D["avec_texte"] else 0.0]]
    for th in themes:
        n = th["name"]
        t.append([n, L["par_theme"][n], pct(L["par_theme"][n], L["avec_texte"]),
                  D["par_theme"][n], pct(D["par_theme"][n], D["avec_texte"])])
    t.append([])
    blocs = {}
    ncols = 2 + len(themes)
    for cle, lib in (("de", "ALLEMAGNE"), ("local", lib_local.upper())):
        a = agg[cle]
        t.append([f"Offres par semaine de publication - {lib}"])
        debut = len(t)
        t.append(["Semaine (lundi)", "Toutes les offres"] + [th["name"] for th in themes])
        for w in semaines:
            t.append([w.isoformat(), a["sem_tot"].get(w, 0)] +
                     [a["sem_theme"][th["name"]].get(w, 0) for th in themes])
        blocs[cle] = {"header_row": debut, "first": debut + 1, "last": debut + len(semaines),
                      "titre": f"Offres par semaine - {lib}"}
        t.append([])
    charts_t = []
    for bloc in blocs.values():
        if bloc["last"] >= bloc["first"]:
            charts_t.append({"type": "LINE", "stacked": False, "titre": bloc["titre"],
                             "header_row": bloc["header_row"], "last_row": bloc["last"], "domain_col": 0,
                             "series_cols": list(range(1, ncols)), "anchor_row": bloc["header_row"],
                             "anchor_col": ncols + 1, "x_title": "Semaine de publication",
                             "y_title": "Nombre d'offres"})
    tab["Tendances"] = {"values": t, "header_rows": [0, 3] + [b["header_row"] for b in blocs.values()],
                        "charts": charts_t, "col_widths": {0: 260}, "extra_cols": ncols + 16}

    # ---- Données
    rows = [["Référence", "Zone", "Distance (km)", "Entreprise", "Titre", "Lieu", "Date", "Lien",
             "Statut du texte", "C1 ou plus", "Expressions", "Thèmes"]]
    for o in sorted(offres, key=lambda x: x["datum"], reverse=True):
        rows.append([cellule(x) for x in [
            o["refnr"], "local" if o["zone"] == "local" else "reste", o["dist"] if o["dist"] is not None else "",
            o["firma"], o["titel"], f"{o['plz']} {o['ort']}".strip(), o["datum"], o["link"],
            {"ok": "ok", "leer": "vide", "fehler": "erreur"}[o["statut"]],
            "oui" if o["c1"] else "non",
            " / ".join(h["name"] for h in o["hits"]), " / ".join(o["themes"])]])
    tab["Données"] = {"values": rows, "header_rows": [0]}

    # ---- ligne d'historique
    ent = ["Date", "Offres Allemagne", f"Offres {lib_local}", "C1 ou plus Allemagne", "% C1 ou plus Allemagne",
           f"C1 ou plus {lib_local}", f"% C1 ou plus {lib_local}", "C1 explicite Allemagne",
           "Entreprises distinctes C1 Allemagne", "Sans texte / illisible"]
    ent += [f"{th['name']} (DE)" for th in themes] + [f"{th['name']} (local)" for th in themes]
    ligne_h = [aujourdhui, D["total"], L["total"], D["c1"], pct(D["c1"], D["avec_texte"]),
               L["c1"], pct(L["c1"], L["avec_texte"]), D["explizit"], D["distinct"], D["vides"] + D["erreurs"]]
    ligne_h += [D["par_theme"][th["name"]] for th in themes] + [L["par_theme"][th["name"]] for th in themes]
    tab["_historique"] = {"header": ent, "row": ligne_h}
    return tab


# --------------------------------------------------------------------------- 7. sauvegarde locale

def sauvegarder_local(offres, tab, agg):
    """Copie locale (artefact GitHub) : sert de secours si l'écriture dans le Sheet échoue."""
    os.makedirs(OUT_DIR, exist_ok=True)
    legers = [{k: v for k, v in o.items() if k != "text"} for o in offres]
    with open(os.path.join(OUT_DIR, "resultat.json"), "w", encoding="utf-8") as f:
        json.dump({"offres": legers, "tableaux": {k: v.get("values", v) for k, v in tab.items()
                                                  if k != "_historique"}},
                  f, ensure_ascii=False, indent=1, default=str)
    for nom, contenu in tab.items():
        if nom.startswith("_"):
            continue
        with open(os.path.join(OUT_DIR, f"{nom}.csv"), "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(contenu["values"])
    log.info("Copie locale écrite dans %s", OUT_DIR)


# --------------------------------------------------------------------------- 8. Google Sheets

def ouvrir_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    cle = os.environ.get("GCP_SA_KEY", "").strip()
    sheet_id = os.environ.get("STAT_JOBS_SHEET_ID", "").strip()
    if not cle or not sheet_id:
        raise StatJobsError("Secrets GCP_SA_KEY et/ou STAT_JOBS_SHEET_ID absents de l'environnement.")
    try:
        info = json.loads(cle)
    except ValueError:
        raise StatJobsError("GCP_SA_KEY n'est pas un JSON valide (colle le fichier .json COMPLET dans le secret).")
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets",
                      "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)


def _onglet(sh, nom, lignes=100, cols=26):
    import gspread
    try:
        return sh.worksheet(nom)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=nom, rows=lignes, cols=cols)


def _ecrire_onglet(sh, nom, valeurs, lignes_entete, cols_min=26):
    ws = _onglet(sh, nom)
    ws.clear()
    largeur = max([len(r) for r in valeurs] + [1])
    ws.resize(rows=max(len(valeurs) + 5, 10), cols=max(largeur, cols_min))
    ws.update(values=valeurs, range_name="A1", value_input_option="USER_ENTERED")
    try:  # mise en forme : non critique
        for i in lignes_entete:
            ws.format(f"{i + 1}:{i + 1}", {"textFormat": {"bold": True}})
        if lignes_entete and lignes_entete[-1] == 0 or lignes_entete == [0]:
            ws.freeze(rows=1)
    except Exception as e:  # noqa: BLE001
        log.warning("Mise en forme de '%s' ignorée : %s", nom, e)
    return ws


def _ecrire_historique(sh, hist):
    ws = _onglet(sh, "Historique", 200, max(30, len(hist["header"]) + 2))
    existant = ws.get_all_values()
    ent, ligne = hist["header"], hist["row"]
    if not existant:
        ws.update(values=[ent, ligne], range_name="A1", value_input_option="USER_ENTERED")
        try:
            ws.format("1:1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
        except Exception:  # noqa: BLE001
            pass
        return
    # dernière ligne d'en-tête connue ; si elle diffère (thèmes modifiés), on ajoute un nouvel en-tête
    dernier_ent = next((r for r in reversed(existant) if r and r[0] == "Date"), None)
    nouveaux = []
    if dernier_ent is None or [c for c in dernier_ent if c != ""] != ent:
        nouveaux.append(ent)
    # même jour : on remplace la ligne du jour (relance)
    idx_jour = None
    for i, r in enumerate(existant):
        if r and r[0] == ligne[0]:
            idx_jour = i + 1
    if idx_jour and not nouveaux:
        ws.update(values=[ligne], range_name=f"A{idx_jour}", value_input_option="USER_ENTERED")
    else:
        if idx_jour:      # en-têtes changés le même jour : on supprime l'ancienne ligne du jour
            ws.delete_rows(idx_jour)
        ws.append_rows(nouveaux + [ligne], value_input_option="USER_ENTERED")


def _requete_graphique(sid, d):
    """Requête Sheets 'addChart' à partir d'un descripteur de graphique (voir construire_tableaux)."""
    def plage(col):
        return {"sourceRange": {"sources": [{"sheetId": sid, "startRowIndex": d["header_row"],
                                             "endRowIndex": d["last_row"] + 1,
                                             "startColumnIndex": col, "endColumnIndex": col + 1}]}}
    horizontal = d["type"] == "BAR"
    axe_valeurs = "BOTTOM_AXIS" if horizontal else "LEFT_AXIS"
    basic = {"chartType": d["type"], "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
             "domains": [{"domain": plage(d["domain_col"])}],
             "series": [{"series": plage(c), "targetAxis": axe_valeurs} for c in d["series_cols"]]}
    if d.get("stacked"):
        basic["stackedType"] = "STACKED"
    axes = []
    if d.get("x_title") and not horizontal:
        axes.append({"position": "BOTTOM_AXIS", "title": d["x_title"]})
    if d.get("y_title") and not horizontal:
        axes.append({"position": "LEFT_AXIS", "title": d["y_title"]})
    if axes:
        basic["axis"] = axes
    return {"addChart": {"chart": {
        "spec": {"title": d["titre"], "basicChart": basic},
        "position": {"overlayPosition": {"anchorCell": {"sheetId": sid, "rowIndex": d["anchor_row"],
                                                        "columnIndex": d["anchor_col"]},
                                         "widthPixels": d.get("largeur", 640),
                                         "heightPixels": d.get("hauteur", 340)}}}}}


def ecrire_sheet(tab):
    sh = ouvrir_sheet()
    log.info("Sheet ouvert : %s", sh.title)
    ordre = ["Résumé", "Offres C1", "Expressions", "Tendances", "Données"]
    feuilles = {}
    for nom in ordre:
        info = tab[nom]
        feuilles[nom] = _ecrire_onglet(sh, nom, info["values"], info["header_rows"],
                                       info.get("extra_cols", 26))
        log.info("Onglet '%s' écrit (%d lignes)", nom, len(info["values"]))

    # Graphiques et largeurs de colonnes : on supprime les anciens graphiques puis on recrée tout.
    # Non critique : si cette étape échoue, les tableaux sont déjà écrits.
    try:
        ids = {ws.id: nom for nom, ws in feuilles.items()}
        reqs = []
        for s in sh.fetch_sheet_metadata().get("sheets", []):
            if s["properties"]["sheetId"] in ids:
                reqs += [{"deleteEmbeddedObject": {"objectId": c["chartId"]}} for c in s.get("charts", [])]
        for nom, ws in feuilles.items():
            for col, px in tab[nom].get("col_widths", {}).items():
                reqs.append({"updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                    "properties": {"pixelSize": px}, "fields": "pixelSize"}})
            for d in tab[nom].get("charts", []):
                reqs.append(_requete_graphique(ws.id, d))
        if reqs:
            sh.batch_update({"requests": reqs})
            log.info("Graphiques créés : %d", sum(len(tab[n].get("charts", [])) for n in feuilles))
    except Exception as e:  # noqa: BLE001
        log.warning("Graphiques non créés (tableaux écrits quand même) : %s", e)

    _ecrire_historique(sh, tab["_historique"])
    log.info("Onglet 'Historique' mis à jour")
    # ordre des onglets + suppression de l'onglet vide par défaut
    try:
        noms = ordre + ["Historique"]
        sh.reorder_worksheets([sh.worksheet(n) for n in noms] +
                              [w for w in sh.worksheets() if w.title not in noms])
        for w in sh.worksheets():
            if w.title in ("Sheet1", "Feuille 1", "Feuille1", "Tabelle1") and not any(w.get_all_values()):
                sh.del_worksheet(w)
    except Exception as e:  # noqa: BLE001
        log.warning("Réordonnancement des onglets ignoré : %s", e)


# --------------------------------------------------------------------------- principal

def collecter(cfg, session=None):
    """Étapes 1 à 3 : recherche, filtre de dates, textes, dédoublonnage. Retourne (offres, doublons, hors_fenetre)."""
    session = session or requests.Session()
    brut = {}
    for mot in cfg["mots_cles"]:
        try:
            liste = lancer_recherche(session, cfg, mot)
        except (requests.RequestException, ValueError) as e:
            raise StatJobsError(f"Recherche '{mot}' échouée après {ESSAIS} essais ({e}). Sheet non modifié.")
        nouveaux = 0
        for o in liste:
            m = mapper_offre(o)
            if m["refnr"] and m["refnr"] not in brut:
                brut[m["refnr"]] = m
                nouveaux += 1
        log.info("Mot-clé '%s' : %d offres, dont %d nouvelles (total unique : %d)",
                 mot, len(liste), nouveaux, len(brut))
    if not brut:
        raise StatJobsError("Aucune offre trouvée : recherche suspecte (API ou paramètres). Sheet non modifié.")

    # L'API filtre sur la DERNIÈRE publication : une annonce republiée récemment peut avoir une date de
    # PREMIÈRE publication très ancienne. On écarte ici celles qui dépassent la fenêtre demandée.
    limite = (date.today() - timedelta(days=int(cfg["jours"]))).isoformat()
    offres = [o for o in brut.values() if not o["datum"] or o["datum"] >= limite]
    hors_fenetre = len(brut) - len(offres)
    log.info("Fenêtre %d jours (depuis le %s) : %d offres écartées car publiées pour la 1re fois plus tôt",
             int(cfg["jours"]), limite, hors_fenetre)
    if not offres:
        raise StatJobsError("Aucune offre dans la fenêtre de dates. Sheet non modifié.")
    log.info("Téléchargement de %d textes d'offres...", len(offres))
    pause = cfg.get("pause_s", 0.15)

    def tache(o):
        o["text"], o["statut"] = telecharger_texte(session, o["refnr"], pause)
        return o

    with ThreadPoolExecutor(max_workers=int(cfg.get("parallele_anfragen", 3))) as pool:
        for i, _ in enumerate(pool.map(tache, offres), 1):
            if i % 200 == 0:
                log.info("  textes : %d/%d", i, len(offres))

    erreurs = sum(1 for o in offres if o["statut"] == "fehler")
    vides = sum(1 for o in offres if o["statut"] == "leer")
    log.info("Textes : %d ok, %d vides/introuvables, %d erreurs techniques",
             len(offres) - erreurs - vides, vides, erreurs)
    if erreurs / len(offres) > float(cfg.get("max_ohne_text_anteil", 0.05)):
        raise StatJobsError(
            f"{erreurs}/{len(offres)} textes illisibles (erreurs techniques) : au-delà du seuil de "
            f"{cfg.get('max_ohne_text_anteil', 0.05):.0%}. Relancer plus tard. Sheet non modifié.")
    uniques, doublons = dedoublonner(offres)
    return uniques, doublons, hors_fenetre


def main(argv=None):
    ap = argparse.ArgumentParser(description="Statistique des offres IT exigeant un allemand C1.")
    ap.add_argument("--sans-sheet", action="store_true", help="ne pas écrire dans Google Sheets (test)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    cfg = charger_json("config.json")
    sprache = charger_json(cfg["sprache_datei"])
    themes_cfg = charger_json(cfg["themes_datei"])
    compiler_expressions(sprache)   # valide les regex AVANT de lancer 10 minutes de téléchargement
    compiler_themes(themes_cfg)

    offres, doublons, hors_fenetre = collecter(cfg)
    expressions, themes = classifier(offres, cfg, sprache, themes_cfg)
    sans_gps = sum(1 for o in offres if o["dist"] is None)
    if sans_gps:
        log.info("%d offres sans coordonnées GPS : comptées dans 'Allemagne' uniquement", sans_gps)
    agg = agreger(offres, sprache, expressions, themes)
    tab = construire_tableaux(offres, agg, sprache, expressions, themes, cfg,
                              date.today().isoformat(), doublons, hors_fenetre)
    sauvegarder_local(offres, tab, agg)

    L, D = agg["local"], agg["de"]
    log.info("RÉSULTAT Allemagne : %d offres, %d C1 ou plus (%.1f %%), %d C1 explicite, %d entreprises distinctes "
             "(principale : %s, %d offres)", D["total"], D["c1"], pct(D["c1"], D["avec_texte"]), D["explizit"],
             D["distinct"], D["top_nom"], D["top_n"])
    log.info("RÉSULTAT %s +%s km : %d offres, %d C1 ou plus (%.1f %%)",
             cfg["lieu_reference"]["nom"], cfg["rayon_km"], L["total"], L["c1"], pct(L["c1"], L["avec_texte"]))

    if args.sans_sheet:
        log.info("--sans-sheet : écriture Google Sheets ignorée")
        return 0
    try:
        ecrire_sheet(tab)
    except StatJobsError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StatJobsError(f"Écriture Google Sheets échouée ({type(e).__name__}: {e}). "
                            "Les résultats sont dans l'artefact 'stat-jobs-out' du run GitHub.")
    log.info("TERMINÉ : Google Sheet mis à jour")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StatJobsError as err:
        log.error("ARRÊT : %s", err)
        sys.exit(1)
