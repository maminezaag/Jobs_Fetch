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
    - "remote"    sinon, SEULEMENT si l'annonce confirme 100% télétravail.
    - "hors_zone" sinon — l'offre est alors ÉCARTÉE (ni accessible en trajet,
                  ni télétravaillable)

Chaque NOUVELLE offre est évaluée selon les critères d'élimination et de
points de criteres.json, vérifiée contre le suivi de candidatures (Google
Sheet), puis un e-mail récapitulatif est envoyé pour les offres qualifiées.

OFFRES AVEC FRANÇAIS (criteres.json > francais, ajout du 14.10.2026) :
recherches supplémentaires nationales + détection du français dans le texte de
TOUTES les offres. Une annonce demandant le français conserve la dérogation
au seuil et au rayon de 70 km SI elle concerne un métier IT pertinent et si
son lieu est bien en Allemagne. Les exigences d'allemand C1/C2/natif restent
éliminatoires. Les offres françaises apparaissent dans une section dédiée.

Fichiers utilisés
------------------
- config.yaml    : configuration TECHNIQUE (chemins, e-mail, seuil)
- criteres.json  : configuration MÉTIER (mots-clés, élimination, points)
- seen_jobs.json : historique des offres déjà analysées (auto-généré)

Utilisation
-----------
    python job_watcher.py --config config.yaml
    python job_watcher.py --config config.yaml --dry-run
    python job_watcher.py --config config.yaml --dry-run --review-seen
    python job_watcher.py --config config.yaml --reset
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import unicodedata
from difflib import SequenceMatcher
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
    score_brut: int = 0
    penalite_metier: int = 0
    bonus_metier: int = 0
    metier_cible: bool = False
    priorite_metier: str = "À examiner"
    statut_teletravail: str = "aucun"
    localisations: list[dict[str, Any]] = field(default_factory=list)
    motifs_adequation: list[str] = field(default_factory=list)
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
    cfg["criteres"]["adequation_metier"] = criteres_data.get("adequation_metier", {})
    cfg["criteres"]["priorites_metier"] = criteres_data.get("priorites_metier", {})

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


# Une déclaration de télétravail PARTIEL n'autorise PAS un poste éloigné.
# Les mentions « Remote-Support » désignent une technique de support, pas
# nécessairement la possibilité de travailler depuis Oldenburg.
_RE_TELETRAVAIL_TOTAL = [
    r"\b100\s*%\s*(?:remote|home[ -]?office|mobiles? arbeiten|telearbeit)\b",
    r"\b(?:remote|home[ -]?office|telearbeit)\s*(?:zu\s*)?100\s*%\b",
    r"\b(?:fully|completely|entirely|full)\s+remote\b",
    r"\bfull[ -]remote\b",
    r"\b(?:vollständig|komplett|ausschließlich|rein)\s+(?:remote|im home[ -]?office)\b",
    r"\b(?:remote|home[ -]?office)\s+(?:deutschlandweit|bundesweit|innerhalb deutschlands)\b",
    r"\b(?:deutschlandweit|bundesweit)\s+(?:remote|home[ -]?office)\b",
    r"\b(?:ortsunabhängig|ortsunabhaengig)\s+(?:arbeiten|tätigkeit|arbeit)\b",
    r"\bwork\s+from\s+anywhere\s+in\s+germany\b",
]
_RE_TELETRAVAIL_PARTIEL = re.compile(
    r"(?<!\w)(?:home[ -]?office|remote(?:[ -]?work)?|hybrid(?:es)?|mobiles arbeiten|mobile arbeit|telearbeit)(?!\w)",
    re.IGNORECASE,
)
_RE_TELETRAVAIL_NEGATIF = re.compile(
    r"\b(?:kein|keine|keinen|ohne|nicht|no)\s+(?:home[ -]?office|remote|telearbeit)\b"
    r"|\b(?:home[ -]?office|remote)\s+(?:nicht möglich|ausgeschlossen|not available)\b",
    re.IGNORECASE,
)
_RE_TELETRAVAIL_LIMITES = re.compile(
    r"\b(?:[1-4]|ein(?:e|en|em)?|zwei|drei|vier)\s*"
    r"(?:tage?|days?)\s*(?:pro|per|in der|in a)?\s*(?:woche|week)?\s*"
    r"(?:im\s*)?(?:home[ -]?office|remote)\b"
    r"|\b(?:home[ -]?office|remote)\s*(?:an|für|bis zu|up to)\s*"
    r"(?:[1-4]|ein(?:e|en)?|zwei|drei|vier)\s*(?:tage?|days?)\b",
    re.IGNORECASE,
)

_RE_PRESENCE_OBLIGATOIRE = re.compile(
    r"\b(?:[1-4]|ein(?:e|en|em)?|zwei|drei|vier)\s+(?:tage?|days?)"
    r"(?:\s+(?:pro|per)\s+(?:woche|week))?\s+(?:vor\s+ort|im\s+büro|onsite|in\s+the\s+office)\b"
    r"|\b(?:regelmäßige|regelmaessige|wöchentliche|woechentliche)\s+präsenz\b",
    re.IGNORECASE,
)


def statut_teletravail(texte: str) -> str:
    """'total', 'partiel' ou 'aucun'. Seul 'total' autorise le hors-zone."""
    # La ponctuation HTML ne doit pas relier des mots sans séparateur.
    t = re.sub(r"<[^>]*>", " ", texte or "")
    # Remote-Support décrit une opération sur un PC distant, non le lieu de travail.
    t = re.sub(r"\bremote[ -]?support\b", " support_technique ", t, flags=re.I)
    if _RE_TELETRAVAIL_NEGATIF.search(t):
        # Un négatif absolu prévaut par sécurité sur une mention contradictoire.
        return "aucun"
    if _RE_TELETRAVAIL_LIMITES.search(t) or _RE_PRESENCE_OBLIGATOIRE.search(t):
        return "partiel"
    if any(re.search(p, t, re.IGNORECASE) for p in _RE_TELETRAVAIL_TOTAL):
        return "total"
    return "partiel" if _RE_TELETRAVAIL_PARTIEL.search(t) else "aucun"


# Repli contrôlé pour les annonces sans latitude/longitude.
# Seules des villes locales avec codes postaux cohérents sont acceptées.
# Pour une ville inconnue, on ne devine pas : pas de validation automatique.
_VILLES_PROCHES: dict[str, tuple[str, ...]] = {
    "oldenburg": ("26",),
    "bremen": ("28",),
    "delmenhorst": ("277",),
    "hude": ("27798",),
    "bad zwischenahn": ("26160",),
    "westerstede": ("26655",),
    "sandkrug": ("26209",),
    "hatten": ("26209",),
    "brake": ("26919",),
    "berne": ("27804",),
    "rastede": ("26180",),
    "wardenbug": ("26203",),
    "wardenburg": ("26203",),
    "edewecht": ("26188",),
    "elsfleth": ("26931",),
    "ganderkesee": ("27777",),
    "wildeshausen": ("27793",),
    "varel": ("26316",),
    "wilhelmshaven": ("263",),
    "leer": ("26789",),
}


def _localisation_proche_sans_gps(ort: str, plz: str) -> bool:
    ville = re.sub(r"\s*\([^)]*\)", "", ort or "").strip().lower()
    ville = ville.replace("oldenburg (oldb)", "oldenburg")
    prefixes = _VILLES_PROCHES.get(ville)
    return bool(prefixes and str(plz or "").strip().startswith(prefixes))


def _flottant(valeur: Any) -> float | None:
    try:
        return float(valeur) if valeur not in (None, "") else None
    except (ValueError, TypeError):
        return None


def classify_source(job: JobResult, cfg: dict[str, Any]) -> None:
    """Évalue tous les lieux. Hors rayon : uniquement 100% remote ou français."""
    recherche = cfg["criteres"]["recherche"]
    ref = recherche.get("lieu_reference", {})
    rayon_km = float(recherche.get("rayon_km", 70))
    ref_lat, ref_lon = _flottant(ref.get("latitude")), _flottant(ref.get("longitude"))

    local = False
    distances = []
    lieux = job.localisations or [{
        "breite": job.latitude, "laenge": job.longitude,
        "adresse": {"ort": job.ort, "plz": job.plz},
    }]
    for loc in lieux:
        adresse = loc.get("adresse") or {}
        lat, lon = _flottant(loc.get("breite")), _flottant(loc.get("laenge"))
        dist = haversine_km(lat, lon, ref_lat, ref_lon) if None not in (lat, lon, ref_lat, ref_lon) else None
        if dist is not None:
            distances.append(dist)
        if (dist is not None and dist <= rayon_km) or (
            dist is None and _localisation_proche_sans_gps(adresse.get("ort", ""), adresse.get("plz", ""))
        ):
            local = True
            # Signaler l'emplacement local réellement disponible plutôt que
            # la première ville (éventuellement très éloignée).
            job.ort = adresse.get("ort") or job.ort
            job.plz = str(adresse.get("plz") or job.plz)
            break

    job.statut_teletravail = statut_teletravail(f"{job.title}\n{job.description}")
    if local:
        job.source = "locale"
    elif job.francais and _lieu_francais_allemagne(job):
        job.source = "francais"  # exception nationale EN ALLEMAGNE uniquement
    elif job.francais and not _lieu_francais_allemagne(job):
        job.source = "hors_zone"
        job.matched_elimination.append("offre en français : localisation allemande non vérifiée")
    elif job.statut_teletravail == "total":
        job.source = "remote"
    else:
        job.source = "hors_zone"
        distance = f"{min(distances):.0f} km" if distances else "lieu sans GPS non reconnu"
        job.matched_elimination.append(
            f"hors zone ({distance}) ; télétravail {job.statut_teletravail}, pas de 100% remote confirmé"
        )


# --------------------------------------------------------------------------
# Évaluation (élimination stricte/souple + règle B2, points positifs)
# --------------------------------------------------------------------------

def _expression_presente(expression: str, texte: str) -> bool:
    """Recherche Unicode avec limites de mots (Git != digitale, SAN != interessant).

    Les séparateurs comme espace, tiret et slash restent autorisés : GitLab,
    CI/CD, Windows-Server, SAN-Storage et les expressions composées fonctionnent.
    """
    expression = str(expression).strip()
    if not expression:
        return False
    return re.search(r"(?<!\w)" + re.escape(expression) + r"(?!\w)", texte, re.IGNORECASE) is not None


def _penalite_devops_cicd(job: JobResult, crit: dict[str, Any]) -> tuple[int, str]:
    """Baisse la priorité des postes DevOps centrés sur une compétence CI/CD exigée.

    Une simple mention de CI/CD (ou "wünschenswert") ne déclenche rien.
    Les postes d'administration système mentionnant CI/CD restent inchangés.
    """
    regle = (crit.get("adequation_metier", {}) or {}).get("devops_cicd", {}) or {}
    if not regle.get("actif", False):
        return 0, ""

    titres = regle.get("mots_titre", ["DevOps", "CI/CD"])
    if not any(_expression_presente(m, job.title) for m in titres):
        return 0, ""

    # Séparer les lignes de compétences d'un texte HTML éventuel ; ne pas
    # relier une exigence située sur une ligne à un mot CI/CD d'une autre.
    description = re.sub(r"</(?:p|li|div|h[1-6]|ul|ol)>|<br\s*/?>", "\n", job.description, flags=re.IGNORECASE)
    description = re.sub(r"<[^>]+>", " ", description)
    clauses = re.split(r"[\n\r.!?;]+", description)
    exigence_detectee = False
    for clause in clauses:
        if not any(_expression_presente(m, clause) for m in regle.get("mots_cicd", [])):
            continue
        if any(_expression_presente(m, clause) for m in regle.get("marqueurs_optionnels", [])):
            continue
        if any(_expression_presente(m, clause) for m in regle.get("marqueurs_obligatoires", [])):
            exigence_detectee = True
            break

    if not exigence_detectee:
        return 0, ""

    titre_infra = any(_expression_presente(m, job.title) for m in regle.get("mots_titre_infra", []))
    cle = "penalite_infrastructure_points" if titre_infra else "penalite_points"
    penalite = max(0, int(regle.get(cle, 5 if titre_infra else 10)))
    return penalite, ("DevOps infrastructure avec exigence CI/CD" if titre_infra
                      else "DevOps avec compétence CI/CD exigée")


# On associe C1/B2 à la langue ALLEMANDE, jamais à English/French.
# Des niveaux isolés (« mindestens C1 ») sont volontairement insuffisants
# pour éliminer : ils pourraient qualifier une langue différente.
_RE_DEUTSCH = re.compile(
    r"\b(?:deutsch(?:e|en|er|es|em)?(?:\s*(?:sprach)?kenntnisse)?|german(?:\s+language)?)\b",
    re.IGNORECASE,
)
_RE_AUTRE_LANGUE = re.compile(
    r"\b(?:englisch|english|französisch|franzoesisch|french|spanisch|spanish)\b",
    re.IGNORECASE,
)
_RE_NIVEAU_FORT = re.compile(r"(?<!\w)(?:C1|C2)(?!\w)", re.IGNORECASE)
_RE_NIVEAU_B2 = re.compile(r"(?<!\w)(?:B2|B1|A2)(?!\w)", re.IGNORECASE)
_RE_MUTTERSPRACHE = re.compile(r"\b(?:muttersprach\w*|native(?:[- ]?level)?|mother tongue)\b", re.IGNORECASE)
_RE_VERHANDLUNGSSICHER = re.compile(r"\bverhandlungssicher\w*\b", re.IGNORECASE)


def _niveaux_allemands(texte: str) -> tuple[list[str], list[str], bool]:
    """Associe chaque niveau à la mention de langue la plus proche.

    "Englisch C1 und Deutsch B2" ne doit JAMAIS entraîner un rejet allemand.
    "Deutsch C1 und Englisch B2" doit être rejeté.
    """
    t = re.sub(r"</(?:li|p|div|h[1-6])>|<br\s*/?>", "\n", texte or "", flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    clauses = re.split(r"[\n\r;,.]+", t)
    strict: list[str] = []
    souple: list[str] = []
    b2 = False
    for clause in clauses:
        mentions = [(m.start(), m.end(), "de") for m in _RE_DEUTSCH.finditer(clause)]
        mentions.extend((m.start(), m.end(), "autre") for m in _RE_AUTRE_LANGUE.finditer(clause))
        if not mentions:
            continue
        for rx, etiquette in [
            (_RE_NIVEAU_FORT, "fort"),
            (_RE_MUTTERSPRACHE, "natif"),
            (_RE_VERHANDLUNGSSICHER, "souple"),
            (_RE_NIVEAU_B2, "b2"),
        ]:
            for m in rx.finditer(clause):
                # Distance dans le texte entre deux spans, et non entre leurs centres.
                nearest = min(mentions, key=lambda x: max(x[0] - m.end(), m.start() - x[1], 0))
                distance = max(nearest[0] - m.end(), m.start() - nearest[1], 0)
                if nearest[2] != "de" or distance > 65:
                    continue
                if etiquette == "fort" and "allemand C1/C2" not in strict:
                    strict.append("allemand C1/C2")
                elif etiquette == "natif" and "allemand niveau natif" not in strict:
                    strict.append("allemand niveau natif")
                elif etiquette == "souple" and "allemand verhandlungssicher" not in souple:
                    souple.append("allemand verhandlungssicher")
                elif etiquette == "b2":
                    b2 = True
    return strict, souple, b2


def _adequation_titre(job: JobResult, crit: dict[str, Any], points_techniques: int) -> tuple[int, bool]:
    regle = (crit.get("adequation_metier", {}) or {}).get("titres_prioritaires", {}) or {}
    if not regle.get("actif", False):
        return 0, False
    titre = job.title
    fortes = regle.get("mots_forts", [])
    ambigus = regle.get("mots_avec_competence", [])
    if any(_expression_presente(mot, titre) for mot in fortes):
        return max(0, int(regle.get("bonus_fort", 5))), True

    # Virtualisation, Citrix et stockage/sauvegarde : des métiers explicitement
    # adaptés qui peuvent être peu détaillés dans les annonces de la BA.
    # Ne pas accorder ce bonus à un simple terme générique dans un domaine non IT.
    specialites = regle.get("specialites_infrastructure", {}) or {}
    if specialites.get("actif", False):
        directs = specialites.get("titres_explicites", []) or []
        contextuels = specialites.get("titres_a_confirmer", []) or []
        if any(_expression_presente(mot, titre) for mot in directs):
            return max(0, int(specialites.get("bonus", 5))), True
        if any(_expression_presente(mot, titre) for mot in contextuels):
            preuve_titre = any(_expression_presente(mot, titre)
                               for mot in specialites.get("indices_titre_it", []))
            preuve_description = points_techniques >= int(specialites.get("min_points_tech", 3))
            if preuve_titre or preuve_description:
                return max(0, int(specialites.get("bonus", 5))), True

    # IT-Anwendungsbetreuer : le titre est IT, mais les missions peuvent être
    # purement fonctionnelles. Accorder le bonus seulement si le contexte
    # confirme des systèmes / applications / tâches techniques.
    applications = regle.get("anwendungsbetreuung", {}) or {}
    if applications.get("actif", False) and any(
        _expression_presente(mot, titre)
        for mot in applications.get("titres", [])
    ):
        confirme_titre = any(_expression_presente(mot, titre)
                            for mot in applications.get("indices_titre", []))
        confirme_description = any(_expression_presente(mot, job.description)
                                  for mot in applications.get("indices_description", []))
        if confirme_titre or confirme_description or points_techniques >= int(
                applications.get("min_points_tech", 4)):
            return max(0, int(applications.get("bonus", 5))), True

    # Un « System Engineer » hors informatique avec zéro compétence IT ne
    # doit pas gagner artificiellement la priorité métier.
    if points_techniques >= 4 and any(_expression_presente(mot, titre) for mot in ambigus):
        return max(0, int(regle.get("bonus_ambigu", 3))), True
    return 0, False


# --------------------------------------------------------------------------
# Métier : priorité indépendante des points, et validation des offres en français
# --------------------------------------------------------------------------

def _priorite_metier(job: JobResult, crit: dict[str, Any]) -> str:
    """Classe le métier, pas les technologies listées dans la description.

    Une annonce Python Developer peut avoir 15 points grâce à Linux/Python,
    sans devenir un poste d'administration système.
    """
    regles = crit.get("priorites_metier", {}) or {}
    titre = job.title
    if any(_expression_presente(m, titre) for m in regles.get("tres_adapte", [])):
        return "Très adapté"
    if any(_expression_presente(m, titre) for m in regles.get("adapte", [])):
        return "Adapté"
    return "À examiner"


def _metier_francais_pertinent(job: JobResult, crit: dict[str, Any]) -> bool:
    """Le français ne doit PAS transformer un poste SAP fonctionnel en poste IT.

    Les rôles de support/terrain sont conservés ; les rôles génériques (Field
    Engineer, System Engineer...) doivent présenter un contexte IT explicite.
    Une mention '1st Level Support' dans la description d'un consultant SAP
    ne suffit pas à valider son INTITULÉ de consultant fonctionnel.
    """
    t = job.title
    if re.search(r"\b(?:sap|s/4\s*hana|s/4)\b", t, re.I) and re.search(
        r"\b(?:consultant|berater|beratung|fi|co|service)\b", t, re.I
    ) and not re.search(r"\b(?:basis[- ]?administrator|systemadministrator)\b", t, re.I):
        return False
    # Les titres purement génériques doivent également être confirmés par
    # leur description, même s'ils entrent dans une famille « adaptée ».
    titre_ambigu = ("field engineer", "system engineer", "service engineer", "technical engineer",
                    "it consultant", "it-berater", "cloud engineer", "devops engineer",
                    "support engineer", "technical consultant")
    est_ambigu = any(_expression_presente(m, t) for m in titre_ambigu)
    est_explicitement_it = any(_expression_presente(m, t) for m in
                               ("IT System Engineer", "IT-System Engineer", "IT Field Engineer",
                                "IT Support Engineer"))
    if est_ambigu and not est_explicitement_it:
        indices = ("linux", "windows server", "active directory", "server hardware",
                   "it infrastructure", "it-infrastruktur", "rechenzentrum",
                   "datacenter", "vmware", "network infrastructure", "it services",
                   "microsoft 365", "entra id", "intune", "azure", "linux server",
                   "kubernetes", "it betrieb", "it-betrieb", "exchange online")
        texte = f"{job.employer} {job.description}"
        return any(_expression_presente(m, texte) for m in indices)
    if job.priorite_metier in ("Très adapté", "Adapté"):
        return True
    # Rôle très éloigné : programmer, SAP Functional Consultant, vente...
    return False


# --------------------------------------------------------------------------
# Déduplication prudente entre PLUSIEURS références BA d'une même annonce
# --------------------------------------------------------------------------

def _ascii_texte(v: str) -> str:
    v = unicodedata.normalize("NFKD", (v or "").casefold())
    v = "".join(c for c in v if not unicodedata.combining(c))
    return v.replace("ß", "ss")


def _norm_entreprise_doublon(nom: str) -> str:
    n = _ascii_texte(nom)
    n = re.sub(r"\b(?:gmbh|ag|kg|se|mbh|ug|co|gbr|e\s*v)\b", " ", n)
    return " ".join(re.findall(r"[a-z0-9]+", n))


def _norm_ville_doublon(ville: str) -> str:
    v = _ascii_texte(ville)
    v = re.sub(r"\([^)]*\)", " ", v)
    v = " ".join(re.findall(r"[a-z0-9]+", v))
    return {"munich": "munchen", "muenchen": "munchen", "munchen": "munchen",
            "bremen city": "bremen", "oldenburg oldb": "oldenburg"}.get(v, v)


def _norm_titre_doublon(titre: str, ville: str = "") -> str:
    t = _ascii_texte(titre)
    t = re.sub(r"\(\s*(?:m\s*/\s*w\s*/\s*d|w\s*/\s*m\s*/\s*d|m\s*/\s*f\s*/\s*d|m\s*/\s*w)\s*\)", " ", t)
    t = re.sub(r"\b(?:m\s*/\s*w\s*/\s*d|w\s*/\s*m\s*/\s*d)\b", " ", t)
    t = " ".join(re.findall(r"[a-z0-9]+", t))
    # La ville est parfois ajoutée au titre, ex. « ..., Bochum ».
    v = _norm_ville_doublon(ville)
    if v and t.endswith(" " + v):
        t = t[:-(len(v) + 1)]
    t = re.sub(r"\bfur\b", " ", t)
    return " ".join(t.split())


def _description_tokens(description: str) -> set[str]:
    t = re.sub(r"<[^>]*>", " ", description or "")
    return {mot for mot in re.findall(r"[a-z0-9]{4,}", _ascii_texte(t))
            if mot not in {"sowie", "einer", "einem", "einen", "unsere", "werden", "wurde", "durch", "the", "with"}}


def _hash_description(job: JobResult) -> str:
    mots = sorted(_description_tokens(job.description))
    return hashlib.sha256(" ".join(mots).encode("utf-8")).hexdigest()


def _date_proche(a: str, b: str, jours: int = 14) -> bool:
    try:
        return abs((datetime.fromisoformat(a[:10]) - datetime.fromisoformat(b[:10])).days) <= jours
    except (ValueError, TypeError):
        return False


def _sont_memes_annonces(a: JobResult, b: JobResult) -> bool:
    if a.refnr == b.refnr:
        return True
    if not a.employer or not b.employer or _norm_entreprise_doublon(a.employer) != _norm_entreprise_doublon(b.employer):
        return False
    if not a.ort or not b.ort or _norm_ville_doublon(a.ort) != _norm_ville_doublon(b.ort):
        return False
    t1, t2 = _norm_titre_doublon(a.title, a.ort), _norm_titre_doublon(b.title, b.ort)
    if min(len(t1), len(t2)) < 10 or SequenceMatcher(None, t1, t2).ratio() < .89:
        return False
    if not _date_proche(a.published, b.published):
        return False
    # Deux postes distincts peuvent partager un titre. Exiger aussi un
    # recoupement fort de leurs descriptions évite de les fusionner par erreur.
    mots_a, mots_b = _description_tokens(a.description), _description_tokens(b.description)
    if len(mots_a) < 8 or len(mots_b) < 8:
        return False
    return len(mots_a & mots_b) / len(mots_a | mots_b) >= .64


def _dedupliquer_qualifies(jobs: list[JobResult]) -> tuple[list[JobResult], list[JobResult]]:
    uniques: list[JobResult] = []
    doublons: list[JobResult] = []
    for j in sorted(jobs, key=lambda x: (x.score, x.refnr), reverse=True):
        if any(_sont_memes_annonces(j, precedent) for precedent in uniques):
            doublons.append(j)
        else:
            uniques.append(j)
    return uniques, doublons


def _empreinte_notification(job: JobResult) -> dict[str, str]:
    return {"employeur": _norm_entreprise_doublon(job.employer),
            "ville": _norm_ville_doublon(job.ort),
            "titre": _norm_titre_doublon(job.title, job.ort),
            "description_sha256": _hash_description(job),
            "date": job.published[:10],
            "reference": job.refnr}


def _deja_notifie_autre_reference(job: JobResult, anciennes: list[dict[str, str]]) -> bool:
    """Historique nouveau format : ne se fonde jamais sur les simples 'vus',
    qui contiennent aussi des offres rejetées et non notifiées.
    """
    now = _empreinte_notification(job)
    return any(old.get("reference") != job.refnr and
               old.get("employeur") == now["employeur"] and
               old.get("ville") == now["ville"] and
               old.get("titre") == now["titre"] and
               old.get("description_sha256") == now["description_sha256"] and
               _date_proche(old.get("date", ""), now["date"], 21)
               for old in anciennes)


def _lieu_francais_allemagne(job: JobResult) -> bool:
    """Pas de dérogation nationale pour un poste en Suisse/Autriche.

    La présence d'un code postal allemand ou d'un Land allemand identifiable
    suffit. Sans indication fiable, le lieu est 'à vérifier' et non notifié.
    """
    regions = {"BADEN_WUERTTEMBERG", "BAYERN", "BERLIN", "BRANDENBURG", "BREMEN",
               "HAMBURG", "HESSEN", "MECKLENBURG_VORPOMMERN", "NIEDERSACHSEN",
               "NORDRHEIN_WESTFALEN", "RHEINLAND_PFALZ", "SAARLAND", "SACHSEN",
               "SACHSEN_ANHALT", "SCHLESWIG_HOLSTEIN", "THUERINGEN"}
    lieux = job.localisations or [{"adresse": {"plz": job.plz, "ort": job.ort, "region": job.region}}]
    for loc in lieux:
        a = (loc or {}).get("adresse") or {}
        plz = str(a.get("plz") or job.plz or "").strip()
        region = str(a.get("region") or job.region or "").upper().replace("-", "_").replace(" ", "_")
        ville = str(a.get("ort") or job.ort or "")
        pays = str(a.get("land") or a.get("staat") or a.get("country") or "").lower()
        if pays and any(s in pays for s in ("schweiz", "switzerland", "suisse", "austria", "osterreich", "österreich")):
            continue
        if pays and any(s in pays for s in ("deutschland", "germany", "allemagne")):
            return True
        if re.fullmatch(r"\d{5}", plz) or region in regions:
            return True
        if not plz and _norm_ville_doublon(ville) in {
            "berlin", "hamburg", "bremen", "oldenburg", "hannover", "munchen",
            "koln", "frankfurt am main", "stuttgart", "dresden", "leipzig",
            "dusseldorf", "dortmund", "essen", "bonn", "wetzlar"}:
            return True
    return False


def evaluate(job: JobResult, cfg: dict[str, Any]) -> None:
    """Score technique + bonus métier, pénalité DevOps ; allemand uniquement."""
    crit = cfg["criteres"]
    haystack = f"{job.title}\n{job.description}"
    francais = crit.get("francais", {}) or {}
    mots_fr = [m for m in francais.get("mots_detection", []) if _expression_presente(m, haystack)]
    job.francais = bool(mots_fr)

    matched_stricte, matched_souple, allemand_b2 = _niveaux_allemands(haystack)
    job.matched_elimination = matched_stricte + ([] if allemand_b2 else matched_souple)

    # Les titres d'architectes spécialisés ne sont pas adaptés au profil,
    # même lorsque leurs descriptions énumèrent des technologies familières.
    if re.search(r"(?<!\w)(?:architect|architekt)(?:e|en|in|innen)?(?!\w)", job.title, re.I):
        job.matched_elimination.append("poste d'architecte (titre hors cible)")

    score = 0
    teletravail = statut_teletravail(haystack)
    for groupe in crit.get("points_positifs", []) or []:
        nom = groupe.get("nom", "?")
        poids = int(groupe.get("poids", 0))
        if nom == "Homeoffice / Remote / Hybrid":
            synonymes_trouves = [teletravail] if teletravail != "aucun" else []
        else:
            synonymes_trouves = [m for m in (groupe.get("mots", []) or []) if _expression_presente(m, haystack)]
        if synonymes_trouves:
            score += poids
            job.matched_positive.append(f"{nom} ({'/'.join(synonymes_trouves)})")

    job.score_brut = score
    poids_teletravail = next((int(g.get("poids", 0)) for g in crit.get("points_positifs", [])
                             if g.get("nom") == "Homeoffice / Remote / Hybrid"), 0)
    bonus, cible = _adequation_titre(
        job, crit, score - (poids_teletravail if teletravail != "aucun" else 0)
    )
    job.bonus_metier = bonus
    job.metier_cible = cible
    if bonus:
        job.motifs_adequation.append(f"titre de métier ciblé (+{bonus})")
    penalite, motif = _penalite_devops_cicd(job, crit)
    job.penalite_metier = penalite
    if motif and penalite:
        job.motifs_adequation.append(motif)
    job.score = max(0, score + bonus - penalite)
    job.priorite_metier = _priorite_metier(job, crit)
    if job.francais:
        job.matched_positive.insert(0, f"Français ({'/'.join(mots_fr[:3])})")

    if job.francais and not _metier_francais_pertinent(job, crit):
        job.matched_elimination.append("français présent, mais métier hors IT ciblée")
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
        f"  Priorité métier : {j.priorite_metier}",
        f"  Score : {j.score} points (technique {j.score_brut}, "
        f"bonus métier +{j.bonus_metier}, pénalité -{j.penalite_metier})" +
        f" | Compétences : {', '.join(j.matched_positive) or '-'}",
        f"  Publié le : {j.published} | Télétravail : {j.statut_teletravail}",
        f"  Lien : {j.url}",
        "",
    ]


def build_email_body(jobs: list[JobResult]) -> str:
    rang = {"Très adapté": 0, "Adapté": 1, "À examiner": 2}
    critere = lambda j: (rang.get(j.priorite_metier, 2), -j.score, j.title)
    fr = sorted([j for j in jobs if j.francais], key=critere)
    autres = sorted([j for j in jobs if not j.francais], key=critere)
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
    p.add_argument("--review-seen", action="store_true", help="Réévalue une sélection d'offres vues sans notification ni écriture.")
    p.add_argument("--review-limit", type=int, default=150, help="Nombre maximal d'offres déjà vues à réexaminer (défaut 150).")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    if args.review_seen and not args.dry_run:
        raise SystemExit("--review-seen exige --dry-run : aucun ancien e-mail ne sera renvoyé.")
    if args.review_limit < 1:
        raise SystemExit("--review-limit doit être >= 1.")
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
    if args.review_seen:
        # L'ancien historique n'indique pas si une offre a été NOTIFIÉE ou
        # REJETÉE. Toujours aperçu uniquement, jamais de réenvoi automatique.
        candidates = [o for o in raw_offers if o.get("referenznummer") in state["vus"]]
        candidates.sort(key=lambda o: str(state["vus"].get(o.get("referenznummer"), "")), reverse=True)
        new_offers = candidates[:args.review_limit]
        logger.info("Réexamen sans notification : %d offres de l'archive parmi %d encore visibles dans l'API.", len(new_offers), len(candidates))
    else:
        new_offers = [o for o in raw_offers if o.get("referenznummer") not in state["vus"]]
        logger.info("Nouvelles offres depuis le dernier passage : %d", len(new_offers))

    results: list[JobResult] = []
    for o in new_offers:
        lokalisations = o.get("stellenlokationen") or [{}]
        loc0 = lokalisations[0] or {}
        adresse = loc0.get("adresse") or {}
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
            localisations=lokalisations,
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

        if not args.review_seen:
            state["vus"][job.refnr] = datetime.now(timezone.utc).isoformat()
        time.sleep(REQUEST_DELAY_S)

    seuil = int(cfg["criteres"].get("seuil_points", 10))
    # Le français reste une exception au seuil, seulement pour un métier IT pertinent et un lieu allemand.
    seuil_cible = int(cfg["criteres"].get("seuil_points_cibles", 5))
    qualifies = [j for j in results if not j.eliminated and (
        j.score >= seuil or j.francais or (
            j.metier_cible and not j.penalite_metier and j.score >= seuil_cible
        )
    )]
    # Deux références BA peuvent désigner une seule annonce. Ne fusionner que
    # titre + entreprise + ville + date + description fortement compatibles.
    qualifies, doublons_run = _dedupliquer_qualifies(qualifies)
    if doublons_run:
        logger.info("Doublons même annonce / autre référence BA (run courant) : %d", len(doublons_run))
    deja_notifies = state.get("notifications", [])
    doublons_historiques = [j for j in qualifies if _deja_notifie_autre_reference(j, deja_notifies)]
    if doublons_historiques:
        logger.info("Doublons d'annonces déjà notifiées (autre référence BA) : %d", len(doublons_historiques))
    qualifies = [j for j in qualifies if j not in doublons_historiques]
    eliminees = [j for j in results if j.eliminated]
    references_qualifiees = {j.refnr for j in qualifies}
    references_doublons = {j.refnr for j in doublons_run + doublons_historiques}
    sous_le_seuil = [j for j in results if j.refnr not in references_qualifiees
                     and j.refnr not in references_doublons and not j.eliminated]

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
        if j.motifs_adequation:
            logger.info("  ↳ Adéquation métier : %s (-%d points)", ", ".join(j.motifs_adequation), j.penalite_metier)
        logger.info("  ○ %s — %s (%d pts, sous le seuil de %d) [%s] %s", j.title, j.employer, j.score, seuil, j.source, j.url)

    if qualifies and not args.dry_run:
        if send_email(cfg, qualifies):
            # Ce registre contient seulement les offres réellement envoyées.
            # Il complète 'vus' sans changer son format ni rejouer l'historique.
            nouvelles = [_empreinte_notification(j) for j in qualifies]
            state.setdefault("notifications", []).extend(nouvelles)
            # Purge prudente des très anciens enregistrements (sans toucher vus).
            today = datetime.now(timezone.utc).date()
            state["notifications"] = [n for n in state["notifications"]
                if n.get("date") and _date_proche(n.get("date"), today.isoformat(), 60)]
        else:
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
        logger.info("Mode dry-run : seen_jobs.json laissé intact. %s",
                    "Le réexamen ne permet pas de distinguer les annonces auparavant envoyées." if args.review_seen else "")
    else:
        save_state(st_path, state)


if __name__ == "__main__":
    main()
