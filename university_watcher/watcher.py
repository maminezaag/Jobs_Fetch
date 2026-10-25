#!/usr/bin/env python3
"""Surveillance d'offres universitaires (dossier autonome de Jobs_Fetch).

Collecte PUBLIQUE et à basse fréquence seulement. N'utilise ni authentification,
ni contournement de protections. Les 4 adaptateurs sont spécifiques aux sites.
L'échec d'une source apparaît explicitement dans le compte rendu et dans les logs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, date
from email.message import EmailMessage
from io import BytesIO
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import smtplib
import sys
import time
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
HEADERS = {"User-Agent": "UniversityJobWatcher/1.0 (personal job alerts; public pages; 1 request per 1.5s)"}
TIMEOUT = 22
log = logging.getLogger("university_watcher")

@dataclass
class Offre:
    source: str
    titre: str
    url: str
    ville: str
    texte: str = ""
    # Vérification positive après lecture de la fiche individuelle.
    detail_verifie: bool = False
    detail_erreur: str = ""

    @property
    def identifiant(self):
        return hashlib.sha256(self.url.split('#')[0].encode('utf-8')).hexdigest()[:20]


def txt(tag):
    return tag.get_text(" ", strip=True) if tag else ""


def normaliser(texte):
    return re.sub(r"\s+", " ", html.unescape(texte or "")).strip()


def occur(texte, mot):
    # Evite que IT, BPM, RHEL ou git soient détectés dans un mot plus long.
    return bool(re.search(r"(?<!\w)" + re.escape(mot.casefold()) + r"(?!\w)", texte.casefold()))


def meme_site(url, source_url):
    # Les documents peuvent être hébergés dans le sous-domaine officiel du site.
    a, b = urlparse(url).hostname or "", urlparse(source_url).hostname or ""
    return a == b or (a.endswith("." + b.split(".", 1)[-1]) and b.split(".", 1)[-1] not in ("de", "com"))


class Collecteur:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.last_request = 0.0
        self.robots = {}

    def get(self, url):
        u = urlparse(url)
        if u.scheme not in {"http", "https"}:
            raise RuntimeError("URL non HTTP(S)")
        host = f"{u.scheme}://{u.netloc}"
        if host not in self.robots:
            rp = RobotFileParser()
            try:
                res = self.session.get(host + "/robots.txt", timeout=TIMEOUT)
                if res.status_code == 200:
                    rp.parse(res.text.splitlines())
                    self.robots[host] = rp
                elif res.status_code == 404:
                    self.robots[host] = None
                else:
                    raise RuntimeError(f"robots.txt statut {res.status_code}")
            except requests.RequestException as exc:
                raise RuntimeError(f"robots.txt inaccessible : {exc}") from exc
        rp = self.robots[host]
        if rp and not rp.can_fetch(HEADERS["User-Agent"], url):
            raise RuntimeError(f"Collecte interdite par robots.txt : {url}")
        pause = float(self.config.get("delai_requetes_secondes", 1.5))
        time.sleep(max(0, pause - (time.monotonic() - self.last_request)))
        resp = self.session.get(url, timeout=TIMEOUT)
        self.last_request = time.monotonic()
        resp.raise_for_status()
        if len(resp.content) > 5_000_000:
            raise RuntimeError("Document supérieur à 5 Mo")
        return resp

    def page_text(self, url):
        resp = self.get(url)
        if resp.headers.get("Content-Type", "").lower().startswith("application/pdf") or urlparse(resp.url).path.lower().endswith(".pdf"):
            from pypdf import PdfReader
            pdf = PdfReader(BytesIO(resp.content))
            return normaliser(" ".join((p.extract_text() or "") for p in pdf.pages[:12]))
        soup = BeautifulSoup(resp.text, "html.parser")
        for el in soup.select("script, style, nav, footer, header, aside, form"):
            el.decompose()
        # Une fiche individuelle est prioritaire sur le menu et les listes d'offres.
        # Pour TYPO3 (Bremen), <main> contient les tâches / prérequis de l'annonce.
        root = soup.select_one("main") or soup.select_one("article") or soup.body or soup
        contenu = normaliser(txt(root))[:50000]
        if len(contenu) < 180 or re.search(r"(?:404\s+not\s+found|seite\s+nicht\s+gefunden|access\s+denied)", contenu[:300], re.I):
            raise RuntimeError(f"Description individuelle absente/trop courte ({len(contenu)} caractères)")
        return contenu

    def collect_oldenburg(self, src):
        resp = self.get(src["url"])
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise RuntimeError("Le RSS d'Oldenburg n'est pas du XML valide") from exc
        offres = []
        entries = root.findall(".//item")
        if not entries:
            # RSS Atom, si l'institution change de format
            entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in entries:
            title = (item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link = item.findtext("link") or ""
            if not link:
                node = item.find("{http://www.w3.org/2005/Atom}link")
                if node is not None:
                    link = node.get("href", "")
            description = item.findtext("description") or item.findtext("{http://www.w3.org/2005/Atom}summary") or ""
            if title and link:
                offres.append(Offre("oldenburg", title, urljoin(src["url"], link), "Oldenburg", normaliser(BeautifulSoup(description, "html.parser").get_text(" "))))
        return offres

    def collect_bremen(self, src):
        soup = BeautifulSoup(self.get(src["url"]).text, "html.parser")
        offres = []
        # Lien de chaque résultat : "Bewerbungsfrist DATE ... Kennziffer: ...".
        for a in soup.find_all("a", href=True):
            contenu = normaliser(txt(a))
            if not re.search(r"Bewerbungsfrist\s+\d{1,2}\.\d{1,2}\.\d{4}", contenu, re.I):
                continue
            url = urljoin(src["url"], a["href"])
            titre = re.sub(r"^.*?Bewerbungsfrist\s+\d{1,2}\.\d{1,2}\.\d{4}\s*", "", contenu, flags=re.I)
            titre = re.sub(r"^Neu:\s*", "", titre, flags=re.I)
            titre = re.split(r"\bKennziffer\s*:", titre, flags=re.I)[0].strip()
            if len(titre) > 180:
                titre = titre[:180] + "…"
            offres.append(Offre("bremen", titre, url, "Bremen", contenu))
        return offres

    def collect_jade(self, src):
        """Lit les offres tarifaires sur le sous-domaine HTTPS officiel www.

        La date limite est dans la 3e colonne : ne pas alerter après expiration.
        """
        soup = BeautifulSoup(self.get(src["url"]).text, "html.parser")
        offres = []
        for row in soup.select("table tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) < 3:
                continue
            reference = normaliser(txt(cells[0]))
            # V 07/26, F&T 05/26 etc. (la regex originale ratait F&T).
            if not re.fullmatch(r"[A-Z]{1,6}(?:&[A-Z]{1,6})?\s+\d{1,3}/\d{2}", reference, re.I):
                continue
            m = re.search(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b", txt(cells[2]))
            if m:
                try:
                    if datetime.strptime(m.group(1), "%d.%m.%Y").date() < date.today():
                        log.info("jade: offre expirée écartée : %s (%s)", reference, m.group(1))
                        continue
                except ValueError:
                    log.warning("jade: date de fin invalide : %s", m.group(1))
            links = [a for a in cells[1].find_all("a", href=True) if a["href"].strip()]
            # Ne pas générer de pseudo-URL pour une annonce sans lien.
            if not links:
                log.warning("jade: pas de lien d'annonce pour %s", reference)
                continue
            link = urljoin(src["url"], links[0]["href"])
            titre = normaliser(txt(cells[1]))[:220]
            ville = ville_depuis_texte(txt(row), src["ville_defaut"])
            offres.append(Offre("jade", titre, link, ville, normaliser(txt(row))))
        return offres

    @staticmethod
    def coveto_offres_depuis_html(html_source, source_url):
        """Parse les lignes du tableau Coveto, si elles sont présentes dans HTML."""
        soup = BeautifulSoup(html_source, "html.parser")
        items = []
        for row in soup.select("table tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 2:
                continue
            anchor = cells[0].find("a", href=True)
            if not anchor:
                continue
            title = normaliser(txt(cells[0]))
            url = urljoin(source_url, anchor.get("href", ""))
            if not title or not url.startswith("https://"):
                continue
            city = normaliser(txt(cells[1])) or "Emden"
            items.append(Offre("emden", title[:220], url, ville_depuis_texte(city, city), normaliser(txt(row))))
        return items

    @staticmethod
    def coveto_table_reconnue(html_source):
        """Valide la structure du portail, indépendamment de la présence de jobs.

        Un tableau arbitraire ne suffit pas : les colonnes officielles doivent
        être présentes. Utilisé uniquement APRES le chargement dynamique.
        """
        soup = BeautifulSoup(html_source, "html.parser")
        for table in soup.find_all("table"):
            headings = normaliser(" ".join(txt(t) for t in table.select("th"))).casefold()
            if "stellenname" in headings and "arbeitsort" in headings:
                return True
        return False

    @staticmethod
    def coveto_semble_charger(texte):
        """Empêche de déclarer vide une page affichant encore le chargement."""
        return bool(re.search(r"(?:bitte\s+warten|daten\s+werden\s+geladen|jobs?\s+werden\s+geladen|loading\s+(?:jobs|data))", texte, re.I))

    @staticmethod
    def coveto_indique_aucune_offre(texte):
        """N'accepte qu'un vrai message de 'aucun résultat', jamais l'en-tête Stellenname."""
        return bool(re.search(r"\b(?:keine\s+(?:offenen?\s+|aktuellen?\s+)?"
                              r"(?:stellen|stellenangebote|treffer)|0\s+(?:treffer|ergebnisse)|"
                              r"keine\s+ergebnisse)\b", texte, re.I))

    def collect_emden(self, src):
        """Consulte le portail public Coveto sur l'hôte officiel de l'établissement.

        N'accepte zéro annonce que si le navigateur a fini son chargement,
        que le VRAI tableau de jobs a été reconnu et que son état est stable.
        Une erreur réseau ou une page sans tableau restent des erreurs.
        """
        response = self.get(src["url"])
        offres = self.coveto_offres_depuis_html(response.text, src["url"])
        if offres:
            return offres
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright absent. Installer Chromium (voir README).") from exc
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                erreurs_reseau = []
                hote = urlparse(src["url"]).hostname

                def reponse_observee(r):
                    if (urlparse(r.url).hostname == hote
                            and r.request.resource_type in {"document", "xhr", "fetch"}
                            and r.status >= 400):
                        erreurs_reseau.append(f"HTTP {r.status} sur {r.url[:120]}")

                def requete_echouee(req):
                    if (urlparse(req.url).hostname == hote
                            and req.resource_type in {"document", "xhr", "fetch"}):
                        erreurs_reseau.append(f"Échec réseau sur {req.url[:120]}")

                page.on("response", reponse_observee)
                page.on("requestfailed", requete_echouee)
                resp = page.goto(src["url"], wait_until="load", timeout=45000)
                if resp is None or resp.status >= 400:
                    raise RuntimeError(f"Coveto: erreur chargement du document ({resp.status if resp else 'sans réponse'})")

                # Plusieurs observations pour éviter les faux zéros lorsque
                # le JavaScript ne termine pas encore son rendu.
                precedent = None
                etat_stable = 0
                for _ in range(6):
                    page.wait_for_timeout(2000)
                    rendu = page.content()
                    offres = self.coveto_offres_depuis_html(rendu, src["url"])
                    if offres:
                        return offres
                    corps = page.locator("body").inner_text(timeout=10000)
                    if erreurs_reseau:
                        raise RuntimeError("Coveto: requête nécessaire en échec : " + "; ".join(erreurs_reseau[:2]))
                    if self.coveto_indique_aucune_offre(corps):
                        log.info("emden: Coveto signale explicitement zéro offre")
                        return []
                    tables_visibles = page.locator("table").filter(has_text="Stellenname")
                    table_visible = (self.coveto_table_reconnue(rendu)
                                     and tables_visibles.count() > 0
                                     and tables_visibles.first.is_visible())
                    en_chargement = self.coveto_semble_charger(corps)
                    # Le contenu peut inclure des horodatages dynamiques ;
                    # on compare seulement les lignes du tableau principal.
                    soup = BeautifulSoup(rendu, "html.parser")
                    signature = tuple(normaliser(txt(row)) for row in soup.select("table tr"))
                    if table_visible and not en_chargement and signature == precedent:
                        etat_stable += 1
                    else:
                        etat_stable = 0
                    precedent = signature if table_visible and not en_chargement else None
                    if etat_stable >= 2:
                        log.warning("emden: tableau Coveto reconnu, vide et stable après rendu JS ; zéro offre actuellement visible")
                        return []
                raise RuntimeError("Coveto: liste non confirmée après chargement JS ; source NON VÉRIFIÉE "
                                   f"(tableau reconnu={self.coveto_table_reconnue(page.content())})")
            finally:
                browser.close()

    def collect(self, cle, src):
        methods = {"oldenburg": self.collect_oldenburg, "bremen": self.collect_bremen,
                   "jade": self.collect_jade, "emden": self.collect_emden}
        if cle not in methods:
            raise ValueError(f"Source inconnue: {cle}")
        items = methods[cle](src)
        if not items and cle in {"oldenburg", "bremen", "jade"}:
            raise RuntimeError("0 offre détectée sur un portail normalement actif : extraction à vérifier")
        return items


def ville_depuis_texte(texte, ville_defaut):
    # Ordre important : Bad Zwischenahn doit être reconnu avant 'Zwischenahn'.
    villes = ["Bad Zwischenahn", "Oldenburg", "Bremen", "Brême", "Wilhelmshaven", "Elsfleth", "Emden", "Leer", "Westerstede", "Rastede", "Brake", "Delmenhorst"]
    for v in villes:
        if occur(texte, v):
            return "Bremen" if v == "Brême" else v
    return ville_defaut


def geo(ville, texte):
    # Une localisation incertaine = estimation signalée dans l'alerte.
    if ville.casefold() == "oldenburg": return 30, "LOCAL"
    if ville.casefold() == "bremen": return 28, "LOCAL"
    if ville.casefold() in {"bad zwischenahn", "wilhelmshaven", "elsfleth", "emden", "leer", "westerstede", "rastede", "brake", "delmenhorst"}:
        return 25, "LOCAL"
    if re.search(r"(?<!\w)(100\s*%\s*remote|fully remote|vollständig(?:es)? homeoffice|remote-first)(?!\w)", texte, re.I):
        return 20, "REMOTE"
    if re.search(r"\b(homeoffice|hybrid|remote|mobiles arbeiten)\b", texte, re.I):
        return 10, "REMOTE À VÉRIFIER"
    return 0, "HORS ZONE"


def classe(titre, texte):
    v = (titre + " " + texte[:1200]).casefold()
    if re.search(r"doktorand|promotionsstelle|doctoral researcher|phd position|phd student|gelegenheit zur promotion|möglichkeit zur promotion", v):
        return "Doctorat rémunéré (à confirmer dans l'annonce)"
    if re.search(r"wissenschaftlich|research assistant|researcher|forschung|wissenschaft", v):
        return "Recherche scientifique"
    return "Emploi informatique / autre"


def niveau_associe_a_allemand(texte, niveau):
    """Associe C1/C2 à Deutsch/German et non à Englisch/English.

    Les annonces utilisent aussi « deutschen Sprache ... (mind. CEF-Level C1) » :
    ce libellé dépasse la fenêtre de 45 caractères de la V3. On considère
    la langue la plus proche, mais sans traverser une fin de phrase / puce.
    """
    langues = list(re.finditer(r"\b(?:deutsch\w*|german|englisch\w*|english)\b", texte, re.I))
    for niveau_match in re.finditer(r"(?<!\w)" + re.escape(niveau) + r"(?!\w)", texte, re.I):
        candidats = []
        for langue in langues:
            debut, fin = sorted((niveau_match.start(), langue.start()))
            entre = texte[debut:fin]
            # « mind. CEF-Level C1 » contient une abréviation et non une fin
            # de phrase. Neutraliser ces points AVANT le test de séparation.
            entre_sans_abreviations = re.sub(r"\b(?:mind|ggf|bzw|z\.b)\.\s*", " ", entre, flags=re.I)
            if len(entre) > 110 or re.search(r"[;\n]|[.!?](?:\s|$)", entre_sans_abreviations):
                continue
            candidats.append((abs(langue.start() - niveau_match.start()), langue.group(0).lower()))
        if candidats and min(candidats, key=lambda x: x[0])[1].startswith(("deutsch", "german")):
            yield niveau_match


def exclure(titre, texte):
    s = normaliser(titre + " " + texte).casefold()
    if re.search(r"\b(?:professur|professor:in|professorin|professor)\b", titre.casefold()):
        return "professorat hors cible"
    if re.search(r"\b(?:ausbildung|auszubildend|duales studium|praktikant|studentische hilfskraft)\w*", titre.casefold()):
        return "formation/stage/emploi étudiant"
    if re.search(r"(?:abgeschlossen[ea-z]*|erfolgreich abgeschlossen[ea-z]*|bereits erworben[ea-z]*)\s+(?:wissenschaftlich[ea-z]*\s+)?(?:promotion|doktorgrad)", s):
        return "doctorat déjà obtenu exigé"
    if re.search(r"\b(?:postdoc|post-doc|postdoktorand)\b", titre.casefold()):
        return "postdoctorat"
    for niveau in ("c1", "c2"):
        for match in niveau_associe_a_allemand(s, niveau):
            # Les mentions « wünschenswert » ou « von Vorteil » qui qualifient
            # le niveau NE doivent PAS déclencher une exclusion.
            avant = s[max(0, match.start()-110):match.start()]
            apres = s[match.end():match.end()+55]
            if re.search(r"^\s*(?:\)|,|:|-)?\s*(?:wünschenswert|von vorteil|optional|nicht erforderlich|keine voraussetzung)\b", apres):
                continue
            if re.search(r"(?:wünschenswert|von vorteil|optional)\W{0,18}(?:cef[- ]?level\s*)?$", avant):
                continue
            # Le contexte « mind. CEF-Level C1 » est une exigence explicite.
            if re.search(r"(?:mind\.?|mindestens|zwingend|vorausgesetzt|erforderlich|voraussetzung|erwartet|minimum|nachweis|sicher\w* beherrschung)", avant[-90:] + " " + apres[:22]):
                return "allemand C1/C2 requis"
            # Quand la langue est explicitement qualifiée C1/C2 sans "souhaité",
            # elle est considérée comme exigée (ex. « Deutsch C1 »).
            if re.search(r"\b(?:deutsch\w*|german)\b.{0,100}(?:cef[- ]?level\s*)?$", avant):
                return "allemand C1/C2 requis"
    if re.search(r"\b(?:muttersprachlich\w*|native\s+german)\b.{0,30}\bdeutsch", s) or re.search(r"\bdeutsch\w*\b.{0,30}\bmuttersprachlich", s):
        return "allemand maternel exigé"
    b2_accepte = bool(list(niveau_associe_a_allemand(s, "b2")))
    if not b2_accepte and re.search(r"verhandlungssicher\w*\s+(?:deutsch|deutschkenntnisse)|verhandlungssicher\w*\s+deutsch\s*-", s):
        return "allemand verhandlungssicher exigé (B2 non mentionné)"
    if re.search(r"\b(?:promotion|promotionsstelle|doktorand|doctoral|phd)\b", s) and re.search(r"(?:ohne\s+(?:gehalt|vergütung|bezahlung)|unbezahlt|unfunded\s+phd)", s):
        return "doctorat non rémunéré"
    return ""


def points_contrat(texte):
    s = texte.casefold()
    m = re.search(r"\b(?:tv[- ]?l|e)\s*e?\s*(?:g?\s*)?(\d{1,2})\s*[abc]?\b|\b(?:e|eg)\s*[- ]?\s*(\d{1,2})\s*[abc]?\s*(?:tv[- ]?l)?\b", s)
    n = int(m.group(1) or m.group(2)) if m else 0
    sal = 10 if n >= 13 else 8 if n == 12 else 6 if n in (10, 11) else 4 if n >= 9 else 0
    percent = re.search(r"\b(100|90|80|75|70|65|60|50)\s*%", s)
    q = int(percent.group(1)) if percent else 0
    # Si 50% est annoncé, ne pas l'écraser avec la mention « Gehalt für eine
    # Vollzeitstelle » dans le paragraphe standard sur les salaires.
    temps = (5 if q >= 100 else 3 if q >= 75 else 1 if q >= 50 else 0) if q else (5 if re.search(r"\bvollzeit\b|\bfull[- ]time\b", s) else 0)
    return min(15, sal + temps), (f"TV-L E{n}" if n else "Rémunération inconnue") + (f", {q}%" if q else "")


def noter(o, cfg):
    texte = normaliser(o.titre + " " + o.texte)
    exclusion = exclure(o.titre, o.texte)
    trouve = [(g["nom"], g["points"]) for g in cfg["competences"] if any(occur(texte, mot) for mot in g["synonymes"])]
    skills = min(cfg["plafond_competences"], sum(pts for _, pts in trouve))
    lieu, typ = geo(o.ville, texte)
    contrat, cond = points_contrat(texte)
    score = skills + lieu + contrat
    # Noter() fonctionne également sur des offres synthétiques (tests de score).
    # Le contrôle obligatoire du détail est appliqué dans main() avant e-mail.
    eligible = not exclusion and typ != "HORS ZONE" and score >= cfg["seuil_total"] and skills >= cfg["minimum_competences"]
    motifs = []
    if exclusion: motifs.append(exclusion)
    if typ == "HORS ZONE": motifs.append("hors zone et pas de remote vérifiable")
    if skills < cfg["minimum_competences"]: motifs.append(f"compétences {skills}/{cfg['minimum_competences']}")
    if score < cfg["seuil_total"]: motifs.append(f"total {score}/{cfg['seuil_total']}")
    return {"score": score, "skills": skills, "lieu": lieu, "contrat": contrat, "conditions": cond,
            "localite": typ, "categorie": classe(o.titre, o.texte), "competences": trouve,
            "exclusion": exclusion, "eligible": eligible, "motifs": motifs}


def decision_apres_verification(offre, note):
    """Une fiche manquante ne doit jamais déclencher un faux positif."""
    if not offre.detail_verifie:
        note["eligible"] = False
        if "fiche complète non vérifiée" not in note["motifs"]:
            note["motifs"].append("fiche complète non vérifiée")
    return note


def emailer(resultats, destinataire, adresse, mot_de_passe):
    msg = EmailMessage()
    msg["Subject"] = f"University Watcher : {len(resultats)} nouvelle(s) offre(s) pertinente(s)"
    msg["From"] = adresse
    msg["To"] = destinataire
    lines = ["Offres universitaires détectées, classement par score :", ""]
    for o, note in resultats:
        lines.extend([f"{o.titre} — {note['score']}/100", f"{o.source} — {o.ville} — {note['localite']} — {note['categorie']}",
                      f"Compétences {note['skills']}/55 ; lieu {note['lieu']}/30 ; contrat {note['contrat']}/15 ({note['conditions']})",
                      "Correspondances : " + ", ".join(n for n,_ in note["competences"]),
                      f"Annonce : {o.url}", ""])
    msg.set_content("\n".join(lines))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=25) as smtp:
        smtp.login(adresse, mot_de_passe)
        smtp.send_message(msg)


def main():
    ap = argparse.ArgumentParser(description="Offres universitaires : scoring + e-mail")
    ap.add_argument("--dry-run", action="store_true", help="analyse sans envoyer ni enregistrer")
    ap.add_argument("--sources", nargs="*", choices=["oldenburg", "bremen", "jade", "emden"], default=None)
    args = ap.parse_args()
    cfg = json.loads((BASE / "criteres.json").read_text(encoding="utf-8"))
    etat = BASE / "seen_university_jobs.json"
    seen = set(json.loads(etat.read_text(encoding="utf-8"))) if etat.exists() else set()
    collector = Collecteur(cfg)
    offres, erreurs = {}, []
    for key, src in cfg["sources"].items():
        if args.sources and key not in args.sources: continue
        try:
            items = collector.collect(key, src)
            log.info("%s: %d offres brutes", key, len(items))
            for item in items:
                # Pas de lien: ne pas inventer de résultat.
                if not item.url.startswith("http"): continue
                if item.identifiant in offres: continue
                if item.identifiant in seen and not args.dry_run: continue
                # Lire la description complète quand elle est accessible.
                # Pour Emden, Playwright aura résolu la liste mais le détail peut varier.
                if item.url.split('#')[0] != src["url"].split('#')[0]:
                    try:
                        detail = collector.page_text(item.url)
                        item.texte += " " + detail
                        item.detail_verifie = True
                        log.info("DETAIL OK %s — %d caractères ; %s", key, len(detail), item.url[:130])
                    except Exception as exc:
                        item.detail_erreur = str(exc)
                        log.warning("DETAIL NON VERIFIE %s — %s — %s", key, item.url[:130], exc)
                else:
                    item.detail_erreur = "Le lien renvoie à la liste des annonces"
                    log.warning("DETAIL NON VERIFIE %s — %s", key, item.detail_erreur)
                offres[item.identifiant] = item
        except Exception as exc:
            log.error("Source %s non analysée : %s", key, exc)
            erreurs.append(f"{key}: {exc}")
    qualifiees = []
    for o in offres.values():
        score = noter(o, cfg)
        score = decision_apres_verification(o, score)
        raison = "; ".join(score["motifs"]) if score["motifs"] else "conditions remplies"
        log.info("%3d points (skills %2d) %s — %s — %s — %s", score["score"], score["skills"], o.source,
                 "QUALIFIEE" if score["eligible"] else "ECARTEE", raison, o.titre[:110])
        if score["eligible"]: qualifiees.append((o, score))
    qualifiees.sort(key=lambda x:x[1]["score"], reverse=True)
    nombre = min(len(qualifiees), cfg["max_offres_email"])
    incompletes = [o for o in offres.values() if not o.detail_verifie]
    log.info("%d offres qualifiées, %d pour e-mail ; %d source(s) en échec ; %d fiche(s) incomplète(s)",
             len(qualifiees), nombre, len(erreurs), len(incompletes))
    if incompletes:
        log.warning("Attention: %d annonces n'ont PAS pu être évaluées sur leur texte complet. Vérifier les avertissements DETAIL NON VERIFIE.", len(incompletes))
    if args.dry_run:
        for o,n in qualifiees[:nombre]: print(f"{n['score']:>3}/100 [{o.source}] {o.titre}: {o.url}")
    elif nombre:
        noms = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "GMAIL_TO")
        if not all(os.getenv(k) for k in noms):
            log.error("Secrets manquants : %s", ", ".join(k for k in noms if not os.getenv(k)))
            return 1
        emailer(qualifiees[:nombre], os.environ["GMAIL_TO"], os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        seen.update(o.identifiant for o,_ in qualifiees[:nombre])
        etat.write_text(json.dumps(sorted(seen), indent=2) + "\n", encoding="utf-8")
        log.info("Notification envoyée ; état enregistré")
    if erreurs:
        # Code 2 : collecte partielle, même si d'autres offres ont été envoyées.
        print("ATTENTION : sources non vérifiées : " + " | ".join(erreurs), file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
