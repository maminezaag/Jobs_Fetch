#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stat_jobs.py - Statistiques sur les offres IT de mon domaine (Jobbörse de l'Agentur für Arbeit)

DEUX QUESTIONS
    1. Niveau d'allemand : combien d'offres exigent C1 ou plus ? (Oldenburg + rayon, et toute l'Allemagne)
    2. Expertise technique : quelles technologies sont demandées, et comment ça évolue (Kubernetes, Ansible,
       Azure, VMware, Proxmox...) ?

POURQUOI UNE ARCHIVE
    L'API ne renvoie que les offres ENCORE EN LIGNE : une offre retirée disparaît pour toujours.
    Le script tourne donc chaque nuit et garde chaque offre vue dans stat_jobs/archive/ (fichiers JSONL,
    un par mois, texte complet compris). Une offre retirée de la Jobbörse reste dans l'archive ET dans les
    statistiques. Comme le texte est gardé, un terme ajouté plus tard dans themes.json s'applique aussi aux
    anciennes offres (tout est reclassé à chaque exécution).

DÉROULEMENT D'UNE EXÉCUTION
    1. Recherche API (mots-clés de config.json, toute l'Allemagne).
    2. Offres nouvelles -> téléchargement du texte -> ajout à l'archive.
       Offres déjà connues -> seule la date de "dernière vue" est mise à jour (pas de re-téléchargement).
    3. Statistiques calculées sur l'archive, période glissante de 'periode_jours' (90 par défaut),
       doublons retirés en arrière-plan (même annonce publiée par plusieurs agences d'une entreprise).
    4. Google Sheet ENTIÈREMENT vidé (tous les onglets, tous les graphiques) puis reconstruit :
       Résumé / Offres C1 / Tendances.

SÉCURITÉ
    - Recherche en échec après 3 essais -> arrêt, ni l'archive ni le Sheet ne sont modifiés.
    - Texte d'une nouvelle offre illisible (panne) -> l'offre n'est pas archivée, elle sera retentée la nuit suivante.
    - L'archive est écrite AVANT le Sheet : si le Sheet échoue, les nouvelles offres sont quand même gardées
      (le workflow les commite dans le dépôt) et le Sheet sera reconstruit à la prochaine exécution.

SECRETS GITHUB : GCP_SA_KEY (clé JSON du compte de service Google), STAT_JOBS_SHEET_ID (identifiant du Sheet)

UTILISATION LOCALE (test, sans écrire dans le Sheet) :  python stat_jobs.py --sans-sheet
"""

import argparse
import base64
import csv
import glob
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
API_HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "stat_jobs/2.0"}
LIEN_OFFRE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{}"

PAGE_SIZE = 100
MAX_PAGES = 60          # garde-fou : 6000 offres par mot-clé maximum
ESSAIS = 3
TEXTE_MIN = 80          # en dessous : on considère que l'offre n'a pas de vrai texte
GRIS_CLAIR = {"red": 0.85, "green": 0.85, "blue": 0.85}


# =========================================================================== utilitaires

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


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def lundi(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return d - timedelta(days=d.weekday())


def cellule(v):
    """Neutralise les textes qui seraient pris pour une formule par Google Sheets."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def ecrire_atomique(chemin, contenu):
    """Écrit dans un fichier temporaire puis renomme : jamais de fichier à moitié écrit."""
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(contenu)
    os.replace(tmp, chemin)


# =========================================================================== 1. API

def lancer_recherche(session, cfg, mot_cle):
    """Toutes les pages d'un mot-clé (toute l'Allemagne). Retourne la liste brute d'offres."""
    resultats, page = [], 1
    while page <= MAX_PAGES:
        params = {
            "was": mot_cle,
            "size": PAGE_SIZE,
            "page": page,
            "veroeffentlichtseit": min(int(cfg.get("recherche_jours", 100)), 100),
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
        log.info("Recherche '%s' page %d : %d offres (total annoncé : %s)", mot_cle, page, len(liste), total)
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
    return {
        "refnr": o.get("referenznummer") or o.get("refnr") or "",
        "titel": o.get("stellenangebotsTitel") or o.get("hauptberuf", "") or "",
        "firma": o.get("firma", "") or "",
        "ort": adr.get("ort", "") or "",
        "plz": adr.get("plz", "") or "",
        "region": adr.get("region", "") or "",
        "lat": loc0.get("breite"),
        "lon": loc0.get("laenge"),
        "datum": (o.get("datumErsteVeroeffentlichung") or "")[:10],
    }


def telecharger_texte(session, refnr, pause):
    """
    Retourne (texte, statut). statut : 'ok' | 'leer' (offre sans texte / 404 : définitif)
    | 'fehler' (panne réseau/serveur après 3 essais : on retentera la nuit suivante).
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
    texte = nettoyer_html(data.get("stellenangebotsBeschreibung") or data.get("stellenbeschreibung") or "")
    return (texte, "leer") if len(texte) < TEXTE_MIN else (texte, "ok")


# =========================================================================== 2. archive

def dossier_archive(cfg):
    return os.path.join(BASE_DIR, cfg.get("archive_dossier", "archive"))


def charger_archive(cfg):
    """
    Lit toutes les offres archivées : archive/AAAA-MM.jsonl (une offre par ligne)
    + archive/derniere_vue.json ({référence: date de dernière vue}).
    """
    dossier = dossier_archive(cfg)
    archive = {}
    for chemin in sorted(glob.glob(os.path.join(dossier, "*.jsonl"))):
        with open(chemin, encoding="utf-8") as f:
            for n, ligne in enumerate(f, 1):
                ligne = ligne.strip()
                if not ligne:
                    continue
                try:
                    o = json.loads(ligne)
                except ValueError:
                    raise StatJobsError(f"Archive corrompue : {os.path.basename(chemin)} ligne {n}")
                archive[o["refnr"]] = o
    vues = {}
    chemin_vues = os.path.join(dossier, "derniere_vue.json")
    if os.path.exists(chemin_vues):
        with open(chemin_vues, encoding="utf-8") as f:
            vues = json.load(f)
    for ref, d in vues.items():
        if ref in archive:
            archive[ref]["derniere_vue"] = d
    log.info("Archive : %d offres déjà connues", len(archive))
    return archive


def enregistrer_archive(cfg, nouvelles, archive, aujourdhui):
    """Ajoute les nouvelles offres au fichier du mois et met à jour derniere_vue.json."""
    dossier = dossier_archive(cfg)
    os.makedirs(dossier, exist_ok=True)
    if nouvelles:
        chemin = os.path.join(dossier, f"{aujourdhui[:7]}.jsonl")
        existant = ""
        if os.path.exists(chemin):
            with open(chemin, encoding="utf-8") as f:
                existant = f.read()
            if existant and not existant.endswith("\n"):
                existant += "\n"
        champs = ("refnr", "titel", "firma", "ort", "plz", "region", "lat", "lon", "datum",
                  "premiere_vue", "statut", "text")
        lignes = "".join(json.dumps({k: o.get(k) for k in champs}, ensure_ascii=False) + "\n" for o in nouvelles)
        ecrire_atomique(chemin, existant + lignes)
    vues = {ref: o.get("derniere_vue") or o.get("premiere_vue") for ref, o in sorted(archive.items())}
    ecrire_atomique(os.path.join(dossier, "derniere_vue.json"), json.dumps(vues, ensure_ascii=False, indent=0))
    log.info("Archive enregistrée : %d nouvelles offres, %d au total", len(nouvelles), len(archive))


def mettre_a_jour_archive(cfg, session, aujourdhui):
    """Étapes 1-2 : recherche, téléchargement des seules nouvelles offres, archivage. Retourne l'archive."""
    archive = charger_archive(cfg)

    en_ligne = {}
    for mot in cfg["mots_cles"]:
        try:
            liste = lancer_recherche(session, cfg, mot)
        except (requests.RequestException, ValueError) as e:
            raise StatJobsError(f"Recherche '{mot}' échouée après {ESSAIS} essais ({e}). Rien n'est modifié.")
        for o in liste:
            m = mapper_offre(o)
            if m["refnr"]:
                en_ligne.setdefault(m["refnr"], m)
        log.info("Mot-clé '%s' : %d offres (total unique en ligne : %d)", mot, len(liste), len(en_ligne))
    if not en_ligne:
        raise StatJobsError("Aucune offre trouvée : recherche suspecte (API ou paramètres). Rien n'est modifié.")

    for ref in en_ligne:
        if ref in archive:
            archive[ref]["derniere_vue"] = aujourdhui
    a_telecharger = [m for ref, m in en_ligne.items() if ref not in archive]
    log.info("Offres en ligne : %d, dont %d nouvelles à télécharger", len(en_ligne), len(a_telecharger))

    pause = cfg.get("pause_s", 0.15)

    def tache(m):
        m["text"], m["statut"] = telecharger_texte(session, m["refnr"], pause)
        return m

    nouvelles = []
    with ThreadPoolExecutor(max_workers=int(cfg.get("parallele_anfragen", 3))) as pool:
        for i, m in enumerate(pool.map(tache, a_telecharger), 1):
            if i % 200 == 0:
                log.info("  textes : %d/%d", i, len(a_telecharger))
            if m["statut"] == "fehler":
                continue                        # pas archivée : retentée la nuit suivante
            m["premiere_vue"] = m["derniere_vue"] = aujourdhui
            archive[m["refnr"]] = m
            nouvelles.append(m)

    erreurs = len(a_telecharger) - len(nouvelles)
    if erreurs:
        log.warning("%d textes illisibles (panne) : ces offres seront retentées à la prochaine exécution", erreurs)
    if a_telecharger and erreurs > 10 and erreurs / len(a_telecharger) > 0.5:
        raise StatJobsError(f"{erreurs}/{len(a_telecharger)} textes illisibles : API en panne ? Rien n'est modifié.")

    enregistrer_archive(cfg, nouvelles, archive, aujourdhui)
    return archive, len(nouvelles)


# =========================================================================== 3. doublons

def _shingles(texte, n=5):
    mots = norm(texte).split()
    return {" ".join(mots[i:i + n]) for i in range(max(1, len(mots) - n + 1))}


_FORMES_JURIDIQUES = r"\b(gmbh|ag|se|kg|kgaa|mbh|co|ohg|ug|e\.?\s?v|haftungsbeschränkt|germany|deutschland)\b"


def firme_normalisee(nom):
    """'FERCHAU GmbH Niederlassung Bremen' et 'Ferchau GmbH' -> 'ferchau'."""
    n = (nom or "").lower()
    n = re.split(r"\b(niederlassung|standort|filiale|zweigniederlassung|vertriebsstützpunkt|region)\b|[,(/|]", n)[0]
    n = re.sub(_FORMES_JURIDIQUES, " ", n)
    n = re.sub(r"[^a-z0-9äöüß]+", " ", n).strip()
    return n or (nom or "").strip().lower() or "(inconnue)"


def dedoublonner(offres, cfg):
    """
    Même entreprise (agences confondues) + même titre + texte presque identique = une seule offre.
    On garde l'exemplaire le plus proche du lieu de référence, pour ne pas vider la zone locale.
    Traitement en arrière-plan : rien n'est affiché dans les statistiques.
    """
    seuil = float(cfg.get("doublon_aehnlichkeit", 0.85))
    offres = sorted(offres, key=lambda o: o["dist"] if o["dist"] is not None else float("inf"))
    groupes, uniques, doublons, refs_vues = {}, [], 0, set()
    for o in offres:
        if o["statut"] != "ok":
            if o["refnr"] not in refs_vues:
                refs_vues.add(o["refnr"])
                uniques.append(o)
            continue
        cle = (firme_normalisee(o["firma"]), norm(o["titel"]))
        sh = _shingles(o["text"])
        if any(len(sh & autre) / len(sh | autre) >= seuil for autre in groupes.get(cle, []) if sh & autre):
            doublons += 1
            continue
        groupes.setdefault(cle, []).append(sh)
        uniques.append(o)
    log.info("Dédoublonnage : %d doublons retirés, %d offres uniques", doublons, len(uniques))
    return uniques


# =========================================================================== 4. classification

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
            })
        except re.error as err:
            raise StatJobsError(f"sprache.json : regex invalide dans '{e['name']}' : {err}")
    return ex


def classer_langue(titre, texte, expressions):
    """Retourne la liste des expressions de niveau trouvées (une entrée par expression)."""
    corpus = f"{titre}. {texte}"
    trouvees = []
    for e in expressions:
        for motif in e["muster"]:
            hit = None
            for m in motif.finditer(corpus):
                # 'ausschluss' : mention ignorée si le contexte proche l'infirme (ex. "Führerschein C1", "(mind. B2)")
                # la zone 'après' est coupée au prochain point de liste
                apres = re.split(r"\*|•|\s-\s", corpus[m.end():m.end() + e["nach"]], maxsplit=1)[0]
                contexte = corpus[max(0, m.start() - e["vor"]):m.end()] + apres
                if any(x.search(contexte) for x in e["ausschluss"]):
                    continue
                hit = m
                break
            if hit:
                trouvees.append({"name": e["name"], "kategorie": e["kategorie"]})
                break
    return trouvees


def compiler_themes(cfg_themes):
    """themes.json : chaque terme = {name, muster: [regex...]} (au moins une regex doit être trouvée)."""
    out = []
    try:
        for t in cfg_themes["themes"]:
            out.append({"name": t["name"], "muster": [re.compile(r, re.I) for r in t["muster"]]})
    except (re.error, KeyError) as e:
        raise StatJobsError(f"themes.json invalide : {e}")
    return out


def classifier(offres, cfg, sprache, expressions, themes):
    cats = sprache["kategorien"]
    ref = cfg["lieu_reference"]
    rayon = float(cfg["rayon_km"])
    for o in offres:
        try:
            o["dist"] = round(haversine_km(ref["latitude"], ref["longitude"], float(o["lat"]), float(o["lon"])), 1)
        except (TypeError, ValueError):
            o["dist"] = None
        o["zone"] = "local" if (o["dist"] is not None and o["dist"] <= rayon) else "reste"
        ok = o.get("statut") == "ok"
        o["hits"] = classer_langue(o["titel"], o["text"], expressions) if ok else []
        o["c1"] = any(cats[h["kategorie"]]["zaehlt_als_c1"] for h in o["hits"])
        corpus = f"{o['titel']} {o['text']}"
        o["themes"] = [t["name"] for t in themes if any(r.search(corpus) for r in t["muster"])] if ok else []


def offres_de_la_periode(archive, cfg, aujourdhui):
    """Offres de l'archive publiées pour la 1re fois dans la période glissante (en ligne ou déjà retirées)."""
    jours = int(cfg.get("periode_jours", 90))
    debut = (datetime.strptime(aujourdhui, "%Y-%m-%d").date() - timedelta(days=jours)).isoformat()
    res = []
    for o in archive.values():
        d = o.get("datum") or o.get("premiere_vue") or ""
        if d >= debut:
            res.append(dict(o, datum=d))
    return res, debut


# =========================================================================== 5. tableaux du Sheet

def construire_tableaux(offres, cfg, themes, aujourdhui, debut, debut_archive, groupes=None):
    """offres = offres de la période, classées et dédoublonnées, avec un texte exploitable."""
    lib_local = f"{cfg['lieu_reference']['nom']} + {int(cfg['rayon_km'])} km"
    lib_moins = "Moins que C1 ou non précisé"
    loc = [o for o in offres if o["zone"] == "local"]
    nL, nD = len(loc), len(offres)
    c1L, c1D = sum(o["c1"] for o in loc), sum(o["c1"] for o in offres)
    tab = {}
    note_archive = ("" if debut_archive <= debut else
                    f" Archive démarrée le {debut_archive} : les offres retirées avant cette date ne sont pas comptées.")

    # ---------------------------------------------------------------- Résumé
    r = [["Offres IT de mon domaine exigeant un allemand C1 ou plus"],
         [f"Offres publiées du {debut} au {aujourdhui} (encore en ligne ou déjà retirées). "
          f"Mise à jour : {aujourdhui}.{note_archive}"],
         [],
         ["", lib_local, "Allemagne"],
         ["Offres analysées", nL, nD],
         ["C1 ou plus (nombre d'offres)", c1L, c1D],
         ["C1 ou plus (%)", pct(c1L, nL), pct(c1D, nD)],
         [f"{lib_moins} (nombre d'offres)", nL - c1L, nD - c1D],
         [f"{lib_moins} (%)", pct(nL - c1L, nL), pct(nD - c1D, nD)],
         [], []]
    h1 = len(r)
    r.append(["Données du graphique", "C1 ou plus", lib_moins])
    r.append([lib_local, pct(c1L, nL), pct(nL - c1L, nL)])
    r.append(["Allemagne", pct(c1D, nD), pct(nD - c1D, nD)])
    tab["Résumé"] = {
        "values": r, "gras": [0, 3], "gris": (h1, h1 + 2), "col_widths": {0: 330, 1: 150, 2: 120},
        "charts": [{"type": "COLUMN", "stacked": True, "titre": "Niveau d'allemand exigé (% des offres)",
                    "header_row": h1, "last_row": h1 + 2, "domain_col": 0, "series_cols": [1, 2],
                    "anchor_row": 2, "anchor_col": 4, "y_title": "% des offres"}]}

    # ---------------------------------------------------------------- Offres C1
    rows = [["Zone", "Entreprise", "Titre", "Lieu", "Date de publication", "Lien", "Expression(s)"]]
    c1 = [o for o in offres if o["c1"]]
    ordre = (sorted([o for o in c1 if o["zone"] == "local"], key=lambda o: o["datum"], reverse=True) +
             sorted([o for o in c1 if o["zone"] != "local"], key=lambda o: o["datum"], reverse=True))
    for o in ordre:
        lieu = f"{o.get('plz') or ''} {o.get('ort') or ''}".strip() or o.get("region") or "non indiqué"
        rows.append([cellule(x) for x in [
            lib_local if o["zone"] == "local" else "Reste de l'Allemagne",
            o["firma"], o["titel"], lieu, o["datum"],
            LIEN_OFFRE.format(urllib.parse.quote(o["refnr"])),
            " / ".join(h["name"] for h in o["hits"])]])
    tab["Offres C1"] = {"values": rows, "gras": [0], "freeze": 1,
                        "col_widths": {0: 170, 1: 260, 2: 330, 3: 200, 4: 130, 5: 120, 6: 200}}

    # ---------------------------------------------------------------- Tendances
    t = [["Expertise technique demandée (part des offres qui mentionnent chaque terme)"],
         [f"Offres publiées du {debut} au {aujourdhui}. Termes modifiables dans themes.json.{note_archive}"],
         [],
         ["Terme", f"% {lib_local}", "% Allemagne", f"Offres {lib_local}", "Offres Allemagne"]]
    h_classement = len(t) - 1
    lignes = []
    for th in themes:
        n = th["name"]
        a, b = sum(n in o["themes"] for o in loc), sum(n in o["themes"] for o in offres)
        lignes.append([n, pct(a, nL), pct(b, nD), a, b])
    lignes.sort(key=lambda x: x[2], reverse=True)
    t += lignes
    fin_classement = len(t) - 1
    t.append([])
    # -- évolution : moyenne glissante sur N semaines, uniquement si assez d'offres dans la fenêtre
    fenetre = int(cfg.get("tendance_fenetre_semaines", 4))
    minimum = int(cfg.get("tendance_min_offres", 20))
    t.append([f"Évolution - Allemagne : % des offres mentionnant chaque terme, moyenne glissante sur {fenetre} "
              f"semaines (une semaine n'est affichée que si sa fenêtre contient au moins {minimum} offres)"])
    h_sem = len(t)
    t.append(["Semaine (lundi)"] + [th["name"] for th in themes] + ["Offres dans la fenêtre"])
    semaines = {}
    for o in offres:
        w = lundi(o["datum"])
        if w:
            semaines.setdefault(w, []).append(o)
    debut_arch = lundi(debut_archive)
    if semaines:
        w, fin = min(semaines), max(semaines)
        while w <= fin:
            lst = [o for k in range(fenetre) for o in semaines.get(w - timedelta(weeks=k), [])]
            if len(lst) >= minimum:
                etiquette = w.isoformat() + (" *" if debut_arch and w < debut_arch else "")
                t.append([etiquette] + [pct(sum(th["name"] in o["themes"] for o in lst), len(lst)) for th in themes]
                         + [len(lst)])
            w += timedelta(weeks=1)
    fin_sem = len(t) - 1
    if fin_sem == h_sem:
        t.append(["(pas encore assez d'offres pour tracer une évolution)"])
    t.append([])
    t.append(["* semaine antérieure au démarrage de l'archive : incomplète (seules les offres encore en ligne "
              "à ce moment-là sont comptées)."])
    ncols = 2 + len(themes)

    # -- un petit graphique par famille de termes (themes.json -> "groupes")
    col_de = {th["name"]: i + 1 for i, th in enumerate(themes)}
    groupes = [g for g in (groupes or []) if any(n in col_de for n in g.get("termes", []))]
    deja = {n for g in groupes for n in g["termes"]}
    restants = [th["name"] for th in themes if th["name"] not in deja]
    if restants:
        groupes.append({"name": "Autres", "termes": restants})
    charts = [{"type": "BAR", "stacked": False, "titre": "Termes techniques demandés (% des offres)",
               "header_row": h_classement, "last_row": fin_classement, "domain_col": 0, "series_cols": [1, 2],
               "anchor_row": 2, "anchor_col": ncols + 1, "hauteur": 420, "largeur": 640}]
    if fin_sem > h_sem:
        for i, g in enumerate(groupes):
            charts.append({"type": "LINE", "stacked": False, "titre": f"{g['name']} - évolution (% des offres)",
                           "header_row": h_sem, "last_row": fin_sem, "domain_col": 0,
                           "series_cols": [col_de[n] for n in g["termes"] if n in col_de],
                           "anchor_row": 25 + (i // 2) * 19, "anchor_col": ncols + 1 + (i % 2) * 7,
                           "y_title": "% des offres", "largeur": 640, "hauteur": 340})
    tab["Tendances"] = {"values": t, "gras": [0, h_classement, h_sem], "col_widths": {0: 200},
                        "extra_cols": ncols + 16, "charts": charts}
    return tab


# =========================================================================== 6. sauvegarde locale

def sauvegarder_local(tab):
    """Copie CSV des onglets (artefact GitHub) : secours si l'écriture dans le Sheet échoue."""
    os.makedirs(OUT_DIR, exist_ok=True)
    for nom, contenu in tab.items():
        with open(os.path.join(OUT_DIR, f"{nom}.csv"), "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(contenu["values"])
    log.info("Copie CSV écrite dans %s", OUT_DIR)


# =========================================================================== 7. Google Sheets

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
    return gspread.authorize(creds).open_by_key(sheet_id)


def _requete_graphique(sid, d):
    """Requête Sheets 'addChart' à partir d'un descripteur de graphique."""
    def plage(col):
        return {"sourceRange": {"sources": [{"sheetId": sid, "startRowIndex": d["header_row"],
                                             "endRowIndex": d["last_row"] + 1,
                                             "startColumnIndex": col, "endColumnIndex": col + 1}]}}
    horizontal = d["type"] == "BAR"
    basic = {"chartType": d["type"], "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
             "domains": [{"domain": plage(d["domain_col"])}],
             "series": [{"series": plage(c), "targetAxis": "BOTTOM_AXIS" if horizontal else "LEFT_AXIS"}
                        for c in d["series_cols"]]}
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
    """VIDE ENTIÈREMENT le Sheet (tous les onglets et graphiques), puis recrée Résumé / Offres C1 / Tendances."""
    sh = ouvrir_sheet()
    log.info("Sheet ouvert : %s", sh.title)

    # 1. vidage total : un onglet temporaire (un Sheet doit toujours en avoir un), puis suppression de tous les autres
    anciens = sh.worksheets()
    tmp = sh.add_worksheet(title=f"_vidage_{int(time.time())}", rows=1, cols=1)
    for w in anciens:
        sh.del_worksheet(w)
    log.info("Sheet vidé : %d onglets supprimés", len(anciens))

    # 2. création et remplissage des onglets
    feuilles = {}
    for nom, info in tab.items():
        valeurs = info["values"]
        largeur = max([len(x) for x in valeurs] + [1])
        ws = sh.add_worksheet(title=nom, rows=max(len(valeurs) + 5, 20), cols=max(largeur, info.get("extra_cols", 12)))
        ws.update(values=valeurs, range_name="A1", value_input_option="USER_ENTERED")
        feuilles[nom] = ws
        log.info("Onglet '%s' écrit (%d lignes)", nom, len(valeurs))
    sh.del_worksheet(tmp)

    # 3. mise en forme + graphiques (non critique : les tableaux sont déjà écrits)
    try:
        reqs = []
        for nom, ws in feuilles.items():
            info = tab[nom]
            for i in info.get("gras", []):
                reqs.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": i, "endRowIndex": i + 1},
                                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                                            "fields": "userEnteredFormat.textFormat.bold"}})
            if info.get("gris"):
                a, b = info["gris"]
                reqs.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": a, "endRowIndex": b + 1},
                                            "cell": {"userEnteredFormat": {"textFormat": {"foregroundColor": GRIS_CLAIR}}},
                                            "fields": "userEnteredFormat.textFormat.foregroundColor"}})
            if info.get("freeze"):
                reqs.append({"updateSheetProperties": {
                    "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": info["freeze"]}},
                    "fields": "gridProperties.frozenRowCount"}})
            for col, px in info.get("col_widths", {}).items():
                reqs.append({"updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                    "properties": {"pixelSize": px}, "fields": "pixelSize"}})
            for d in info.get("charts", []):
                reqs.append(_requete_graphique(ws.id, d))
        if reqs:
            sh.batch_update({"requests": reqs})
            log.info("Mise en forme et graphiques appliqués")
    except Exception as e:  # noqa: BLE001
        log.warning("Mise en forme / graphiques non appliqués (tableaux écrits quand même) : %s", e)


# =========================================================================== principal

def main(argv=None, session=None):
    ap = argparse.ArgumentParser(description="Statistiques des offres IT (niveau d'allemand, expertise technique).")
    ap.add_argument("--sans-sheet", action="store_true", help="ne pas écrire dans Google Sheets (test)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    cfg = charger_json("config.json")
    sprache = charger_json(cfg["sprache_datei"])
    expressions = compiler_expressions(sprache)          # valide les regex AVANT tout appel réseau
    themes_cfg = charger_json(cfg["themes_datei"])
    themes = compiler_themes(themes_cfg)
    aujourdhui = date.today().isoformat()

    archive, nb_nouvelles = mettre_a_jour_archive(cfg, session or requests.Session(), aujourdhui)

    periode, debut = offres_de_la_periode(archive, cfg, aujourdhui)
    debut_archive = min((o.get("premiere_vue") or aujourdhui) for o in archive.values())
    classifier(periode, cfg, sprache, expressions, themes)
    uniques = [o for o in dedoublonner(periode, cfg) if o.get("statut") == "ok"]
    tab = construire_tableaux(uniques, cfg, themes, aujourdhui, debut, debut_archive, themes_cfg.get("groupes"))
    sauvegarder_local(tab)

    loc = [o for o in uniques if o["zone"] == "local"]
    log.info("RÉSULTAT (%s -> %s) Allemagne : %d offres, %d C1 ou plus (%.1f %%)", debut, aujourdhui,
             len(uniques), sum(o["c1"] for o in uniques), pct(sum(o["c1"] for o in uniques), len(uniques)))
    log.info("RÉSULTAT %s + %s km : %d offres, %d C1 ou plus (%.1f %%)", cfg["lieu_reference"]["nom"],
             cfg["rayon_km"], len(loc), sum(o["c1"] for o in loc), pct(sum(o["c1"] for o in loc), len(loc)))

    if args.sans_sheet:
        log.info("--sans-sheet : écriture Google Sheets ignorée")
        return 0
    try:
        ecrire_sheet(tab)
    except StatJobsError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StatJobsError(f"Écriture Google Sheets échouée ({type(e).__name__}: {e}). L'archive est à jour ; "
                            "le Sheet sera reconstruit à la prochaine exécution (CSV dans l'artefact 'stat-jobs-out').")
    log.info("TERMINÉ : %d nouvelles offres archivées, Google Sheet reconstruit", nb_nouvelles)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StatJobsError as err:
        log.error("ARRÊT : %s", err)
        sys.exit(1)
