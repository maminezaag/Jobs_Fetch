#!/usr/bin/env python3
"""
job_watcher.py
==============

Surveille la Jobbörse de la Bundesagentur für Arbeit via son API officielle
"Jobsuche". Architecture (depuis la refonte du 24.09.2026) : UNE recherche
nationale par mot-clé (aucune restriction géographique au niveau de l'API),
puis chaque offre trouvée est classifiée a posteriori :

    - "locale"    si ses coordonnées GPS sont à moins de rayon_km du point
                  de référence (voir criteres.json > recherche.lieu_reference)
    - "remote"    sinon, si l'annonce mentionne Homeoffice/Remote/etc.
    - "hors_zone" sinon — l'offre est alors ÉCARTÉE (ni accessible en trajet,
                  ni télétravaillable)

Chaque NOUVELLE offre est évaluée selon les critères d'élimination et de
points de criteres.json, vérifiée contre le suivi de candidatures (Google
Sheet), puis un e-mail récapitulatif est envoyé pour les offres qualifiées.

OFFRES AVEC FRANÇAIS (criteres.json > francais, ajout du 14.10.2026) :
recherches supplémentaires nationales + détection du français dans le texte de
TOUTES les offres. Une offre qui demande le français passe le seuil
automatiquement, n'est jamais écartée pour la distance, mais reste éliminée si
elle exige un allemand C1/C2/natif. Elle apparaît dans une section à part de
l'e-mail.

Fichiers utilisés
------------------
- config.yaml    : configuration TECHNIQUE (chemins, e-mail, seuil)
- criteres.json  : configuration MÉTIER (mots-clés, élimination, points)
- seen_jobs.json : historique des offres déjà notifiées (auto-généré)

Utilisation
-----------
    python job_watcher.py --config config.yaml
    python job_watcher.py --config config.yaml --dry-run
    python job_watcher.py --config config.yaml --reset
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import logging
import math
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote

import requests
import yaml

# --------------------------------------------------------------------------
# Constantes API Jobsuche (Bundesagentur für Arbeit)
# --------------------------------------------------------------------------

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
SEARCH_ENDPOINT = f"{API_BASE}/pc/v6/jobs"
DETAILS_ENDPOINT = f"{API_BASE}/pc/v4/jobdetails/{{encoded_refnr}}"
JOB_URL_TEMPLATE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"

# Client ID publique documentée par le projet open-source bundesAPI/jobsuche-api.
API_CLIENT_ID = "jobboerse-jobsuche"
HEADERS = {"X-API-Key": API_CLIENT_ID, "User-Agent": "job-watcher/3.0"}

PAGE_SIZE = 100
REQUEST_TIMEOUT = 30
REQUEST_DELAY_S = 0.4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("job_watcher")


# --------------------------------------------------------------------------
# Modèle de données
# --------------------------------------------------------------------------

@dataclass
class JobResult:
    refnr: str
    title: str
    employer: str
    ort: str
    plz: str
    region: str
    published: str
    latitude: float | None = None
    longitude: float | None = None
    description: str = ""
    score: int = 0
    matched_positive: list[str] = field(default_factory=list)
    matched_elimination: list[str] = field(default_factory=list)
    deja_postule: bool = False
    source: str = "?"  # "locale" / "remote" / "francais" / "hors_zone", déterminé après evaluate()
    francais: bool = False        # l'offre demande le français (criteres.json > francais.mots_detection)
    via_francais_seul: bool = False   # trouvée UNIQUEMENT par les recherches "français"

    @property
    def url(self) -> str:
        return JOB_URL_TEMPLATE.format(refnr=self.refnr)

    @property
    def eliminated(self) -> bool:
        return bool(self.matched_elimination)


# --------------------------------------------------------------------------
# Configuration (config.yaml + criteres.json)
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    """Charge config.yaml (technique) puis fusionne criteres.json (métier)."""
    if not path.exists():
        logger.error("Fichier de configuration introuvable : %s", path)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg.setdefault("criteres", {})
    cfg.setdefault("email", {})
    cfg.setdefault("etat", {})
    cfg.setdefault("candidatures_sheet", {})

    criteres_path = Path(cfg["criteres"].get("fichier", "criteres.json"))
    if not criteres_path.is_absolute():
        criteres_path = path.parent / criteres_path
    if not criteres_path.exists():
        logger.error("Fichier de critères introuvable : %s", criteres_path)
        sys.exit(1)
    with criteres_path.open("r", encoding="utf-8") as f:
        criteres_data = json.load(f)

    cfg["criteres"]["recherche"] = criteres_data.get("recherche", {})
    elim = criteres_data.get("elimination", {})
    cfg["criteres"]["elimination_stricte"] = elim.get("mots_stricts", [])
    cfg["criteres"]["elimination_souple"] = elim.get("mots_souples", [])
    cfg["criteres"]["b2_marqueurs"] = elim.get("b2_marqueurs", [])
    cfg["criteres"]["points_positifs"] = criteres_data.get("points_positifs", {}).get("groupes", [])
    cfg["criteres"]["francais"] = criteres_data.get("francais", {})

    return cfg


def resolve_path(cfg_path: Path, raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else cfg_path.parent / p


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Fichier d'état corrompu, on repart de zéro : %s", path)
    return {"vus": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Transport API fiable : les erreurs temporaires sont retentees.
# --------------------------------------------------------------------------

def _api_json_avec_retry(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    contexte: str = "API",
    essais: int = 3,
) -> tuple[dict[str, Any], str]:
    """Retourne (JSON, URL finale). Ne masque jamais une erreur persistante."""
    for tentative in range(1, essais + 1):
        try:
            response = session.get(
                url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError(f"Réponse JSON inattendue ({type(data).__name__})")
            return data, response.url
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # Les 4xx permanentes (ex. une annonce retiree -> 404) ne
            # beneficient pas de nouvelles tentatives.
            erreur_permanente = status is not None and 400 <= status < 500 and status not in (408, 429)
            if tentative == essais or erreur_permanente:
                raise
            pause = 2 * tentative
            logger.warning(
                "%s : tentative %s/%s échouée (%s), nouvel essai dans %ss",
                contexte, tentative, essais, exc, pause,
            )
            time.sleep(pause)
    raise RuntimeError(f"{contexte} : échec inattendu")


# --------------------------------------------------------------------------
# Recherche (une seule liste de mots-clés, nationale, sans filtre de lieu)
# --------------------------------------------------------------------------

def _run_search(
    session: requests.Session,
    query: str,
    common_params: dict[str, Any],
    fusion: dict[str, dict[str, Any]],
) -> None:
    """Exécute une recherche paginée pour un mot-clé donné, nationale (aucun
    filtre wo/umkreis/arbeitszeit) et ajoute les résultats dans 'fusion'
    (dédoublonnage par referenznummer)."""
    page = 1
    while True:
        params = {
            "was": query,
            "size": PAGE_SIZE,
            "page": page,
            **common_params,
        }
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params = {
            k: ("true" if v is True else "false" if v is False else v)
            for k, v in params.items()
        }

        logger.info("Recherche %r (page %s)...", query, page)
        try:
            data, final_url = _api_json_avec_retry(
                session, SEARCH_ENDPOINT, params=params,
                contexte=f"Recherche {query!r}, page {page}",
            )
            logger.info("URL appelée : %s", final_url)
        except (requests.RequestException, ValueError) as exc:
            # IMPORTANT : ne pas envoyer de notifications ni enregistrer l'état
            # si une recherche est partielle, sous peine de perdre des offres.
            raise RuntimeError(f"Recherche incomplète pour {query!r}, page {page}") from exc

        # Avec ZERO résultat, l'API /pc/v6/jobs omet `ergebnisliste` :
        # {"maxErgebnisse": 0, ...} est une réponse tout à fait valide.
        # En revanche, une réponse vide, une erreur JSON de proxy ou un
        # `maxErgebnisse` absent/invalide ne doit PAS être prise pour 0 annonce.
        try:
            if "maxErgebnisse" not in data or isinstance(data["maxErgebnisse"], bool):
                raise ValueError("maxErgebnisse absent ou invalide")
            max_results = int(data["maxErgebnisse"])
            if max_results < 0:
                raise ValueError("maxErgebnisse négatif")
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Réponse API incompatible pour {query!r}, page {page} : "
                f"maxErgebnisse absent/invalide ; clés reçues : {list(data)}"
            ) from exc

        offers = data.get("ergebnisliste")
        if max_results == 0 and offers is None:
            logger.info("Recherche %r : aucun résultat (maxErgebnisse=0).", query)
            return
        if not isinstance(offers, list) or (max_results == 0 and offers):
            raise RuntimeError(
                f"Réponse API incompatible pour {query!r}, page {page} : "
                f"ergebnisliste incohérente avec maxErgebnisse={max_results} ; "
                f"clés reçues : {list(data)}"
            )
        # NOTE (23-24.09.2026) : la réponse réelle en production diffère du
        # schéma openapi.yaml documenté (périmé). Clé de la liste d'offres :
        # "ergebnisliste" (pas "stellenangebote") ; identifiant d'offre :
        # "referenznummer" (pas "refnr"). Voir main() pour le mapping complet.
        for o in offers:
            refnr = o.get("referenznummer")
            if refnr and refnr not in fusion:
                fusion[refnr] = o

        if not offers or page * PAGE_SIZE >= max_results:
            return
        page += 1
        time.sleep(REQUEST_DELAY_S)


def search_jobs(cfg: dict[str, Any], session: requests.Session) -> list[dict[str, Any]]:
    """Lance une recherche nationale (sans filtre géographique) pour chaque
    mot-clé de criteres.json > recherche.mots_cles, fusionnée et dédoublonnée."""
    recherche = cfg["criteres"]["recherche"]
    commun = recherche.get("parametres_communs", {})
    # "interim": "alle" -> NE PAS envoyer 'zeitarbeit' du tout.
    # API : true = intérim seulement, false = hors intérim seulement.
    # Absence du paramètre = les deux catégories.
    interim = commun.get("interim", "alle")
    if interim is None or (isinstance(interim, str) and interim.strip().lower() in ("alle", "tous", "all", "both")):
        zeitarbeit = None
    elif isinstance(interim, bool):
        zeitarbeit = interim
    elif isinstance(interim, str) and interim.strip().lower() in ("true", "false"):
        zeitarbeit = interim.strip().lower() == "true"
    else:
        raise ValueError("recherche.parametres_communs.interim doit valoir 'alle', true ou false")

    common_params = {
        "berufsfeld": commun.get("domaine"),
        "veroeffentlichtseit": commun.get("jours", 7),
        "angebotsart": commun.get("type_offre", 1),
        "zeitarbeit": zeitarbeit,
    }
    logger.info("Filtre intérim : %s", "toutes les offres" if zeitarbeit is None else f"zeitarbeit={zeitarbeit}")

    fusion: dict[str, dict[str, Any]] = {}
    for query in recherche.get("mots_cles", []):
        _run_search(session, query, common_params, fusion)

    # Recherches "français" : nationales, SANS filtre de domaine (berufsfeld). Les offres trouvées
    # uniquement ici sont marquées pour le filtre de titre (criteres.json > francais.titre_doit_contenir).
    francais = cfg["criteres"].get("francais", {}) or {}
    if francais.get("mots_cles"):
        params_fr = {**common_params, "berufsfeld": None}
        fusion_fr: dict[str, dict[str, Any]] = {}
        for query in francais["mots_cles"]:
            _run_search(session, query, params_fr, fusion_fr)
        nouvelles_fr = 0
        for refnr, o in fusion_fr.items():
            if refnr not in fusion:
                o["_via_francais_seul"] = True
                fusion[refnr] = o
                nouvelles_fr += 1
        logger.info("Recherches 'français' : %d offres, dont %d non trouvées par les mots-clés habituels",
                    len(fusion_fr), nouvelles_fr)

    logger.info("Total offres uniques trouvées : %d", len(fusion))

    if not fusion:
        nb_mots_cles = len(recherche.get("mots_cles", []))
        logger.warning(
            "Aucune offre trouvée par AUCUN des %d mots-clés — test diagnostic "
            "avec une requête minimale (was=Linux, sans autre filtre)...",
            nb_mots_cles,
        )
        try:
            r = session.get(
                SEARCH_ENDPOINT, headers=HEADERS,
                params={"was": "Linux", "size": 5, "page": 1}, timeout=REQUEST_TIMEOUT,
            )
            logger.warning("[diagnostic] URL appelée : %s", r.url)
            logger.warning("[diagnostic] Code HTTP : %s", r.status_code)
            diag_data = r.json()
            logger.warning("[diagnostic] Clés de premier niveau : %s", list(diag_data.keys()))
            for key, value in diag_data.items():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    logger.warning(
                        "[diagnostic] '%s' : %d élément(s), clés du premier : %s",
                        key, len(value), list(value[0].keys()),
                    )
        except requests.RequestException as e:
            logger.warning("[diagnostic] La requête minimale a aussi échoué : %s", e)
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("[diagnostic] Réponse reçue mais pas du JSON valide : %s", e)

    return list(fusion.values())


_description_field_warned = False


def fetch_description(session: requests.Session, refnr: str) -> str:
    """Récupère la description complète de l'offre (refnr encodé en base64
    dans le chemin). Retourne une chaîne vide en cas d'échec plutôt que de
    faire échouer tout le run."""
    global _description_field_warned
    if not refnr:
        return ""
    encoded_refnr = url_quote(base64.b64encode(refnr.encode("utf-8")).decode("ascii"), safe="")
    try:
        data, _ = _api_json_avec_retry(
            session, DETAILS_ENDPOINT.format(encoded_refnr=encoded_refnr),
            contexte=f"Détail {refnr}",
        )
        description = data.get("stellenangebotsBeschreibung") or data.get("stellenbeschreibung")
        if not description and not _description_field_warned:
            logger.warning(
                "Aucun champ de description reconnu dans jobdetails — clés : %s",
                list(data.keys()),
            )
            _description_field_warned = True
        return description or ""
    except (requests.RequestException, ValueError) as e:
        logger.warning("Détails indisponibles pour refnr=%s : %s", refnr, e)
        return ""


# --------------------------------------------------------------------------
# Distance géographique (classification locale / remote / hors_zone)
# --------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance à vol d'oiseau en km entre deux points GPS (formule de
    Haversine). Suffisant pour classifier 'proche/loin' — pas besoin de la
    distance routière réelle pour ce cas d'usage."""
    R = 6371.0  # rayon moyen de la Terre en km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def classify_source(job: JobResult, cfg: dict[str, Any]) -> None:
    """Détermine job.source ('locale' / 'remote' / 'hors_zone'), APPELÉE
    APRÈS evaluate() car elle réutilise job.matched_positive pour savoir si
    le groupe de points 'Homeoffice / Remote / Hybrid' a été détecté (une
    seule liste de synonymes remote à maintenir, dans criteres.json).

    Si ni proche ni remote : l'offre est ajoutée à matched_elimination —
    inatteignable en trajet ET non télétravaillable, donc inutile à notifier.
    """
    recherche = cfg["criteres"]["recherche"]
    ref = recherche.get("lieu_reference", {})
    rayon_km = recherche.get("rayon_km", 60)

    distance_km: float | None = None
    if job.latitude is not None and job.longitude is not None and ref.get("latitude") is not None:
        distance_km = haversine_km(job.latitude, job.longitude, ref["latitude"], ref["longitude"])

    remote_detecte = any(m.startswith("Homeoffice / Remote / Hybrid") for m in job.matched_positive)

    if distance_km is not None and distance_km <= rayon_km:
        job.source = "locale"
    elif job.francais:
        # offre avec français : toute l'Allemagne, jamais écartée pour la distance
        job.source = "francais"
    elif remote_detecte:
        job.source = "remote"
    else:
        job.source = "hors_zone"
        motif = f"hors zone ({distance_km:.0f} km)" if distance_km is not None else "lieu inconnu"
        job.matched_elimination.append(f"{motif} et non taggé remote")


# --------------------------------------------------------------------------
# Évaluation (élimination stricte/souple + règle B2, points positifs)
# --------------------------------------------------------------------------

def evaluate(job: JobResult, cfg: dict[str, Any]) -> None:
    """Applique les critères d'élimination (avec règle B2, voir criteres.json)
    puis calcule le score de points positifs.

    RÈGLE B2 : si l'annonce mentionne explicitement B2 comme niveau accepté
    (b2_marqueurs), les critères d'élimination "souples" (formulations vagues
    type 'verhandlungssicheres Deutsch') sont neutralisés — seuls les critères
    "stricts" (C1/C2 explicite, niveau natif) éliminent encore l'offre.

    RÈGLE POINTS : chaque groupe de points_positifs est accordé UNE SEULE FOIS
    même si plusieurs synonymes du groupe apparaissent (voir criteres.json)."""
    crit = cfg["criteres"]
    haystack = f"{job.title}\n{job.description}".lower()

    # --- français (criteres.json > francais)
    francais = crit.get("francais", {}) or {}
    mots_fr = [m for m in francais.get("mots_detection", []) if m.lower() in haystack]
    job.francais = bool(mots_fr)

    matched_stricte = [kw for kw in crit.get("elimination_stricte", []) if kw.lower() in haystack]
    matched_souple = [kw for kw in crit.get("elimination_souple", []) if kw.lower() in haystack]
    b2_present = any(m.lower() in haystack for m in crit.get("b2_marqueurs", []))

    if job.francais:
        # Sur une offre française, 'Niveau C1' / 'mindestens C1' seuls visent souvent le FRANÇAIS :
        # on n'élimine que sur les expressions qui nomment explicitement l'allemand.
        matched_stricte = [kw for kw in matched_stricte if "deutsch" in kw.lower()]
        matched_souple = [kw for kw in matched_souple if "deutsch" in kw.lower()]

    if b2_present:
        job.matched_elimination = matched_stricte
    else:
        job.matched_elimination = matched_stricte + matched_souple

    score = 0
    for groupe in crit.get("points_positifs", []) or []:
        nom = groupe.get("nom", "?")
        poids = int(groupe.get("poids", 0))
        mots = groupe.get("mots", []) or []
        synonymes_trouves = [m for m in mots if m.lower() in haystack]
        if synonymes_trouves:
            score += poids
            job.matched_positive.append(f"{nom} ({'/'.join(synonymes_trouves)})")

    job.score = score
    if job.francais:
        job.matched_positive.insert(0, f"Français ({'/'.join(mots_fr[:3])})")

    # Offre trouvée UNIQUEMENT par les recherches "français" : le titre doit être un poste IT
    if job.via_francais_seul:
        mots_titre = francais.get("titre_doit_contenir", []) or []
        if mots_titre and not any(_mot_dans_titre(m, job.title) for m in mots_titre):
            job.matched_elimination.append("titre hors IT (recherche 'français')")


def _mot_dans_titre(mot: str, titre: str) -> bool:
    """Le mot doit commencer un mot du titre ('Admin' trouve 'Administrator') ; les mots de 3 lettres
    ou moins doivent être des mots entiers ('IT' trouve 'IT-Support' mais pas 'Mitarbeiter')."""
    fin = r"\b" if len(mot) <= 3 else ""
    return re.search(r"(?<![a-zäöüß0-9])" + re.escape(mot) + fin, titre, re.IGNORECASE) is not None


# --------------------------------------------------------------------------
# Anti-doublon de candidature (Google Sheet "Meine Bewerbungen")
# --------------------------------------------------------------------------

_SUFFIXES_ENTREPRISE = re.compile(r"\b(gmbh|ag|kg|co\.?\s*kg|ug|mbh|se|e\.?v\.?|gbr)\b", re.IGNORECASE)


def _normaliser_nom_entreprise(nom: str) -> str:
    nom = _SUFFIXES_ENTREPRISE.sub("", nom)
    nom = re.sub(r"[^\w\s]", " ", nom, flags=re.UNICODE)
    nom = re.sub(r"\s+", " ", nom).strip().lower()
    return nom


def load_recent_companies(cfg: dict[str, Any], session: requests.Session) -> list[str]:
    """Télécharge l'onglet Google Sheet (export CSV public) et retourne les
    N dernières entreprises normalisées de la colonne configurée."""
    sheet_cfg = cfg.get("candidatures_sheet", {})
    if not sheet_cfg.get("actif", False):
        return []

    url = sheet_cfg.get("url_csv")
    colonne = sheet_cfg.get("colonne_entreprise", "Firma")
    n = int(sheet_cfg.get("nb_dernieres_lignes", 30))

    if not url:
        logger.warning("candidatures_sheet.actif=true mais url_csv manquant — vérification ignorée.")
        return []

    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Impossible de lire le Google Sheet de candidatures : %s", e)
        return []

    reader = csv.DictReader(io.StringIO(r.text))
    entetes = reader.fieldnames or []
    colonne_reelle = next((h for h in entetes if h.strip() == colonne.strip()), None)
    if colonne_reelle is None:
        logger.warning("Colonne %r introuvable dans le Google Sheet (colonnes : %s)", colonne, entetes)
        return []

    noms = [row[colonne_reelle].strip() for row in reader if row.get(colonne_reelle, "").strip()]
    dernieres = noms[-n:] if n > 0 else noms
    logger.info("Suivi de candidatures : %d entreprise(s) récente(s) chargée(s).", len(dernieres))
    return [_normaliser_nom_entreprise(n) for n in dernieres]


def marquer_doublons(jobs: list[JobResult], entreprises_normalisees: list[str]) -> None:
    """Marque job.deja_postule=True si l'employeur figure (comparaison
    tolérante) parmi les entreprises récentes du suivi. N'élimine RIEN."""
    if not entreprises_normalisees:
        return
    for job in jobs:
        employeur_norm = _normaliser_nom_entreprise(job.employer)
        if not employeur_norm:
            continue
        for connue in entreprises_normalisees:
            if connue and (employeur_norm in connue or connue in employeur_norm):
                job.deja_postule = True
                break


# --------------------------------------------------------------------------
# Notification e-mail
# --------------------------------------------------------------------------

def _lignes_offre(j: JobResult) -> list[str]:
    avertissement = " ⚠️ eventuell bereits beworben" if j.deja_postule else ""
    return [
        f"● {j.title} — {j.employer}{avertissement}",
        f"  Lieu : {j.plz} {j.ort} ({j.region}) | Source : {j.source}",
        f"  Score : {j.score} points | Compétences : {', '.join(j.matched_positive) or '-'}",
        f"  Publié le : {j.published}",
        f"  Lien : {j.url}",
        "",
    ]


def build_email_body(jobs: list[JobResult]) -> str:
    fr = sorted([j for j in jobs if j.francais], key=lambda x: x.score, reverse=True)
    autres = sorted([j for j in jobs if not j.francais], key=lambda x: x.score, reverse=True)
    lines = [f"{len(jobs)} nouvelle(s) offre(s) correspondant à tes critères :", ""]
    if fr:
        lines += [f"🇫🇷 OFFRES AVEC FRANÇAIS ({len(fr)}) — toute l'Allemagne", "=" * 50, ""]
        for j in fr:
            lines += _lignes_offre(j)
        if autres:
            lines += [f"AUTRES OFFRES ({len(autres)})", "=" * 50, ""]
    for j in autres:
        lines += _lignes_offre(j)
    return "\n".join(lines)


def send_email(cfg: dict[str, Any], jobs: list[JobResult]) -> bool:
    email_cfg = cfg["email"]
    host = email_cfg.get("smtp_host", "smtp.gmail.com")
    port = int(email_cfg.get("smtp_port", 587))

    address = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_raw = os.environ.get("GMAIL_TO")

    if not all([address, app_password, to_raw]):
        logger.error(
            "Variables d'environnement manquantes : GMAIL_ADDRESS / GMAIL_APP_PASSWORD / "
            "GMAIL_TO doivent être définies. Aucun e-mail envoyé."
        )
        return False

    recipients = [addr.strip() for addr in to_raw.split(",") if addr.strip()]
    nb_fr = sum(1 for j in jobs if j.francais)
    subject = f"[Job Watcher] {len(jobs)} nouvelle(s) offre(s) IT à Oldenburg/Bremen"
    if nb_fr:
        subject += f" (dont {nb_fr} avec français 🇫🇷)"
    body = build_email_body(jobs)

    msg = MIMEMultipart()
    msg["From"] = address
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=REQUEST_TIMEOUT) as server:
            server.starttls()
            server.login(address, app_password)
            server.sendmail(address, recipients, msg.as_string())
        logger.info("E-mail envoyé à %s (%d offre(s)).", ", ".join(recipients), len(jobs))
        return True
    except Exception as e:
        logger.error("Échec de l'envoi de l'e-mail : %s", e)
        return False


# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Surveille la Jobbörse Arbeitsagentur et notifie par e-mail.")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reset", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    if args.dry_run and args.reset:
        raise SystemExit("--dry-run et --reset ne peuvent pas être combinés (protection de seen_jobs.json).")
    st_path = resolve_path(args.config, cfg["etat"].get("fichier", "seen_jobs.json"))

    if args.reset and st_path.exists():
        st_path.unlink()
        logger.info("Historique réinitialisé (%s supprimé).", st_path)

    state = load_state(st_path)
    state.setdefault("vus", {})

    session = requests.Session()
    raw_offers = search_jobs(cfg, session)
    new_offers = [o for o in raw_offers if o.get("referenznummer") not in state["vus"]]
    logger.info("Nouvelles offres depuis le dernier passage : %d", len(new_offers))

    results: list[JobResult] = []
    for o in new_offers:
        lokalisations = o.get("stellenlokationen") or [{}]
        loc0 = lokalisations[0] or {}
        adresse = loc0.get("adresse", {})
        job = JobResult(
            refnr=o.get("referenznummer", ""),
            title=o.get("stellenangebotsTitel") or o.get("hauptberuf", ""),
            employer=o.get("firma", ""),
            ort=adresse.get("ort", ""),
            plz=str(adresse.get("plz", "")),
            region=adresse.get("region", ""),
            published=o.get("datumErsteVeroeffentlichung")
                      or (o.get("veroeffentlichungszeitraum") or {}).get("von", ""),
            latitude=loc0.get("breite"),
            longitude=loc0.get("laenge"),
            via_francais_seul=bool(o.get("_via_francais_seul")),
        )
        job.description = fetch_description(session, job.refnr)
        if not job.description.strip():
            logger.warning(
                "Annonce %s sans description : non marquée comme vue ; "
                "elle sera retentée au prochain passage si toujours en ligne.",
                job.refnr,
            )
            continue
        evaluate(job, cfg)
        classify_source(job, cfg)  # doit venir APRÈS evaluate() (utilise matched_positive)
        results.append(job)

        state["vus"][job.refnr] = datetime.now(timezone.utc).isoformat()
        time.sleep(REQUEST_DELAY_S)

    seuil = int(cfg["criteres"].get("seuil_points", 10))
    # une offre avec français passe le seuil automatiquement (si elle n'est pas éliminée)
    qualifies = [j for j in results if not j.eliminated and (j.score >= seuil or j.francais)]
    eliminees = [j for j in results if j.eliminated]
    sous_le_seuil = [j for j in results if not j.eliminated and j.score < seuil and not j.francais]

    if qualifies:
        entreprises_recentes = load_recent_companies(cfg, session)
        marquer_doublons(qualifies, entreprises_recentes)

    logger.info(
        "Résumé : %d analysées | %d éliminées | %d sous le seuil | %d qualifiées (seuil=%d pts)",
        len(results), len(eliminees), len(sous_le_seuil), len(qualifies), seuil,
    )
    for j in qualifies:
        flag = " [DOUBLON POSSIBLE]" if j.deja_postule else ""
        fr = " 🇫🇷" if j.francais else ""
        logger.info("  ✔ %s — %s (%d pts)%s%s [%s] %s", j.title, j.employer, j.score, fr, flag, j.source, j.url)
    for j in eliminees:
        logger.info("  ✘ %s — %s (élim.: %s)", j.title, j.employer, ", ".join(j.matched_elimination))
    for j in sorted(sous_le_seuil, key=lambda x: x.score, reverse=True):
        logger.info("  ○ %s — %s (%d pts, sous le seuil de %d) [%s] %s", j.title, j.employer, j.score, seuil, j.source, j.url)

    if qualifies and not args.dry_run:
        if not send_email(cfg, qualifies):
            # Sans confirmation SMTP, ces offres doivent rester disponibles
            # pour une nouvelle notification lors de la prochaine exécution.
            for j in qualifies:
                state["vus"].pop(j.refnr, None)
            logger.warning(
                "SMTP en échec : %d annonce(s) qualifiée(s) NON marquée(s) vue(s) ; nouvel essai au prochain run.",
                len(qualifies),
            )
    elif qualifies and args.dry_run:
        print("\n--- APERÇU (dry-run, aucun e-mail envoyé) ---\n")
        print(build_email_body(qualifies))

    if args.dry_run:
        logger.info("Mode dry-run : seen_jobs.json laissé intact.")
    else:
        save_state(st_path, state)


if __name__ == "__main__":
    main()
