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
    source: str = "?"  # "locale" / "remote" / "hors_zone", déterminé après evaluate()

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
            r = session.get(SEARCH_ENDPOINT, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            logger.info("URL appelée : %s", r.url)
            r.raise_for_status()
        except requests.RequestException as e:
            logger.error("Échec de la recherche pour %r : %s", query, e)
            return

        data = r.json()
        # NOTE (23-24.09.2026) : la réponse réelle en production diffère du
        # schéma openapi.yaml documenté (périmé). Clé de la liste d'offres :
        # "ergebnisliste" (pas "stellenangebote") ; identifiant d'offre :
        # "referenznummer" (pas "refnr"). Voir main() pour le mapping complet.
        offers = data.get("ergebnisliste", []) or []
        for o in offers:
            refnr = o.get("referenznummer")
            if refnr and refnr not in fusion:
                fusion[refnr] = o

        try:
            max_results = int(data.get("maxErgebnisse", 0))
        except (TypeError, ValueError):
            max_results = 0

        if not offers or page * PAGE_SIZE >= max_results:
            return
        page += 1
        time.sleep(REQUEST_DELAY_S)


def search_jobs(cfg: dict[str, Any], session: requests.Session) -> list[dict[str, Any]]:
    """Lance une recherche nationale (sans filtre géographique) pour chaque
    mot-clé de criteres.json > recherche.mots_cles, fusionnée et dédoublonnée."""
    recherche = cfg["criteres"]["recherche"]
    commun = recherche.get("parametres_communs", {})
    common_params = {
        "berufsfeld": commun.get("domaine"),
        "veroeffentlichtseit": commun.get("jours", 7),
        "angebotsart": commun.get("type_offre", 1),
        "zeitarbeit": commun.get("interim", True),
    }

    fusion: dict[str, dict[str, Any]] = {}
    for query in recherche.get("mots_cles", []):
        _run_search(session, query, common_params, fusion)

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
        r = session.get(
            DETAILS_ENDPOINT.format(encoded_refnr=encoded_refnr),
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        description = data.get("stellenangebotsBeschreibung") or data.get("stellenbeschreibung")
        if not description and not _description_field_warned:
            logger.warning(
                "Aucun champ de description reconnu dans jobdetails — clés : %s",
                list(data.keys()),
            )
            _description_field_warned = True
        return description or ""
    except requests.RequestException as e:
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

    matched_stricte = [kw for kw in crit.get("elimination_stricte", []) if kw.lower() in haystack]
    matched_souple = [kw for kw in crit.get("elimination_souple", []) if kw.lower() in haystack]
    b2_present = any(m.lower() in haystack for m in crit.get("b2_marqueurs", []))

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

def build_email_body(jobs: list[JobResult]) -> str:
    lines = [f"{len(jobs)} nouvelle(s) offre(s) correspondant à tes critères :", ""]
    for j in sorted(jobs, key=lambda x: x.score, reverse=True):
        avertissement = " ⚠️ eventuell bereits beworben" if j.deja_postule else ""
        lines.append(f"● {j.title} — {j.employer}{avertissement}")
        lines.append(f"  Lieu : {j.plz} {j.ort} ({j.region}) | Source : {j.source}")
        lines.append(f"  Score : {j.score} points | Compétences : {', '.join(j.matched_positive) or '-'}")
        lines.append(f"  Publié le : {j.published}")
        lines.append(f"  Lien : {j.url}")
        lines.append("")
    return "\n".join(lines)


def send_email(cfg: dict[str, Any], jobs: list[JobResult]) -> None:
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
        return

    recipients = [addr.strip() for addr in to_raw.split(",") if addr.strip()]
    subject = f"[Job Watcher] {len(jobs)} nouvelle(s) offre(s) IT à Oldenburg/Bremen"
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
    except Exception as e:
        logger.error("Échec de l'envoi de l'e-mail : %s", e)


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
        )
        job.description = fetch_description(session, job.refnr)
        evaluate(job, cfg)
        classify_source(job, cfg)  # doit venir APRÈS evaluate() (utilise matched_positive)
        results.append(job)

        state["vus"][job.refnr] = datetime.now(timezone.utc).isoformat()
        time.sleep(REQUEST_DELAY_S)

    seuil = int(cfg["criteres"].get("seuil_points", 10))
    qualifies = [j for j in results if not j.eliminated and j.score >= seuil]
    eliminees = [j for j in results if j.eliminated]
    sous_le_seuil = [j for j in results if not j.eliminated and j.score < seuil]

    if qualifies:
        entreprises_recentes = load_recent_companies(cfg, session)
        marquer_doublons(qualifies, entreprises_recentes)

    logger.info(
        "Résumé : %d analysées | %d éliminées | %d sous le seuil | %d qualifiées (seuil=%d pts)",
        len(results), len(eliminees), len(sous_le_seuil), len(qualifies), seuil,
    )
    for j in qualifies:
        flag = " [DOUBLON POSSIBLE]" if j.deja_postule else ""
        logger.info("  ✔ %s — %s (%d pts)%s [%s] %s", j.title, j.employer, j.score, flag, j.source, j.url)
    for j in eliminees:
        logger.info("  ✘ %s — %s (élim.: %s)", j.title, j.employer, ", ".join(j.matched_elimination))
    for j in sorted(sous_le_seuil, key=lambda x: x.score, reverse=True):
        logger.info("  ○ %s — %s (%d pts, sous le seuil de %d) [%s] %s", j.title, j.employer, j.score, seuil, j.source, j.url)

    if qualifies and not args.dry_run:
        send_email(cfg, qualifies)
    elif qualifies and args.dry_run:
        print("\n--- APERÇU (dry-run, aucun e-mail envoyé) ---\n")
        print(build_email_body(qualifies))

    save_state(st_path, state)


if __name__ == "__main__":
    main()
