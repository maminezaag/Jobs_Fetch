#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
job_watcher_bochra.py
=====================
Surveillance automatisée d'offres d'emploi pour Bochra (chimie / laboratoire).

PROFIL
  - Teilzeit ou Praktikum uniquement (pas de Vollzeit, pas d'Ausbildung).
  - Dans un rayon de 20 km autour d'Oldenburg (pas de télétravail).
  - Allemand : B1 aujourd'hui, B2 prévu en janvier 2027.

FONCTIONNEMENT (même logique que job_watcher.py, script séparé)
  1. Recherche sur l'API Jobsuche de l'Arbeitsagentur, un mot-clé à la fois.
  2. Pré-filtre : distance GPS, Ausbildung (titre), pertinence chimie/labo.
  3. Lecture du texte complet de chaque nouvelle annonce restante.
  4. Classification (Teilzeit / Praktikum), éliminations, score par groupes.
  5. E-mail Gmail avec les offres qualifiées (une seule fois par offre).
  6. État séparé (seen_jobs_bochra.json) : aucune dépendance à Google Sheet.

FICHIERS
  config_bochra.yaml    réglages techniques (API, rayon, seuils, e-mail)
  criteres_bochra.json  critères métier (mots-clés, éliminations, points)
  seen_jobs_bochra.json historique des offres déjà vues (réécrit par le script)

UTILISATION
  python job_watcher_bochra.py --config config_bochra.yaml
  python job_watcher_bochra.py --config config_bochra.yaml --dry-run --ignore-seen
      --dry-run      n'envoie aucun e-mail, n'écrit pas l'état, affiche le résultat
      --ignore-seen  ignore l'historique (re-traite toutes les offres de la fenêtre)
      --verbose      affiche la décision pour chaque offre

VARIABLES D'ENVIRONNEMENT (secrets GitHub, mêmes que job_watcher.py)
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GMAIL_TO (plusieurs destinataires : séparés par des virgules)

CODES DE SORTIE
  0 = ok | 1 = échec d'envoi e-mail | 2 = aucune recherche API n'a abouti
  3 = impossible de lire le détail des annonces (voir details_paths dans la config)
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import math
import os
import re
import smtplib
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

LOG = logging.getLogger("job_watcher_bochra")
TZ = ZoneInfo("Europe/Berlin")
LIEN_OFFRE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"


# =============================================================================
# 1. OUTILS DE TEXTE ET DE RECHERCHE DE TERMES
# =============================================================================

def norm(text) -> str:
    """Texte -> minuscules, sans HTML, espaces simplifiés (pour la comparaison)."""
    if text is None:
        return ""
    s = html.unescape(str(text))
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\u00ad", "").replace("\xa0", " ").replace("–", "-").replace("—", "-")
    s = s.replace("_", " ").lower()
    return re.sub(r"\s+", " ", s).strip()


def flatten_strings(obj, out=None) -> list:
    """Récupère TOUS les textes d'une réponse JSON, quelle que soit sa structure."""
    if out is None:
        out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            flatten_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            flatten_strings(v, out)
    return out


# Négations : "keine Berufserfahrung", "C1 nicht erforderlich", etc.
NEG_PRE = re.compile(r"\b(?:kein|keine|keinen|keiner|keinem|ohne|nicht)\b")
NEG_POST = re.compile(
    r"nicht\s+(?:zwingend\s+|unbedingt\s+)?(?:erforderlich|nötig|notwendig|vorausgesetzt|"
    r"verlangt|erwartet|möglich)|ist\s+kein|keine\s+voraussetzung"
)
SENT_SPLIT = re.compile(r"[.!?;:]")


class Terms:
    """Liste de termes (mots simples ou 're:' = expression régulière)."""

    def __init__(self, terms):
        self.patterns = []
        for t in terms or []:
            t = str(t)
            if t.startswith("re:"):
                pat = t[3:]
            else:
                # Terme ordinaire : début de mot (accepte les déclinaisons à la fin).
                pat = r"(?<![0-9a-zäöüß])" + re.escape(t.lower())
            try:
                self.patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error as exc:
                raise SystemExit(f"Motif invalide dans criteres_bochra.json : {t!r} ({exc})")

    def search(self, text: str) -> bool:
        return any(rx.search(text) for rx in self.patterns)

    def find(self, text: str, negation: bool = False, soft: "Terms | None" = None) -> list:
        """Retourne les extraits trouvés (au plus un par motif).

        negation=True : ignore une occurrence précédée/suivie d'une négation.
        soft : si un de ces termes est juste à côté, l'occurrence est ignorée.
        """
        hits = []
        for rx in self.patterns:
            for m in rx.finditer(text):
                if negation or soft is not None:
                    pre = SENT_SPLIT.split(text[max(0, m.start() - 60):m.start()])[-1][-35:]
                    post = SENT_SPLIT.split(text[m.end():m.end() + 60])[0][:50]
                    if negation and (NEG_PRE.search(pre) or NEG_POST.search(post)):
                        continue
                    if soft is not None and (soft.search(pre) or soft.search(post)):
                        continue
                hits.append(m.group(0).strip())
                break
        return hits


class Criteres:
    """Charge criteres_bochra.json et prépare les motifs."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.mots_cles = raw["recherche"]["mots_cles"]
        self.domaine = Terms(raw["domaine_requis"]["termes"])
        t = raw["types_offre"]
        self.praktikum_titre = Terms(t["praktikum_titre"])
        self.teilzeit = Terms(t["teilzeit"])
        e = raw["elimination"]
        self.ausbildung_titre = Terms(e["ausbildung_titre"]["termes"])
        self.statut_etudiant = Terms(e["statut_etudiant_ou_eleve"]["termes"])
        self.experience = Terms(e["experience"]["termes"])
        self.experience_types = set(e["experience"].get("types", []))
        self.experience_soft = Terms(e["experience"].get("ignorer_si_contexte", []))
        self.langue_stricte = Terms(e["langue_stricte"]["termes"])
        self.langue_souple = Terms(e["langue_souple"]["termes"])
        self.b2 = Terms(e["b2_accepte"]["termes"])
        self.alertes = [(a["nom"], Terms(a["termes"])) for a in raw.get("alertes", [])]
        self.points = []
        for g in raw["points"]:
            self.points.append({
                "nom": g["nom"],
                "points": int(g["points"]),
                "scope": g.get("scope", "all"),
                "types": set(g.get("types", [])),
                "b2": bool(g.get("utilise_detecteur_b2", False)),
                "terms": Terms(g.get("termes", [])),
            })


# =============================================================================
# 2. OUTILS GÉOGRAPHIQUES ET LECTURE DES CHAMPS DE L'API
# =============================================================================

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Distance à vol d'oiseau en km entre deux points GPS."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_coords(obj, out=None) -> list:
    """Cherche (lat, lon) n'importe où dans la structure (schéma non figé)."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        low = {str(k).lower(): v for k, v in obj.items()}
        lat = next((low[k] for k in ("lat", "latitude", "breitengrad") if k in low), None)
        lon = next((low[k] for k in ("lon", "lng", "longitude", "laengengrad", "längengrad")
                    if k in low), None)
        if lat is not None and lon is not None:
            try:
                la, lo = float(lat), float(lon)
                if 46.0 <= la <= 56.0 and 5.0 <= lo <= 16.0:  # plausible pour l'Allemagne
                    out.append((la, lo))
            except (TypeError, ValueError):
                pass
        for v in obj.values():
            find_coords(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_coords(v, out)
    return out


def find_places(obj, out=None) -> list:
    """Cherche les noms de lieux (clés 'ort'/'stadt') dans la structure."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in ("ort", "stadt", "city") and isinstance(v, str) and v.strip():
                if v.strip() not in out:
                    out.append(v.strip())
            else:
                find_places(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_places(v, out)
    return out


def item_refnr(item):
    return item.get("referenznummer") or item.get("refnr") or item.get("refNr")


def item_title(item) -> str:
    return (item.get("stellenangebotsTitel") or item.get("titel") or item.get("beruf")
            or item.get("hauptberuf") or "")


def item_beruf(item) -> str:
    return item.get("hauptberuf") or item.get("beruf") or ""


def item_firma(item) -> str:
    f = item.get("firma") or item.get("arbeitgeber") or ""
    if isinstance(f, dict):
        f = f.get("name") or ""
    return f if isinstance(f, str) else ""


def item_date(item) -> str:
    d = item.get("datumErsteVeroeffentlichung") or item.get("aktuelleVeroeffentlichungsdatum") or ""
    return str(d)[:10]


# =============================================================================
# 3. APPELS HTTP VERS L'API
# =============================================================================

def make_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-API-Key": cfg["api"]["api_key"],
        "Accept": "application/json",
        "User-Agent": "job-watcher-bochra/1.0",
    })
    return s


def clean_params(params: dict) -> dict:
    """Retire les valeurs vides et écrit les booléens en minuscules (exigence de l'API)."""
    out = {}
    for k, v in params.items():
        if v is None or v == "":
            continue
        if v is True:
            v = "true"
        elif v is False:
            v = "false"
        out[k] = v
    return out


def http_get_json(session, url, params, cfg) -> dict:
    """GET avec nouvelles tentatives. Retourne {data, status, error, url}."""
    essais = int(cfg["api"].get("essais_par_requete", 3))
    timeout = cfg["api"].get("timeout_s", 30)
    res = {"data": None, "status": None, "error": None, "url": url}
    for attempt in range(1, essais + 1):
        try:
            r = session.get(url, params=clean_params(params or {}), timeout=timeout)
            res["status"], res["url"] = r.status_code, r.url
            if r.status_code == 200:
                res["data"], res["error"] = r.json(), None
                return res
            res["error"] = r.text[:200].replace("\n", " ")
            if r.status_code in (429, 500, 502, 503, 504) and attempt < essais:
                time.sleep(2 * attempt)
                continue
            return res
        except (requests.RequestException, ValueError) as exc:
            res["error"] = str(exc)[:200]
            if attempt < essais:
                time.sleep(2 * attempt)
    return res


def search_all(session, cfg, crit) -> tuple:
    """Lance toutes les recherches. Retourne ({refnr: enregistrement}, stats)."""
    api, rech = cfg["api"], cfg["recherche"]
    url = api["base_url"].rstrip("/") + api["search_path"]
    size, max_pages = int(api["page_size"]), int(api["max_pages"])
    pause = float(api.get("delai_entre_requetes_s", 0.4))
    found: dict = {}
    stats = {"requetes": 0, "ok": 0}
    first_logged = False

    for entry in crit.mots_cles:
        was = entry["was"]
        for art in entry.get("angebotsarten", [1]):
            for page in range(1, max_pages + 1):
                params = {
                    "was": was, "page": page, "size": size,
                    "veroeffentlichtseit": rech["jours"], "angebotsart": art,
                }
                if rech.get("wo"):
                    params["wo"] = rech["wo"]
                    params["umkreis"] = rech.get("umkreis_api_km", 25)
                res = http_get_json(session, url, params, cfg)
                stats["requetes"] += 1
                if res["data"] is None:
                    LOG.warning("Recherche '%s' (type %s, page %s) échouée : HTTP %s %s",
                                was, art, page, res["status"], res["error"])
                    break
                stats["ok"] += 1
                if not first_logged:
                    LOG.info("Première requête réussie : %s", res["url"])
                    first_logged = True
                data = res["data"]
                items = data.get("ergebnisliste") or data.get("stellenangebote")
                if items is None:
                    LOG.warning("Réponse sans 'ergebnisliste' pour '%s' : clés reçues = %s",
                                was, sorted(data.keys()) if isinstance(data, dict) else type(data))
                    break
                for it in items:
                    refnr = item_refnr(it)
                    if not refnr:
                        continue
                    rec = found.setdefault(refnr, {"item": it, "arts": set(), "mots": set()})
                    rec["arts"].add(art)
                    rec["mots"].add(was)
                total = data.get("maxErgebnisse")
                time.sleep(pause)
                if len(items) < size:
                    break
                try:
                    if total is not None and page * size >= int(total):
                        break
                except (TypeError, ValueError):
                    pass
    return found, stats


def fetch_details(session, cfg, refnr) -> tuple:
    """Lit le détail d'une annonce. Retourne (donnees_ou_None, derniere_erreur)."""
    api = cfg["api"]
    base = api["base_url"].rstrip("/")
    paths = list(api["details_paths"])
    pref = cfg.get("_details_pref")
    if pref is not None and 0 <= pref < len(paths):
        order = [pref] + [i for i in range(len(paths)) if i != pref]
    else:
        order = list(range(len(paths)))
    b64 = base64.b64encode(str(refnr).encode("utf-8")).decode("ascii")
    last = ""
    for i in order:
        url = base + paths[i].format(b64=b64, refnr=refnr)
        res = http_get_json(session, url, {}, cfg)
        time.sleep(float(api.get("delai_entre_requetes_s", 0.4)))
        if res["data"] is not None:
            if cfg.get("_details_pref") != i:
                LOG.info("Endpoint de détail retenu : %s", paths[i])
                cfg["_details_pref"] = i
            return res["data"], ""
        last = f"HTTP {res['status']} {res['error']} ({paths[i]})"
    return None, last


# =============================================================================
# 4. ÉVALUATION D'UNE OFFRE
# =============================================================================

def prefilter(rec, cfg, crit):
    """Éliminations possibles AVANT de lire le détail (économise des appels)."""
    item, rech = rec["item"], cfg["recherche"]
    d = rec.get("distance")
    if d is None:
        if not rech.get("garder_si_pas_de_coordonnees", True):
            return "hors zone (pas de coordonnées GPS)"
    elif d > float(rech["rayon_max_km"]):
        return f"hors zone (> {rech['rayon_max_km']} km)"
    title_n = norm(item_title(item))
    if crit.ausbildung_titre.find(title_n):
        return "Ausbildung / duales Studium"
    if not crit.domaine.search(title_n + " " + norm(item_beruf(item))):
        return "hors domaine chimie/labo"
    return None


def evaluate(rec, details, crit, seuils) -> dict:
    """Évalue une offre avec son texte complet. Retourne un dictionnaire de décision."""
    item = rec["item"]
    title_n = norm(item_title(item))
    beruf_n = norm(item_beruf(item))
    full_n = norm(" ".join(flatten_strings(details))) if details else ""
    text = " ".join(x for x in (title_n, beruf_n, full_n) if x)
    out = {"statut": None, "raison": None, "type": None, "score": 0,
           "competences": [], "alertes": []}

    def eliminated(reason):
        out["statut"], out["raison"] = "eliminee", reason
        return out

    # 0. Ausbildung détectée dans les métadonnées de l'annonce
    if isinstance(details, dict):
        art = norm(details.get("angebotsart", ""))
        if art == "4" or "ausbildung" in art:
            return eliminated("Ausbildung / duales Studium")

    # 1. Type d'offre : Praktikum ou Teilzeit, sinon écartée
    if 34 in rec["arts"] or crit.praktikum_titre.find(title_n):
        out["type"] = "praktikum"
    elif crit.teilzeit.find(text, negation=True):
        out["type"] = "teilzeit"
    else:
        return eliminated("ni Teilzeit ni Praktikum (Vollzeit)")
    typ = out["type"]

    # 2. Statut étudiant / élève exigé (même "souhaité")
    hits = crit.statut_etudiant.find(text, negation=True)
    if hits:
        return eliminated(f"statut étudiant/élève mentionné ({hits[0]})")

    # 3. Expérience exigée (éliminatoire pour Teilzeit, signalée pour Praktikum)
    hits = crit.experience.find(text, negation=True, soft=crit.experience_soft)
    if hits:
        if typ in crit.experience_types:
            return eliminated(f"expérience exigée ({hits[0]})")
        out["alertes"].append(f"Expérience mentionnée ({hits[0]}) : non éliminatoire pour un stage")

    # 4. Langue : stricte = toujours éliminée ; souple = éliminée sauf si B1/B2 mentionné
    hits = crit.langue_stricte.find(text, negation=True)
    if hits:
        return eliminated(f"allemand trop exigeant ({hits[0]})")
    soft_hits = crit.langue_souple.find(text, negation=True)
    b2_hits = crit.b2.find(text)
    if soft_hits:
        if b2_hits:
            out["alertes"].append(
                f"Exigence d'allemand vague ({soft_hits[0]}) neutralisée : B1/B2 est mentionné")
        else:
            return eliminated(f"allemand exigé en termes vagues ({soft_hits[0]})")
    if b2_hits:
        out["alertes"].append(f"Niveau B1/B2 mentionné : « {b2_hits[0]} »")

    # 5. Alertes d'information
    for nom, terms in crit.alertes:
        if terms.find(text):
            out["alertes"].append(nom)

    # 6. Score par groupes (un seul comptage par groupe)
    for g in crit.points:
        if g["types"] and typ not in g["types"]:
            continue
        if g["b2"]:
            hit = b2_hits[:1]
        else:
            hit = g["terms"].find(title_n if g["scope"] == "title" else text)
        if hit:
            out["score"] += g["points"]
            out["competences"].append((g["nom"], g["points"], hit[0]))

    seuil = int(seuils.get(typ, 8))
    out["statut"] = "qualifiee" if out["score"] >= seuil else "sous_seuil"
    out["raison"] = f"score {out['score']} / seuil {seuil}"
    return out


# =============================================================================
# 5. E-MAIL
# =============================================================================

def build_email(offers, cfg) -> tuple:
    """Retourne (sujet, corps_html, corps_texte)."""
    prefix = (cfg["email"].get("sujet_prefixe") or "").strip()
    n = len(offers)
    date = datetime.now(TZ).strftime("%d.%m.%Y")
    subject = f"{prefix + ' ' if prefix else ''}{n} offre{'s' if n > 1 else ''} Teilzeit / Praktikum chimie ({date})"
    rayon = cfg["recherche"]["rayon_max_km"]

    html_parts = [
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222\">",
        f"<p>{n} nouvelle{'s' if n > 1 else ''} offre{'s' if n > 1 else ''} "
        f"(Teilzeit ou Praktikum, {rayon} km autour d'Oldenburg), triée{'s' if n > 1 else ''} par score :</p>",
    ]
    text_parts = [f"{n} nouvelle(s) offre(s) (Teilzeit ou Praktikum, {rayon} km autour d'Oldenburg) :", ""]

    for o in offers:
        d = f"{o['distance']:.0f} km" if o["distance"] is not None else "distance inconnue"
        lieu = ", ".join(o["lieux"]) if o["lieux"] else "lieu non précisé"
        type_fr = "Praktikum" if o["type"] == "praktikum" else "Teilzeit"
        comp = "; ".join(f"{nom} (+{pts})" for nom, pts, _ in o["competences"]) or "aucune"
        html_parts.append(
            "<div style=\"border:1px solid #ddd;border-radius:6px;padding:10px 14px;margin:12px 0\">"
            f"<div style=\"font-size:16px\"><a href=\"{html.escape(o['lien'])}\">{html.escape(o['titre'])}</a></div>"
            f"<div>{html.escape(o['firma'] or 'employeur non précisé')} — {html.escape(lieu)} — {d}</div>"
            f"<div>Type : <b>{type_fr}</b> | Score : <b>{o['score']}</b> | Publiée : {html.escape(o['date'])}</div>"
            f"<div>Points forts : {html.escape(comp)}</div>"
        )
        text_parts += [
            f"* {o['titre']}", f"  {o['firma'] or 'employeur non précisé'} — {lieu} — {d}",
            f"  Type : {type_fr} | Score : {o['score']} | Publiée : {o['date']}",
            f"  Points forts : {comp}",
        ]
        if o["alertes"]:
            html_parts.append("<ul style=\"margin:6px 0;color:#8a4b00\">"
                              + "".join(f"<li>{html.escape(a)}</li>" for a in o["alertes"]) + "</ul>")
            text_parts += [f"  ! {a}" for a in o["alertes"]]
        html_parts.append("</div>")
        text_parts += [f"  {o['lien']}", ""]

    html_parts.append("</body></html>")
    return subject, "\n".join(html_parts), "\n".join(text_parts)


def send_email(subject, html_body, text_body, cfg) -> None:
    """Envoi via Gmail (mot de passe d'application). Lève une exception en cas d'échec."""
    try:
        addr = os.environ["GMAIL_ADDRESS"].strip()
        pwd = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "").strip()
        dest = [x.strip() for x in os.environ["GMAIL_TO"].split(",") if x.strip()]
    except KeyError as exc:
        raise RuntimeError(f"Variable d'environnement manquante : {exc}") from exc
    if not dest:
        raise RuntimeError("GMAIL_TO est vide")
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, addr, ", ".join(dest)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL(cfg["email"]["smtp_host"], int(cfg["email"]["smtp_port"]), timeout=60) as smtp:
        smtp.login(addr, pwd)
        smtp.sendmail(addr, dest, msg.as_string())


# =============================================================================
# 6. ÉTAT (offres déjà vues)
# =============================================================================

def load_state(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("format inattendu")
    except FileNotFoundError:
        st = {}
    except (ValueError, OSError) as exc:
        LOG.warning("État illisible (%s) : on repart d'un état vide", exc)
        st = {}
    st.setdefault("seen", {})
    st.setdefault("echecs", {})
    st.setdefault("derniere_execution", None)
    return st


def save_state(path: Path, state: dict, conservation_jours: int) -> None:
    cutoff = (datetime.now(TZ) - timedelta(days=conservation_jours)).strftime("%Y-%m-%d")
    state["seen"] = {k: v for k, v in state["seen"].items() if str(v) >= cutoff}
    state["echecs"] = {k: v for k, v in state["echecs"].items() if k not in state["seen"]}
    state["derniere_execution"] = datetime.now(TZ).isoformat(timespec="seconds")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# =============================================================================
# 7. PROGRAMME PRINCIPAL
# =============================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Job watcher pour Bochra (chimie / labo)")
    ap.add_argument("--config", default="config_bochra.yaml")
    ap.add_argument("--dry-run", action="store_true", help="aucun e-mail, aucun enregistrement d'état")
    ap.add_argument("--ignore-seen", action="store_true", help="ignorer l'historique des offres vues")
    ap.add_argument("--verbose", action="store_true", help="afficher la décision pour chaque offre")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg_path = Path(args.config).resolve()
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    base_dir = cfg_path.parent
    with open(base_dir / cfg["fichiers"]["criteres"], "r", encoding="utf-8") as f:
        crit = Criteres(json.load(f))
    state_path = base_dir / cfg["fichiers"]["etat"]
    state = load_state(state_path)
    ref = cfg["recherche"]["point_reference"]
    seuils = cfg["scoring"]["seuil_points"]
    session = make_session(cfg)
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    # --- Recherche ----------------------------------------------------------
    found, stats = search_all(session, cfg, crit)
    LOG.info("Recherches : %d requêtes, %d réussies, %d offres uniques trouvées",
             stats["requetes"], stats["ok"], len(found))
    if stats["requetes"] > 0 and stats["ok"] == 0:
        LOG.error("Aucune recherche n'a abouti : vérifier search_path / api_key dans la config.")
        return 2

    new = {r: rec for r, rec in found.items() if args.ignore_seen or r not in state["seen"]}
    LOG.info("Nouvelles offres depuis le dernier passage : %d", len(new))

    # --- Traitement ---------------------------------------------------------
    reasons: Counter = Counter()
    qualified: list = []
    processed: set = set()
    details_ok = details_failed = 0
    last_detail_error = ""
    max_echecs = int(cfg["etat"].get("max_echecs_details", 3))

    for refnr, rec in new.items():
        item = rec["item"]
        coords = find_coords(item)
        rec["distance"] = (min(haversine_km(ref["lat"], ref["lon"], la, lo) for la, lo in coords)
                           if coords else None)

        reason = prefilter(rec, cfg, crit)
        if reason:
            reasons[reason] += 1
            processed.add(refnr)
            if args.verbose:
                LOG.info("ÉCARTÉE  %-60s %s", item_title(item)[:60], reason)
            continue

        details, err = fetch_details(session, cfg, refnr)
        if details is None:
            details_failed += 1
            last_detail_error = err
            n_fail = state["echecs"].get(refnr, 0) + 1
            state["echecs"][refnr] = n_fail
            if n_fail >= max_echecs:
                LOG.warning("Détail illisible %d fois, offre abandonnée : %s", n_fail, refnr)
                reasons["détail illisible (abandonnée)"] += 1
                processed.add(refnr)
            continue
        details_ok += 1

        res = evaluate(rec, details, crit, seuils)
        processed.add(refnr)
        if args.verbose:
            LOG.info("%-9s %-60s %s", res["statut"].upper(), item_title(item)[:60], res["raison"])
        if res["statut"] == "eliminee":
            reasons[res["raison"].split(" (")[0]] += 1
        elif res["statut"] == "sous_seuil":
            reasons["sous le seuil de points"] += 1
        else:
            qualified.append({
                "refnr": refnr, "titre": item_title(item) or "(sans titre)",
                "firma": item_firma(item), "lieux": find_places(item)[:3],
                "distance": rec["distance"], "date": item_date(item),
                "type": res["type"], "score": res["score"],
                "competences": res["competences"], "alertes": res["alertes"],
                "lien": LIEN_OFFRE.format(refnr=refnr),
            })

    qualified.sort(key=lambda o: (-o["score"], o["distance"] if o["distance"] is not None else 999))
    qualified = qualified[:int(cfg["email"].get("max_offres_par_email", 50))]

    # --- Bilan --------------------------------------------------------------
    LOG.info("Bilan : %d traitées, %d qualifiées", len(processed), len(qualified))
    for motif, n in reasons.most_common():
        LOG.info("  écartées - %s : %d", motif, n)
    if details_failed:
        LOG.warning("Détail illisible pour %d offre(s) (réessayées au prochain passage). "
                    "Dernière erreur : %s", details_failed, last_detail_error)

    # --- E-mail -------------------------------------------------------------
    if qualified:
        subject, html_body, text_body = build_email(qualified, cfg)
        if args.dry_run:
            print("\n=== DRY-RUN : e-mail non envoyé ===\nSujet :", subject, "\n")
            print(text_body)
        else:
            try:
                send_email(subject, html_body, text_body, cfg)
                LOG.info("E-mail envoyé (%d offre(s)).", len(qualified))
            except Exception as exc:  # noqa: BLE001 - on veut tout attraper et échouer proprement
                LOG.error("Échec de l'envoi de l'e-mail : %s", exc)
                return 1  # état NON enregistré : les offres seront renotifiées au prochain passage
    else:
        LOG.info("Aucune offre à notifier.")

    # --- Enregistrement de l'état ------------------------------------------
    if not args.dry_run:
        for refnr in processed:
            state["seen"][refnr] = today
        save_state(state_path, state, int(cfg["etat"]["conservation_jours"]))
        LOG.info("État enregistré : %s", state_path.name)

    if details_failed and details_ok == 0:
        LOG.error("Aucun détail d'annonce n'a pu être lu : vérifier details_paths dans la config "
                  "(endpoint non documenté officiellement).")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
