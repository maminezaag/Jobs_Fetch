#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arbeitnow : source complémentaire de stat_jobs.

Le code vit dans job_watcher_V2.0/ (nom choisi par l'utilisateur).
Il crée UNE seule feuille "offre arbeitsnow" dans le MÊME Google Sheets.
L'archive BA, ses statistiques, et job_watcher.py restent inchangés.

Note : « non retrouvé dans l'archive BA » n'est PAS une preuve absolue
que l'annonce est absente du portail BA. Les comparaisons sont heuristiques.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

import requests

LOG = logging.getLogger("job_watcher_V2.0.arbeitnow")
ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "https://www.arbeitnow.com/api/job-board-api"
SHEET_NAME = "offre arbeitsnow"

# Les postes voisins (DevOps, Cloud, SRE) sont inclus dans cet OBSERVATOIRE,
# pas dans les notifications du Job Watcher personnel.
IT_TITLE = re.compile(
    r"\b(?:it[\s/-]*(?:system|support|admin|infrastruktur|infrastructure|"
    r"service|techniker|engineer|betrieb|operations|netzwerk|network|"
    r"specialist|spezialist|mitarbeiter)|"
    r"edv[\s/-]*(?:admin|techniker|system|support)|"
    r"system[\s/-]*(?:administ|engineer)|"
    r"linux[\s/-]*(?:admin|engineer|support|system)|"
    r"windows[\s/-]*(?:admin|server|engineer)|"
    r"(?:netzwerk|network|infrastruktur|infrastructure)[\s/-]*(?:admin|engineer|techniker)|"
    r"cloud[\s/-]*(?:admin|engineer|operations|platform)|"
    r"devops|site[\s/-]*reliability[\s/-]*engineer|"
    r"platform[\s/-]*engineer|help[\s/-]*desk|service[\s/-]*desk|"
    r"technical[\s/-]*support|systemintegration|"
    r"(?:vmware|microsoft\s*365|azure)[\s/-]*(?:admin|engineer))",
    re.I,
)
NEIGHBOUR = re.compile(
    r"\b(?:devops|site[\s/-]*reliability|platform[\s/-]*engineer|"
    r"cloud[\s/-]*(?:engineer|administrator|operations|platform)|vdi[\s/-]*engineer)\b", re.I
)
FOREIGN = re.compile(
    r"\b(?:united kingdom|england|\buk\b|london|austria|österreich|wien|vienna|"
    r"switzerland|schweiz|zürich|zurich|france|paris|spain|madrid|"
    r"italy|italia|poland|warsaw|portugal|lisbon|netherlands|amsterdam|"
    r"belgium|brussels|romania|czechia|prague|hungary|budapest|"
    r"sweden|stockholm|denmark|copenhagen|ireland|dublin|"
    r"united states|usa|canada|australia)\b", re.I
)
GERMANY = re.compile(
    r"\b(?:germany|deutschland|berlin|hamburg|münchen|munich|"
    r"frankfurt|bremen|oldenburg|delmenhorst|emden|leer|wilhelmshaven|"
    r"brake|westerstede|bad zwischenahn|rastede|wiefelstede|hude|"
    r"lemwerder|ach[ie]m|hannover|hanover|köln|cologne|bonn|düsseldorf|"
    r"dusseldorf|essen|dortmund|bochum|duisburg|münster|munster|"
    r"osnabrück|osnabruck|stuttgart|karlsruhe|mannheim|heidelberg|"
    r"freiburg|nürnberg|nuremberg|augsburg|regensburg|leipzig|dresden|"
    r"chemnitz|magdeburg|halle|potsdam|rostock|kiel|lübeck|lubeck|"
    r"braunschweig|wolfsburg|göttingen|kassel|mainz|wiesbaden|"
    r"saarbrücken|erfurt|jena|paderborn|bielefeld|ingolstadt|"
    r"reutlingen|heilbronn|tübingen|tubingen|böblingen|boeblingen|"
    r"ulm|passau|landshut|konstanz|fulda|steinfeld|vechta|"
    r"cloppenburg|papenburg|niedersachsen|nordrhein.westfalen|"
    r"baden.württemberg|bayern|sachsen|schleswig.holstein|hessen|"
    r"brandenburg|thüringen|rheinland.pfalz|saarland)\b", re.I
)
LOCAL = re.compile(
    r"\b(?:oldenburg|bremen|delmenhorst|brake|westerstede|bad zwischenahn|"
    r"rastede|wiefelstede|hude|lemwerder|berne|sandkrug|ganderkesee|"
    r"wildeshausen|edewecht|elsfleth|schwanewede|ritterhude|osterholz)\b", re.I
)
POSTCODE = re.compile(r"(?<!\d)\d{5}(?!\d)")


def _text(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\b(?:gmbh|ag|se|kg|ug|mbh|co|ltd|inc|deutschland|germany)\b", " ", text)
    text = re.sub(r"\b(?:m\s*/\s*w\s*/\s*d|w\s*/\s*m\s*/\s*d|all genders|gn)\b", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _safe(value: object):
    # Google Sheets est configuré en USER_ENTERED : éviter les formules injectées.
    return "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value


def _date(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).date().isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def _germany(location: str, description: str) -> bool:
    # Remote sans pays connu => exclure plutôt que compter de faux emplois DE.
    if FOREIGN.search(location) and not re.search(r"\b(?:germany|deutschland)\b", location, re.I):
        return False
    if GERMANY.search(location) or POSTCODE.search(location):
        return True
    return bool(re.search(r"\b(?:remote (?:in|within) germany|"
                          r"(?:in|aus|innerhalb von) deutschland|"
                          r"deutschlandweit|germany-wide)\b", description, re.I))


def _segment(title: str, ba_config: dict) -> str | None:
    if not IT_TITLE.search(title):
        return None
    if any(re.search(p, title, re.I) for p in ba_config.get("titres_exclus", [])):
        return None
    if any(re.search(p, title, re.I) for p in ba_config.get("titres_marche_force", [])):
        return "Marché voisin"
    if any(re.search(p, title, re.I) for p in ba_config.get("titres_domaine_force", [])):
        return "Cœur IT"
    return "Marché voisin" if NEIGHBOUR.search(title) else "Cœur IT"


def _convert(raw: dict, ba_config: dict, today: str) -> dict | None:
    title = str(raw.get("title") or "").strip()
    segment = _segment(title, ba_config)
    if not segment:
        return None
    location = str(raw.get("location") or "").strip()
    description = _text(raw.get("description"))
    company = str(raw.get("company_name") or "").strip()
    url = str(raw.get("url") or "").strip()
    slug = str(raw.get("slug") or "").strip()
    published = _date(raw.get("created_at"))
    parsed = urlparse(url)
    if (not company or not description or not published or not slug and not url
            or parsed.scheme != "https" or not parsed.netloc):
        return None
    if not _germany(location, description):
        return None
    return {"refnr": "arbeitnow:" + (slug or url), "titel": title, "firma": company,
            "location": location, "datum": published, "text": description,
            "remote": raw.get("remote") is True, "url": url, "segment": segment,
            "premiere_vue": today}


def _archive_read() -> dict[str, dict]:
    known = {}
    for p in sorted((ROOT / "archive").glob("????-??.jsonl")):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    job = json.loads(line)
                    known[job["refnr"]] = job
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f"Archive Arbeitnow invalide : {p.name}:{n} ({exc})") from exc
    return known


def _archive_write(new: list[dict], today: str) -> None:
    if not new:
        return
    folder = ROOT / "archive"
    folder.mkdir(exist_ok=True, parents=True)
    target = folder / (today[:7] + ".jsonl")
    previous = target.read_text(encoding="utf-8") if target.exists() else ""
    if previous and not previous.endswith("\n"):
        previous += "\n"
    content = "".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in new)
    temp = target.with_suffix(".tmp")
    temp.write_text(previous + content, encoding="utf-8")
    temp.replace(target)


def _page(session, base_url: str, num: int, seconds: int, retries: int) -> dict:
    last = None
    for retry in range(retries):
        try:
            r = session.get(base_url, params={"page": num}, timeout=seconds,
                            headers={"User-Agent": "Jobs_Fetch/Arbeitnow-observatory"})
            r.raise_for_status()
            result = r.json()
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ValueError("Champ JSON 'data' manquant ou invalide")
            return result
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if retry < retries - 1:
                time.sleep(2 * (retry + 1))
    raise RuntimeError(f"API Arbeitnow page {num} : {last}")


# Orthographes alternatives courantes entre l'API BA et Arbeitnow.
# Cette table est volontairement courte : une équivalence trop large pourrait
# masquer deux postes réellement distincts.
_CITY_ALIASES = {
    "munich": "munchen",
    "muenchen": "munchen",
    "cologne": "koln",
    "koeln": "koln",
    "nuremberg": "nurnberg",
    "nuernberg": "nurnberg",
    "hanover": "hannover",
    "duesseldorf": "dusseldorf",
    "dusseldorf": "dusseldorf",
    "frankfurt am main": "frankfurt am main",
}


def _city_normalized(value: object) -> str:
    """Uniformise les variantes allemand/anglais des villes, sans géocodage."""
    text = _norm(value)
    for alias, canonical in sorted(_CITY_ALIASES.items(), key=lambda item: -len(item[0])):
        text = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", canonical, text)
    return re.sub(r"\s+", " ", text).strip()


def _city_compatible(ba_city: str, arbeitnow_location: str) -> bool:
    """Comparer les noms de villes entiers, éventuellement entourés de région/pays."""
    if not ba_city or not arbeitnow_location:
        return True  # comportement historique : ville inconnue, si titre/entreprise concordent
    return (ba_city == arbeitnow_location
            or re.search(r"(?<!\w)" + re.escape(ba_city) + r"(?!\w)", arbeitnow_location) is not None
            or re.search(r"(?<!\w)" + re.escape(arbeitnow_location) + r"(?!\w)", ba_city) is not None)


def _title_without_trailing_city(title: str, city: str) -> str:
    """Retire UNIQUEMENT une ville explicite en fin de titre, après normalisation.

    Exemple : «Senior IT-Administrator (m/w/d), Bochum» devient
    «senior it administrator». La spécialisation du métier reste intacte.
    """
    if city and title.endswith(" " + city):
        return title[:-(len(city) + 1)].strip()
    return title


def _ba_index(ba_archive) -> dict[str, list[tuple[str, str]]]:
    index: dict[str, list[tuple[str, str]]] = {}
    for j in ba_archive:
        firm = _norm(j.get("firma"))
        title = _norm(j.get("titel"))
        city = _city_normalized(j.get("ort"))
        if firm and title:
            index.setdefault(firm, []).append((title, city))
    return index


def _in_ba(job: dict, index: dict) -> bool:
    firm = _norm(job["firma"])
    title = _norm(job["titel"])
    location = _city_normalized(job["location"])
    if not firm or not title:
        return False
    for other_title, other_city in index.get(firm, []):
        if not _city_compatible(other_city, location):
            continue
        # On enlève les villes terminales SEULEMENT après avoir vérifié
        # le lieu ; cela évite de fusionner des annonces de villes différentes.
        normalized_title = _title_without_trailing_city(title, location)
        normalized_ba_title = _title_without_trailing_city(other_title, other_city)
        if (normalized_title == normalized_ba_title
                or SequenceMatcher(None, normalized_title, normalized_ba_title).ratio() >= 0.90):
            return True
    return False


def _language(job: dict, expressions, sprache: dict, classer_langue_fn) -> tuple[str, str]:
    corpus = f"{job['titel']}. {job['text']}"
    found = classer_langue_fn(job["titel"], job["text"], expressions)
    c1_categories = sprache.get("kategorien", {})
    selected = []
    for hit in found:
        if not c1_categories.get(hit["kategorie"], {}).get("zaehlt_als_c1"):
            continue
        name = hit["name"].lower()
        # Éviter "fließend Englisch" et "English C1" pris pour de l'allemand.
        if name in {"c1", "c2"}:
            level = name.upper()
            occurrences = list(re.finditer(r"\b" + level + r"\b", corpus, re.I))
            def is_explicitly_other_language(match):
                # Choisir la langue immédiatement avant C1/C2, et pas un "German B2"
                # qui suit dans la même phrase.
                before = corpus[max(0, match.start()-40):match.start()]
                languages = list(re.finditer(
                    r"\b(?:english|englisch|french|französisch|german|deutsch)\b", before, re.I))
                return bool(languages and languages[-1].group().lower() in
                            {"english", "englisch", "french", "französisch"})
            if occurrences and all(is_explicitly_other_language(m) for m in occurrences):
                continue
        if name in {"muttersprachlich", "verhandlungssicher", "fließend"}:
            pattern = {"muttersprachlich": r"muttersprach\w*",
                       "verhandlungssicher": r"verhandlungssicher\w*",
                       "fließend": r"flie(?:ß|ss)end\w*"}[name]
            if not any(re.search(r"\b(?:deutsch\w*|german)\b", corpus[max(0,m.start()-55):m.end()+55], re.I)
                       for m in re.finditer(pattern, corpus, re.I)):
                continue
        selected.append(hit["name"])
    if selected:
        return "c1", ", ".join(dict.fromkeys(selected))
    for rule in sprache.get("niveaux_info", []):
        for pattern in rule.get("muster", []):
            if re.search(pattern, corpus, re.I):
                return "b2", rule["name"]
    return "b2", "non précisé (C1 non détecté)"


def _tech(job: dict, themes) -> str:
    corpus = f"{job['titel']} {job['text']}"
    return ", ".join(t["name"] for t in themes if any(pattern.search(corpus) for pattern in t["muster"]))


def _make_sheet(jobs: list[dict], ba_archive, ba_config: dict, expressions,
                sprache: dict, themes, today: str, language_func, max_rows: int,
                stats: dict, error: str) -> dict:
    cutoff = (date.fromisoformat(today) - timedelta(days=int(ba_config.get("periode_jours", 90)))).isoformat()
    relevant = [j for j in jobs if j.get("datum", "") >= cutoff]
    relevant.sort(key=lambda x: (x["datum"], x["firma"]), reverse=True)
    index = _ba_index(ba_archive)
    seen = set()
    retained = []
    duplicate_own = duplicate_ba = 0
    for j in relevant:
        sig = (_norm(j["firma"]), _norm(j["titel"]), _norm(j["location"]))
        if sig in seen:
            duplicate_own += 1
            continue
        seen.add(sig)
        if _in_ba(j, index):
            duplicate_ba += 1
            continue
        retained.append(j)
    retained.sort(key=lambda x: (0 if LOCAL.search(x["location"]) else 1,
                                 -int(x["datum"].replace("-", "")), x["firma"]))
    header = ["Date publication", "Titre du poste", "Entreprise", "Ville / lieu",
              "Zone (indicative)", "Mention de langue", "Justification langue",
              "Technologies détectées", "Télétravail (API)", "Lien vers l'offre",
              "Première détection", "Segment IT", "Comparaison BA", "Identifiant Arbeitnow"]
    table = [header]
    for j in retained[:max_rows]:
        level, reason = _language(j, expressions, sprache, language_func)
        table.append([_safe(x) for x in (
            j["datum"], j["titel"], j["firma"], j["location"],
            "Oldenburg / Bremen (indicatif)" if LOCAL.search(j["location"]) else "Allemagne - autre",
            level, reason, _tech(j, themes), "Oui (selon API)" if j["remote"] else "Non précisé",
            j["url"], j["premiere_vue"], j["segment"],
            "Non retrouvé dans archive BA", j["refnr"]
        )])
    stats.update({"periode": len(relevant), "duplicate_ba": duplicate_ba,
                  "duplicate_internes": duplicate_own, "not_found_ba": len(retained),
                  "shown": len(table)-1, "truncated": max(0, len(retained)-max_rows)})
    LOG.info("Arbeitnow : %s offres de la période ; %s doublons BA, %s internes ; %s non retrouvées dans BA ; %s lignes ajoutées%s",
             len(relevant), duplicate_ba, duplicate_own, len(retained), len(table)-1,
             f" (attention, {stats['truncated']} supplémentaires hors limite d'affichage)" if stats["truncated"] else "")
    if error:
        LOG.warning("Arbeitnow : données archivées utilisées après erreur API : %s", error)
    return {"values": table, "gras": [0], "freeze": 1,
            "col_widths": {0: 125, 1: 320, 2: 260, 3: 220, 4: 190, 5: 130,
                           6: 240, 7: 320, 8: 140, 9: 330, 10: 140, 11: 130,
                           12: 225, 13: 235}}


def collecter_onglet(cfg_ba: dict, ba_archive, expressions, sprache: dict, themes,
                     today: str, classer_langue_fn, session=None, save_archive=True) -> dict:
    """Une seule feuille 'offre arbeitsnow' et aucune modification aux onglets existants."""
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    current = _archive_read()  # NE jamais effacer l'historique en cas de panne.
    pending = {}
    stats = {"scanned": 0, "pages": 0, "new": 0}
    error = ""
    sess = session or requests.Session()
    try:
        seen_next = set()
        max_pages = max(1, int(cfg.get("max_pages", 12)))
        for page in range(1, max_pages+1):
            response = _page(sess, cfg.get("source_url", DEFAULT_URL), page,
                             int(cfg.get("timeout_seconds", 35)), int(cfg.get("retries", 3)))
            batch = response["data"]
            stats["pages"] += 1
            stats["scanned"] += len(batch)
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                job = _convert(raw, cfg_ba, today)
                if job and job["refnr"] not in current and job["refnr"] not in pending:
                    pending[job["refnr"]] = job
            link_next = (response.get("links") or {}).get("next")
            if not batch or not link_next or link_next in seen_next:
                break
            seen_next.add(link_next)
            if page < max_pages:
                time.sleep(float(cfg.get("pause_seconds", 0.5)))
        if save_archive:
            _archive_write(list(pending.values()), today)
        current.update(pending)
        stats["new"] = len(pending)
        LOG.info("Arbeitnow : %d pages, %d annonces brutes, %d nouvelles IT DE dans l'archive",
                 stats["pages"], stats["scanned"], stats["new"])
    except Exception as exc:  # facultatif : un échec Arbeitnow n'arrête pas BA
        error = f"{type(exc).__name__}: {exc}"
        LOG.warning("Collecte Arbeitnow indisponible, archive conservée : %s", error)
    # Passer une vue des offres BA, sans la modifier.
    return {SHEET_NAME: _make_sheet(list(current.values()), ba_archive, cfg_ba,
                                   expressions, sprache, themes, today,
                                   classer_langue_fn, max(1, int(cfg.get("max_rows_sheet", 1500))),
                                   stats, error)}
