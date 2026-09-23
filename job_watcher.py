#!/usr/bin/env python3
"""
job_watcher.py
==============

Surveille la Jobbörse de la Bundesagentur für Arbeit via son API officielle
"Jobsuche" (https://jobsuche.api.bund.dev/, projet bundesAPI/jobsuche-api),
combine une recherche LOCALE (rayon autour d'Oldenburg) et une recherche
REMOTE NATIONALE (sans limite de distance, filtrée télétravail), évalue
chaque NOUVELLE offre selon les critères d'élimination et de points définis
dans criteres.json, vérifie si l'employeur figure déjà dans le suivi de
candidatures (Google Sheet), puis envoie un e-mail récapitulatif pour les
offres qui passent le seuil.

Fichiers utilisés
------------------
- config.yaml    : configuration TECHNIQUE (chemins, e-mail, seuil)
- criteres.json  : configuration MÉTIER (mots-clés, élimination, points)
- seen_jobs.json : historique des offres déjà notifiées (créé/mis à jour
                   automatiquement, ne pas éditer à la main)

Utilisation
-----------
    python job_watcher.py --config config.yaml
    python job_watcher.py --config config.yaml --dry-run   # pas d'e-mail, résumé console
    python job_watcher.py --config config.yaml --reset     # vide l'historique des offres vues

Conçu pour tourner via un déclenchement externe (webhook crontab.org vers
un GitHub Actions "repository_dispatch" — voir README.md et
.github/workflows/job_watcher.yml), pas via le planificateur natif
GitHub Actions.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import logging
import os
import re
import smtplib
import sys
import time
from urllib.parse import quote as url_quote
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests
import yaml

# --------------------------------------------------------------------------
# Constantes API Jobsuche (Bundesagentur für Arbeit)
# --------------------------------------------------------------------------

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"

# NOTE HISTORIQUE (23.09.2026) : l'API est passée de /pc/v4/jobs à /pc/v6/jobs
# entre la rédaction de la doc initialement consultée et le premier vrai test
# du script. L'ancien endpoint /pc/v4/jobs renvoyait 403 "No match found for
# request for url" — pas un problème d'identifiants, juste une route qui
# n'existe plus côté passerelle API. Référence à jour :
# https://github.com/bundesAPI/jobsuche-api (README + openapi.yaml)
SEARCH_ENDPOINT = f"{API_BASE}/pc/v6/jobs"

# Le endpoint de détail attend le refnr encodé en base64 dans le chemin
# (PAS le hashId renvoyé par la recherche, qui sert seulement au logo
# employeur). Exemple documenté : base64("10001-1002716922-S") = "MTAwMDEt...".
DETAILS_ENDPOINT = f"{API_BASE}/pc/v4/jobdetails/{{encoded_refnr}}"

JOB_URL_TEMPLATE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"

# Client ID publique documentée par le projet open-source bundesAPI/jobsuche-api.
# Elle a aussi changé en même temps que l'endpoint : ce n'est plus un GUID,
# mais cette chaîne littérale, à passer en header "X-API-Key".
API_CLIENT_ID = "jobboerse-jobsuche"
HEADERS = {"X-API-Key": API_CLIENT_ID, "User-Agent": "job-watcher/2.1"}

PAGE_SIZE = 100
REQUEST_TIMEOUT = 30
REQUEST_DELAY_S = 0.4  # petite pause entre appels pour rester correct avec l'API

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
    source: str  # "locale", "remote_ho" ou "remote_texte" — d'où vient cette offre
    description: str = ""
    score: int = 0
    matched_positive: list[str] = field(default_factory=list)
    matched_elimination: list[str] = field(default_factory=list)
    deja_postule: bool = False  # True si l'employeur figure dans le Google Sheet

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
    """Charge config.yaml (technique) puis fusionne le contenu de
    criteres.json (métier, pointé par criteres.fichier) sous cfg['criteres']."""
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

    # Fusion : on garde seuil_points/fichier déjà présents dans cfg['criteres'],
    # et on y ajoute recherche/elimination/points_positifs venant du JSON.
    cfg["criteres"]["recherche"] = criteres_data.get("recherche", {})
    cfg["criteres"]["elimination"] = criteres_data.get("elimination", {}).get("mots", [])
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
# Recherche (locale + remote nationale, fusionnées)
# --------------------------------------------------------------------------

def _run_search(
    session: requests.Session,
    query: str,
    extra_params: dict[str, Any],
    common_params: dict[str, Any],
    source_label: str,
    fusion: dict[str, dict[str, Any]],
) -> None:
    """Exécute une recherche paginée pour un mot-clé donné et ajoute les
    résultats dans 'fusion' (dédoublonnage par refnr, la première recherche
    qui trouve une offre donnée en garde l'étiquette 'source')."""
    page = 1
    while True:
        params = {
            "was": query,
            "size": PAGE_SIZE,
            "page": page,
            **common_params,
            **extra_params,
        }
        params = {k: v for k, v in params.items() if v not in (None, "")}
        # IMPORTANT : la bibliothèque requests sérialise un booléen Python
        # True/False en "True"/"False" (majuscule) dans l'URL, ce que l'API
        # Arbeitsagentur ne reconnaît pas forcément comme un booléen valide.
        # On force donc "true"/"false" en minuscules, format attendu par
        # la plupart des API REST (dont celle-ci).
        params = {
            k: ("true" if v is True else "false" if v is False else v)
            for k, v in params.items()
        }

        logger.info("[%s] Recherche %r (page %s)...", source_label, query, page)
        try:
            r = session.get(SEARCH_ENDPOINT, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            # Log de l'URL réellement appelée (avec tous les paramètres encodés) —
            # utile pour copier-coller et tester manuellement (navigateur/curl)
            # si jamais un résultat inattendu (ex. 0 offre partout) se reproduit.
            logger.info("[%s] URL appelée : %s", source_label, r.url)
            r.raise_for_status()
        except requests.RequestException as e:
            logger.error("[%s] Échec de la recherche pour %r : %s", source_label, query, e)
            return

        data = r.json()
        # NOTE (23.09.2026) : la réponse réelle de l'API en production diffère
        # du schéma openapi.yaml documenté (qui semble périmé). Constaté par
        # diagnostic direct : la liste des offres est sous la clé
        # "ergebnisliste" (pas "stellenangebote"), et l'identifiant unique
        # d'une offre est "referenznummer" (pas "refnr"). Voir la fonction
        # main() pour le mapping complet des autres champs (titre, employeur,
        # lieu, date) qui ont eux aussi changé de nom.
        offers = data.get("ergebnisliste", []) or []
        for o in offers:
            refnr = o.get("referenznummer")
            if refnr and refnr not in fusion:
                o["_source"] = source_label
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
    """Lance la recherche LOCALE (rayon autour d'un lieu) puis, si activée,
    la recherche REMOTE NATIONALE (sans limite de distance, arbeitszeit=ho),
    et fusionne le tout (dédoublonné par refnr)."""
    recherche = cfg["criteres"]["recherche"]
    commun = recherche.get("parametres_communs", {})
    common_params = {
        "berufsfeld": commun.get("domaine"),
        "veroeffentlichtseit": commun.get("jours", 7),
        "angebotsart": commun.get("type_offre", 1),
        "zeitarbeit": commun.get("interim", True),
    }

    fusion: dict[str, dict[str, Any]] = {}

    # --- Recherche locale : filtrée par lieu + rayon ---
    locale = recherche.get("locale", {})
    locale_params = {
        "wo": locale.get("lieu"),
        "umkreis": locale.get("rayon_km"),
    }
    for query in locale.get("mots_cles", []):
        _run_search(session, query, locale_params, common_params, "locale", fusion)

    # --- Recherche remote nationale : deux volets complémentaires ---
    remote = recherche.get("remote_national", {})
    if remote.get("actif", False):
        # Volet 1 : mots-clés classiques + tag officiel arbeitszeit=ho
        avec_flag = remote.get("avec_flag_ho", {})
        for query in avec_flag.get("mots_cles", []):
            _run_search(session, query, {"arbeitszeit": "ho"}, common_params, "remote_ho", fusion)

        # Volet 2 : le mot "remote"/"homeoffice" est DANS le mot-clé recherché,
        # aucun filtre arbeitszeit — capte les annonces qui ne cochent pas le
        # tag officiel mais l'annoncent dans leur titre.
        texte_libre = remote.get("texte_libre", {})
        for query in texte_libre.get("mots_cles", []):
            _run_search(session, query, {}, common_params, "remote_texte", fusion)

    # Comptage par source à des fins de diagnostic — pour objectiver combien
    # d'offres viennent de chaque type de recherche plutôt que de deviner.
    compte_par_source: dict[str, int] = {}
    for o in fusion.values():
        src = o.get("_source", "?")
        compte_par_source[src] = compte_par_source.get(src, 0) + 1
    logger.info("Répartition par source : %s", compte_par_source)

    logger.info("Total offres uniques trouvées (toutes sources fusionnées) : %d", len(fusion))

    # ------------------------------------------------------------------
    # FILET DE SÉCURITÉ DIAGNOSTIC : si tout reste à zéro malgré des
    # recherches a priori larges, on fait un ultime appel minimaliste
    # (aucun filtre sauf "was") et on affiche la réponse brute dans les
    # logs. Ça permet de savoir directement, depuis les logs GitHub
    # Actions, si le problème vient d'un des paramètres qu'on envoie ou
    # d'autre chose (ex. l'API renvoie vraiment 0 résultat même nue).
    # ------------------------------------------------------------------
    if not fusion:
        nb_locale = len(recherche.get("locale", {}).get("mots_cles", []))
        nb_remote = (
            len(remote.get("avec_flag_ho", {}).get("mots_cles", []))
            + len(remote.get("texte_libre", {}).get("mots_cles", []))
        )
        logger.warning(
            "Aucune offre trouvée par AUCUNE des %d recherches — test diagnostic "
            "avec une requête minimale (was=Linux, sans autre filtre)...",
            nb_locale + nb_remote,
        )
        try:
            r = session.get(
                SEARCH_ENDPOINT,
                headers=HEADERS,
                params={"was": "Linux", "size": 5, "page": 1},
                timeout=REQUEST_TIMEOUT,
            )
            logger.warning("[diagnostic] URL appelée : %s", r.url)
            logger.warning("[diagnostic] Code HTTP : %s", r.status_code)
            diag_data = r.json()
            # On affiche les VRAIES clés du JSON (plus fiable qu'un extrait de
            # texte tronqué) : la clé de premier niveau contenant la liste des
            # offres, et les clés du premier élément de cette liste, pour
            # savoir précisément comment adapter le parsing si l'API a changé
            # ses noms de champs.
            logger.warning("[diagnostic] Clés de premier niveau : %s", list(diag_data.keys()))
            for key, value in diag_data.items():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    logger.warning(
                        "[diagnostic] '%s' est une liste de %d élément(s) — clés du premier élément : %s",
                        key, len(value), list(value[0].keys()),
                    )
                    logger.warning("[diagnostic] Contenu complet du premier élément : %s", json.dumps(value[0], ensure_ascii=False, indent=2))
        except requests.RequestException as e:
            logger.warning("[diagnostic] La requête minimale a aussi échoué : %s", e)
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("[diagnostic] Réponse reçue mais pas du JSON valide : %s | Contenu brut : %s", e, r.text[:1000])

    return list(fusion.values())


_description_field_warned = False  # évite de spammer les logs, un seul avertissement suffit


def fetch_description(session: requests.Session, refnr: str) -> str:
    """Récupère la description complète de l'offre à partir de son refnr
    (encodé en base64 standard dans le chemin, comme l'exige l'API — voir
    DETAILS_ENDPOINT). En cas d'échec (offre retirée entre-temps, refnr
    invalide, etc.), retourne une chaîne vide plutôt que de faire échouer
    tout le run.

    Le nom du champ contenant le texte a changé selon la version de l'API
    (ancien : "stellenbeschreibung", nouveau documenté : "stellenangebotsBeschreibung")
    — on essaie les deux. Si aucun des deux n'existe (l'API a peut-être encore
    changé ce nom, comme elle l'a fait pour la recherche), on log UNE FOIS les
    vraies clés disponibles pour faciliter un futur diagnostic, sans spammer
    les logs à chaque offre."""
    global _description_field_warned
    if not refnr:
        return ""
    encoded_refnr = url_quote(base64.b64encode(refnr.encode("utf-8")).decode("ascii"), safe="")
    try:
        r = session.get(
            DETAILS_ENDPOINT.format(encoded_refnr=encoded_refnr),
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        description = data.get("stellenangebotsBeschreibung") or data.get("stellenbeschreibung")
        if not description and not _description_field_warned:
            logger.warning(
                "Aucun champ de description reconnu dans la réponse jobdetails — "
                "clés disponibles : %s (le nom du champ a peut-être encore changé "
                "côté API, à vérifier si ce message revient souvent)",
                list(data.keys()),
            )
            _description_field_warned = True
        return description or ""
    except requests.RequestException as e:
        logger.warning("Détails indisponibles pour refnr=%s : %s", refnr, e)
        return ""


# --------------------------------------------------------------------------
# Évaluation (critères d'élimination / points positifs par groupes)
# --------------------------------------------------------------------------

def evaluate(job: JobResult, cfg: dict[str, Any]) -> None:
    """Applique les critères d'élimination puis calcule le score de l'offre.

    IMPORTANT sur les points positifs : chaque groupe de criteres.json peut
    contenir plusieurs synonymes de la même compétence (ex. "RedHat", "RHEL").
    Le poids du groupe est accordé UNE SEULE FOIS si au moins un synonyme est
    trouvé — même si plusieurs synonymes apparaissent. Ça évite de compter
    deux fois la même compétence juste parce qu'elle est nommée différemment
    à deux endroits de l'annonce.
    """
    crit = cfg["criteres"]
    haystack = f"{job.title}\n{job.description}".lower()

    for kw in crit.get("elimination", []) or []:
        if kw.lower() in haystack:
            job.matched_elimination.append(kw)

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

# Suffixes juridiques allemands courants, retirés pour une comparaison plus
# tolérante (ex. "Amaxo GmbH" doit matcher "amaxo" tout court).
_SUFFIXES_ENTREPRISE = re.compile(
    r"\b(gmbh|ag|kg|co\.?\s*kg|ug|mbh|se|e\.?v\.?|gbr)\b",
    re.IGNORECASE,
)


def _normaliser_nom_entreprise(nom: str) -> str:
    nom = _SUFFIXES_ENTREPRISE.sub("", nom)
    nom = re.sub(r"[^\w\s]", " ", nom, flags=re.UNICODE)  # ponctuation -> espace
    nom = re.sub(r"\s+", " ", nom).strip().lower()
    return nom


def load_recent_companies(cfg: dict[str, Any], session: requests.Session) -> list[str]:
    """Télécharge l'onglet Google Sheet configuré (export CSV public, pas
    d'authentification nécessaire puisque le fichier est en lecture libre),
    et retourne les N dernières entreprises de la colonne configurée
    (normalisées pour une comparaison tolérante)."""
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
    # Comparaison tolérante aux espaces superflus dans les en-têtes du Sheet
    # (ex. "Firma " avec une espace finale) — évite de dépendre d'un nom de
    # colonne EXACT côté Google Sheet, qui pourrait changer par erreur.
    entetes = reader.fieldnames or []
    colonne_reelle = next((h for h in entetes if h.strip() == colonne.strip()), None)
    if colonne_reelle is None:
        logger.warning(
            "Colonne %r introuvable dans le Google Sheet (colonnes trouvées : %s)",
            colonne, entetes,
        )
        return []

    noms = [row[colonne_reelle].strip() for row in reader if row.get(colonne_reelle, "").strip()]
    dernieres = noms[-n:] if n > 0 else noms
    logger.info("Suivi de candidatures : %d entreprise(s) récente(s) chargée(s).", len(dernieres))
    return [_normaliser_nom_entreprise(n) for n in dernieres]


def marquer_doublons(jobs: list[JobResult], entreprises_normalisees: list[str]) -> None:
    """Marque job.deja_postule=True si l'employeur figure (comparaison
    tolérante, sous-chaîne dans un sens ou dans l'autre) parmi les
    entreprises récentes du suivi de candidatures. N'élimine RIEN — c'est un
    simple avertissement affiché dans l'e-mail."""
    if not entreprises_normalisees:
        return
    for job in jobs:
        employeur_norm = _normaliser_nom_entreprise(job.employer)
        if not employeur_norm:
            continue
        for connue in entreprises_normalisees:
            if not connue:
                continue
            if employeur_norm in connue or connue in employeur_norm:
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
    """Envoie l'e-mail via Gmail SMTP. Les identifiants viennent EXCLUSIVEMENT
    des variables d'environnement GMAIL_ADDRESS / GMAIL_APP_PASSWORD /
    GMAIL_TO (jamais stockés en clair dans config.yaml)."""
    email_cfg = cfg["email"]
    host = email_cfg.get("smtp_host", "smtp.gmail.com")
    port = int(email_cfg.get("smtp_port", 587))

    address = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_raw = os.environ.get("GMAIL_TO")

    if not all([address, app_password, to_raw]):
        logger.error(
            "Variables d'environnement manquantes : GMAIL_ADDRESS / GMAIL_APP_PASSWORD / "
            "GMAIL_TO doivent être définies (voir .env.example / README.md). Aucun e-mail envoyé."
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
    p.add_argument("--config", type=Path, default=Path("config.yaml"), help="Chemin du fichier de config YAML")
    p.add_argument("--dry-run", action="store_true", help="N'envoie pas d'e-mail, affiche seulement le résumé")
    p.add_argument("--reset", action="store_true", help="Vide l'historique des offres déjà vues avant de lancer")
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
        # Mapping vers les vrais noms de champs de la réponse API en
        # production (voir la note dans _run_search ci-dessus). "stellenlokationen"
        # est une LISTE de lieux possibles pour l'offre — on prend le premier,
        # suffisant pour l'affichage dans l'e-mail.
        lokalisations = o.get("stellenlokationen") or [{}]
        adresse = (lokalisations[0] or {}).get("adresse", {}) if lokalisations else {}
        job = JobResult(
            refnr=o.get("referenznummer", ""),
            title=o.get("stellenangebotsTitel") or o.get("hauptberuf", ""),
            employer=o.get("firma", ""),
            ort=adresse.get("ort", ""),
            plz=str(adresse.get("plz", "")),
            region=adresse.get("region", ""),
            published=o.get("datumErsteVeroeffentlichung")
                      or (o.get("veroeffentlichungszeitraum") or {}).get("von", ""),
            source=o.get("_source", "?"),
        )
        job.description = fetch_description(session, job.refnr)
        evaluate(job, cfg)
        results.append(job)

        # marque comme vue dans tous les cas (même éliminée / sous le seuil)
        # pour ne jamais la ré-analyser lors des prochains passages
        state["vus"][job.refnr] = datetime.now(timezone.utc).isoformat()
        time.sleep(REQUEST_DELAY_S)

    seuil = int(cfg["criteres"].get("seuil_points", 10))
    qualifies = [j for j in results if not j.eliminated and j.score >= seuil]
    eliminees = [j for j in results if j.eliminated]

    # Vérification anti-doublon uniquement sur les offres qui seront notifiées
    # (inutile de charger le Google Sheet s'il n'y a rien à notifier)
    if qualifies:
        entreprises_recentes = load_recent_companies(cfg, session)
        marquer_doublons(qualifies, entreprises_recentes)

    logger.info(
        "Résumé : %d analysées | %d éliminées | %d qualifiées (seuil=%d pts)",
        len(results), len(eliminees), len(qualifies), seuil,
    )
    for j in qualifies:
        flag = " [DOUBLON POSSIBLE]" if j.deja_postule else ""
        logger.info("  ✔ %s — %s (%d pts)%s %s", j.title, j.employer, j.score, flag, j.url)
    for j in eliminees:
        logger.info("  ✘ %s — %s (élim.: %s)", j.title, j.employer, ", ".join(j.matched_elimination))

    if qualifies and not args.dry_run:
        send_email(cfg, qualifies)
    elif qualifies and args.dry_run:
        print("\n--- APERÇU (dry-run, aucun e-mail envoyé) ---\n")
        print(build_email_body(qualifies))

    save_state(st_path, state)


if __name__ == "__main__":
    main()
