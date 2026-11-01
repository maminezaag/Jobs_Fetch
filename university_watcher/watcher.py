#!/usr/bin/env python3
"""Surveillance d'offres universitaires (dossier autonome de Jobs_Fetch).

Collecte PUBLIQUE et à basse fréquence seulement. N'utilise ni authentification,
ni contournement de protections. Les 4 adaptateurs sont spécifiques aux sites.
L'échec d'une source apparaît explicitement dans le compte rendu et dans les logs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, date, timedelta
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
from adequation import evaluer_adequation

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

    def get(self, url, *, timeout=None):
        # Le délai personnalisé est réservé aux contrôles des laboratoires ;
        # les collecteurs d'offres officielles gardent leur fonctionnement.
        timeout = TIMEOUT if timeout is None else timeout
        u = urlparse(url)
        if u.scheme not in {"http", "https"}:
            raise RuntimeError("URL non HTTP(S)")
        host = f"{u.scheme}://{u.netloc}"
        if host not in self.robots:
            rp = RobotFileParser()
            try:
                res = self.session.get(host + "/robots.txt", timeout=timeout)
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
        resp = self.session.get(url, timeout=timeout)
        self.last_request = time.monotonic()
        resp.raise_for_status()
        if len(resp.content) > 5_000_000:
            raise RuntimeError("Document supérieur à 5 Mo")
        return resp

    def get_laboratoire(self, url):
        """Relance une requête de laboratoire seulement en cas d'erreur transitoire.

        Le contrôle robots.txt et la vérification SSL restent obligatoires.
        On n'ignore jamais une interdiction, un 403 ou une erreur de certificat.
        """
        section = self.config.get('pages_laboratoires', {})
        timeout = min(60, max(TIMEOUT, float(section.get('timeout_secondes', 35))))
        essais = min(2, max(0, int(section.get('reessais_erreurs_transitoires', 1))))
        pause = min(15, max(0, float(section.get('pause_reessai_secondes', 2))))
        for numero in range(essais + 1):
            try:
                return self.get(url, timeout=timeout)
            except (requests.RequestException, RuntimeError) as exc:
                erreur = str(exc).casefold()
                # Erreurs de certificat, interdictions robots.txt et 4xx ordinaires
                # ne doivent JAMAIS être contournées par une autre méthode.
                transitoire = bool(re.search(
                    r'timed?\s*out|timeout|\b(?:408|429|500|502|503|504)\b|'
                    r'connection (?:reset|aborted|refused|broken)|'
                    r'remote end closed|temporar(?:y|ily)', erreur))
                if ('ssl' in erreur or 'certificate' in erreur or
                        'collecte interdite' in erreur or not transitoire or
                        numero >= essais):
                    raise
                log.warning('LAB requête temporairement indisponible : %s ; '
                            'nouvelle tentative %d/%d', url, numero + 1, essais)
                time.sleep(pause)

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

    @staticmethod
    def oldenburg_jobnummer(url):
        """Identifiant stable des fiches UOL, y compris les liens avec slug."""
        path = urlparse(url).path.rstrip('/')
        m = re.search(r"/job(?:/[^/]*?-)?(\d{3,6})(?:en)?$", path, re.I)
        if not m:
            m = re.search(r"/job(\d{3,6})(?:en)?$", path, re.I)
        return m.group(1) if m else None

    @staticmethod
    def oldenburg_liste_html(html_source, page_url, aujourd_hui=None):
        """Toutes les annonces /jobs, indépendamment du titre ou de la catégorie RSS.

        Une annonce sans date lisible est conservée pour examen. Une date limite
        explicitement passée exclut la fiche. Les titres viennent des liens d'annonce.
        """
        aujourd_hui = aujourd_hui or date.today()
        soup = BeautifulSoup(html_source, 'html.parser')
        offres = []
        for heading in soup.find_all(['h2', 'h3', 'h4']):
            a = heading.find('a', href=True)
            if a is None:
                continue
            link = urljoin(page_url, a['href'])
            if urlparse(link).hostname not in {'uol.de', 'www.uol.de'}:
                continue
            numero = Collecteur.oldenburg_jobnummer(link)
            if not numero:
                continue
            title = normaliser(txt(a))
            if not title:
                continue
            # Contenu visible après l'en-tête, jusqu'au prochain titre d'annonce.
            parties = []
            for sibling in heading.next_siblings:
                if getattr(sibling, 'name', None) in {'h2', 'h3', 'h4'}:
                    break
                parties.append(txt(sibling) if getattr(sibling, 'name', None) else str(sibling))
                if sum(map(len, parties)) > 2500:
                    break
            descriptif = normaliser(' '.join(parties))
            m = re.search(r"Bewerbungsfrist\s*:?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", descriptif, re.I)
            if m:
                try:
                    limite = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                    if limite < aujourd_hui:
                        continue
                except ValueError:
                    pass
            offres.append(Offre('oldenburg', title, link, 'Oldenburg', descriptif))
        return offres

    def collect_oldenburg(self, src):
        """Compare le flux RSS et le portail global, avec secours si l'un échoue."""
        rss_url = src['url']
        portail_url = src.get('url_portail', 'https://uol.de/jobs')
        rss_offres, portail_offres, erreurs = [], [], []
        try:
            resp = self.get(rss_url)
            root = ET.fromstring(resp.content)
            entries = root.findall('.//item')
            if not entries:
                entries = root.findall('.//{http://www.w3.org/2005/Atom}entry')
            for item in entries:
                title = (item.findtext('title') or item.findtext('{http://www.w3.org/2005/Atom}title') or '').strip()
                link = item.findtext('link') or ''
                if not link:
                    node = item.find('{http://www.w3.org/2005/Atom}link')
                    if node is not None:
                        link = node.get('href', '')
                description = item.findtext('description') or item.findtext('{http://www.w3.org/2005/Atom}summary') or ''
                if title and link:
                    rss_offres.append(Offre('oldenburg', title, urljoin(rss_url, link), 'Oldenburg',
                                            normaliser(BeautifulSoup(description, 'html.parser').get_text(' '))))
            log.info('OLDENBURG RSS: %d annonces', len(rss_offres))
        except Exception as exc:
            erreurs.append('RSS: ' + str(exc))
            log.warning('OLDENBURG RSS NON VÉRIFIÉ: %s', exc)
        try:
            resp = self.get(portail_url)
            portail_offres = self.oldenburg_liste_html(resp.text, resp.url)
            # Un portail sans aucune fiche détectée peut signaler un changement HTML.
            if not portail_offres:
                raise RuntimeError('aucune annonce active reconnue; vérifier la structure ou les dates')
            log.info('OLDENBURG PORTAIL: %d annonces non expirées', len(portail_offres))
        except Exception as exc:
            erreurs.append('Portail: ' + str(exc))
            log.warning('OLDENBURG PORTAIL NON VÉRIFIÉ: %s', exc)
        if not rss_offres and not portail_offres:
            raise RuntimeError('RSS et portail indisponibles: ' + '; '.join(erreurs))
        # Le RSS conserve la priorité pour préserver l'URL et les historiques existants.
        fusion, par_numero = {}, {}
        for offre in rss_offres + portail_offres:
            numero = self.oldenburg_jobnummer(offre.url)
            cle = 'job:' + numero if numero else 'url:' + offre.url.split('#')[0].rstrip('/')
            if cle in fusion:
                if offre.texte and offre.texte not in fusion[cle].texte:
                    fusion[cle].texte = (fusion[cle].texte + ' ' + offre.texte).strip()
            else:
                fusion[cle] = offre
        log.info('OLDENBURG FUSION: %d annonces uniques (RSS=%d, portail=%d)',
                 len(fusion), len(rss_offres), len(portail_offres))
        return list(fusion.values())

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
        lignes_identifiees = 0
        lignes_expirees = 0
        for row in soup.select("table tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) < 3:
                continue
            reference = normaliser(txt(cells[0]))
            # V 07/26, F&T 05/26 etc. (la regex originale ratait F&T).
            if not re.fullmatch(r"[A-Z]{1,6}(?:&[A-Z]{1,6})?\s+\d{1,3}/\d{2}", reference, re.I):
                continue
            lignes_identifiees += 1
            m = re.search(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b", txt(cells[2]))
            if m:
                try:
                    if datetime.strptime(m.group(1), "%d.%m.%Y").date() < date.today():
                        lignes_expirees += 1
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
        if offres:
            return offres
        # Zéro n'est fiable que si le portail confirme lui-même l'absence
        # d'offres OU si des références ont bien été trouvées et toutes ont expiré.
        # Ne jamais transformer une modification du HTML ou une erreur JS en « 0 ».
        if lignes_identifiees and lignes_expirees == lignes_identifiees:
            log.info("jade: %d annonce(s) reconnue(s), toutes expirées ; 0 offre active vérifiée", lignes_identifiees)
            return []
        texte_page = normaliser(txt(soup)).casefold()
        absent = re.search(
            r"(?:keine|derzeit keine|aktuell keine|zurzeit keine)\s+"
            r"(?:offenen?\s+|aktuellen?\s+)?"
            r"(?:stellenangebote|stellen|ausschreibungen|jobs)|"
            r"(?:es sind|sind derzeit|sind aktuell)\s+keine\s+(?:stellen|stellenangebote)\s+(?:verf[uü]gbar|ausgeschrieben)",
            texte_page, re.I)
        if absent and re.search(r"tarifbesch[aä]ftigt|stellenangebot|stellen[a-z]*ausschreibung", texte_page, re.I):
            log.info("jade: absence d'offres explicitement indiquée sur le portail ; 0 offre vérifiée")
            return []
        raise RuntimeError(
            f"Jade: liste non vérifiable (références reconnues={lignes_identifiees}, "
            f"expirées={lignes_expirees}); HTML possiblement modifié. Ne pas interpréter comme 0.")

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
        if not items and cle in {"oldenburg", "bremen"}:
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
    # Les annonces peuvent écrire « Master und Promotion » comme deux
    # qualifications obligatoires, sans « abgeschlossene Promotion ».
    m_prerequis = re.search(r"(?:ihr profil|einstellungsvoraussetzungen|voraussetzungen|requirements)(.{0,900})", s)
    if m_prerequis and re.search(
            r"(?:master|diplom|hochschulabschluss).{0,65}\bund\s+(?:eine\s+)?promotion\b",
            m_prerequis.group(1)):
        return "doctorat déjà obtenu exigé"
    if re.search(r"\b(?:postdoc|post-doc|postdoctoral|post-doctoral|postdoktorand\w*)\b", titre.casefold()):
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


def branche_offre(titre):
    """Le type de candidature dépend du POSTE, pas de mots vagues dans le corps."""
    t = normaliser(titre).casefold()
    if re.search(r"(?:wissenschaftlich\w*\s+mitarbeiter|research assistant|research fellow|"
                 r"doctoral|doktorand|promotion\w*stelle|\bphd\b|wissenschaftliche:r|"
                 r"wissenschaftliche\*r|scientific researcher|postdoc)", t):
        return "RECHERCHE"
    return "IT / AUTRE"


def poste_it_elargi_a_examiner(titre, description, cfg):
    """Rattrape des titres IT atypiques sans valider les titres seuls.

    L'exclusion du type Ausbildung, la zone et l'analyse du domaine sont
    appliquées par noter(). Les compétences sont vérifiées dans la description,
    sans compter les mots de l'intitulé.
    """
    regles = cfg.get('intitules_it_elargis', {})
    if not regles.get('actif', False):
        return False, ''
    t = normaliser(titre).casefold()
    if not any(re.search(motif, t, re.I) for motif in regles.get('titres_regex', [])):
        return False, ''
    noms = set(regles.get('indices_techniques', []))
    preuves = [(g['nom'], points_competence(g, description))
               for g in cfg.get('competences', []) if g['nom'] in noms]
    preuves = [(n, points) for n, points in preuves if points > 0]
    total = sum(points for _, points in preuves)
    if total < regles.get('minimum_competences_description', 3):
        return False, ''
    return True, ', '.join(n for n, _ in preuves[:4])



def recherche_it_revue_humaine(titre, description, cfg):
    """Alerte de découverte pour recherche IT rémunérée, sans score minimum.

    Ne prétend NI que le candidat remplit les exigences obligatoires NI que
    la fiche offre une possibilité de Promotion. La description intégrale
    doit être vérifiée par le circuit général avant notification.
    """
    regles = cfg.get('recherche_it_revue_humaine', {})
    if not regles.get('actif', False):
        return False, ''
    titre_normalise = normaliser(titre)
    if not re.search(r'(?:wissenschaftlich.{0,18}mitarbeiter|'
                 r'research (?:associate|assistant|fellow|scientist)|'
                 r'doktorand|doctoral researcher|phd researcher)', titre_normalise, re.I):
        return False, ''
    texte = normaliser(description)
    # Ne pas interpréter le texte institutionnel générique au pied des pages
    # comme une preuve de pertinence de la fiche elle-même.
    en_tete = texte[:1100]
    infos_profil = re.search(
        r'(?:ihr profil|voraussetzungen|einstellungsvoraussetzungen|requirements)'
        r'(.{0,950})', texte, re.I)
    profil = infos_profil.group(1) if infos_profil else ''
    departement_it = re.search(
        r'(?:department (?:für|of) informatik|'
        r'fachbereich\s*3\s*[-–:]?\s*mathematik\s*/\s*informatik|'
        r'institut (?:für|of) informatik|'
        r'fakultät\s*ii\b.{0,85}informatik|'
        r'department of computer science|faculty of computer science)',
        en_tete, re.I)
    diplome_it = re.search(
        r'(?:studium|master|diplom|degree|hochschulabschluss|university degree)'
        r'.{0,140}(?:wirtschaftsinformatik|informatik|computer science|'
        r'information systems|software engineering)', profil, re.I)
    theme_it_titre = re.search(
        r'(?:wirtschaftsinformatik|informatik|computer[- ]science|'
        r'cybersecurity|it[- ]sicherheit|software|cloud|iot|'
        r'information systems|digitalisierte energiesysteme|'
        r'verteilte systeme|automatisierung|computing|'
        r'human[- ]computer interaction|trustworthy ai)',
        titre_normalise, re.I)
    if not (departement_it or diplome_it or theme_it_titre):
        return False, ''
    # Le signal salarial doit venir de la fiche, et non d'une invitation
    # générale de laboratoire ou de la description d'une bourse non salariée.
    remuneration = re.search(
        r'(?:\bvergütung\b.{0,60}\b(?:e|eg)\s*\d{1,2}\b|'
        r'\bentgelt(?:-\s*/\s*besoldungs)?gruppe\s*(?:e|eg)?\s*\d{1,2}\b|'
        r'\b(?:e|eg)\s*\d{1,2}\s*tv[- ]?l\b|'
        r'\btv[- ]?l\b.{0,35}\b(?:e|eg)\s*\d{1,2}\b)',
        texte[:1800], re.I)
    if not remuneration:
        return False, ''
    indices = []
    if departement_it:
        indices.append('département / faculté informatique')
    if diplome_it:
        indices.append('diplôme informatique cité dans les exigences')
    if theme_it_titre:
        indices.append('thème informatique dans le titre')
    return True, ', '.join(indices)



def points_competence(groupe, texte):
    """Une compétence est comptée UNE FOIS, même avec plusieurs synonymes.

    Les variantes de plateforme (VMware 4, Proxmox 5) donnent le MAXIMUM,
    pas l'addition des deux. Même logique pour Veeam et backup générique.
    """
    variants = groupe.get('variantes_points', [])
    points = groupe['points'] if any(occur(texte, w) for w in groupe['synonymes']) else 0
    for variant in variants:
        if any(occur(texte, w) for w in variant['synonymes']):
            points = max(points, variant['points'])
    return points


def bonus_adequation(titre, branche, noms, cfg):
    """Bonus mesuré, uniquement pour une combinaison spécifique et explicite."""
    montants = cfg.get('bonus_specialite', {})
    ts = titre.casefold()
    if branche == 'RECHERCHE' and {'BPM / BPMN', 'IoT'} <= noms:
        return montants.get('recherche_bpm_iot', 0), 'BPM + IoT en recherche'
    cible_admin = re.search(r"(?:system[ -]?admin|it[ -]?admin|linux[ -]?admin|"
                            r"server[ -]?admin|it[ -]?operations|infrastruktur[ -]?admin)", ts)
    techniques = {'Linux / Red Hat / Ubuntu / SUSE', 'Bash / Shell', 'Ansible',
                  'VMware / Proxmox', 'Puppet / Konfigurationsmanagement',
                  'Checkmk / Nagios', 'Monitoring', 'Docker', 'Terraform',
                  'Windows Server', 'Active Directory / GPO', 'Zimbra'}
    if cible_admin and len(techniques & noms) >= 2:
        return montants.get('it_administration', 0), 'administration IT + stack technique'
    return 0, ''



def offre_promotion_remuneree(titre: str, description: str, regles: dict) -> tuple[bool, str]:
    """Voie d'alerte documentaire (PAS un avis d'admissibilité).

    Nécessite 1) référence explicite à la préparation d'un doctorat et
    2) indices concrets d'un CONTRAT SALARIÉ. Pas de filtre ML implicite.
    La détection fonctionne sur la description individuelle, qui doit ensuite
    avoir été vérifiée avant l'envoi d'une notification.
    """
    if not regles.get('active', False):
        return False, ''
    nom = normaliser(titre).casefold()
    texte = normaliser(description).casefold()
    titre_doctoral = re.search(
        r'(?:promotionsstelle|doktorand(?:in)?|doctoral (?:researcher|position|candidate)|'
        r'phd (?:position|student|researcher)|promotionsprojekt)', nom, re.I)
    # Un poste de collaborateur scientifique n'implique pas automatiquement une
    # Promotion : elle doit être annoncée EXPLICITEMENT dans son descriptif.
    titre_scientifique = re.search(
        r'wissenschaftlich.{0,30}mitarbeiter|research (?:assistant|associate|fellow)', nom, re.I)
    promotion_possible = re.search(
        r'(?:möglichkeit|gelegenheit|perspektive|option|unterstützung).{0,45}promotion|'
        r'promotion.{0,45}(?:möglich|unterstützt|ausdrücklich)|'
        r'promotionsmöglichkeit|promotionsperspektive|opportunity to (?:pursue|complete) (?:a )?phd',
        texte, re.I)
    if not (titre_doctoral or (titre_scientifique and promotion_possible)):
        return False, ''
    # Exclure uniquement quelques disciplines clairement éloignées de l'IT,
    # lorsque le titre n'évoque AUCUN projet informatique ou IoT.
    hors_domaine_titre = re.search(
        r'(?<!\w)(?:literatur(?:wissenschaft)?|sprachwissenschaft|geschichte|'
        r'geschichtswissenschaft|philosophie|theologie|kunstgeschichte)(?!\w)', nom, re.I)
    signal_it = re.search(
        r'(?<!\w)(?:informatik|computer|it[- ]|iot|cyber|security|sicherheit|'
        r'digital|software|data|daten|technik)(?!\w)', nom, re.I)
    if hors_domaine_titre and not signal_it:
        return False, ''
    # Filtre de pertinence pour une PREMIÈRE thèse en informatique.
    # Le simple couple « Doktorand + TV-L » ne suffit pas : les annonces
    # de biologie, droit, histoire, pédagogie, etc. doivent rester écartées.
    # On n'attribue au candidat AUCUNE compétence ML sur cette base.
    domaine_hors_it = re.search(
        r'(?:biolog(?:ie|y|isch)|biochim|chemie|chemistry|paleocean|paläo|geo(?:chemie|logie)|'
        r'geochemistry|isotope|rechtswissenschaft|\brechts?\b|law|jurist|'
        r'pädagogik|didaktik|sportwissenschaft|religions|theolog|'
        r'ophthalmolog|augenheilkunde|medizinisch|medical|'
        r'linguistik|sprachwissenschaft|literaturwissenschaft|history|geschichte|'
        r'politik|economic law|wirtschaftswissenschaft|business law)', nom, re.I)
    domaine_informatique = re.search(
        r'(?:informatik|computer[ -]science|computer[ -]interaction|'
        r'information(?:s)?system|information technology|\bit[ -]sicherheit\b|'
        r'cybersecurity|cybersicherheit|\bsecurity\b|'
        r'\biot\b|internet of things|software|programmierung|'
        r'trustworthy[ -]ai|artificial intelligence|künstliche intelligenz|'
        r'machine learning|\bki[- ]forschung\b|\bbpmn?\b|'
        r'cloud|linux|netzwerksicherheit|datenbanksystem|'
        r'automatisierung|automatisierte systeme)', nom, re.I)
    # Un domaine extérieur explicitement nommé ne devient pas IT sous prétexte
    # qu'une discipline utilise également des données ou des logiciels.
    if domaine_hors_it and not domaine_informatique:
        return False, ''
    if not domaine_informatique:
        # Titres vagues : la fiche doit exiger EXPLICITEMENT un diplôme
        # d'informatique / systèmes d'information, pas seulement parler de
        # traitement de données dans les tâches générales.
        master_it_obligatoire = re.search(
            r'(?:einstellungsvoraussetzungen|ihr profil|anforderungsprofil|'
            r'qualifications|requirements|vorausgesetzt|erforderlich)'
            r'.{0,500}?(?:abgeschlossen\w* studium|hochschulabschluss|'
            r'master(?:abschluss|studium| degree)?|degree in)'
            r'.{0,140}?(?:informatik|computer science|information systems|'
            r'wirtschaftsinformatik|it-sicherheit|cybersecurity)', texte, re.I)
        if not master_it_obligatoire:
            return False, ''
    # Indices salariaux vérifiables, plus stricts que le simple mot «funded»
    # (une bourse n'est pas forcément un contrat de travail).
    tvl_grade = re.search(
        r'\b(?:e|eg)\s*[- ]?\s*(?:[1-9]|1[0-5])\s*[a-c]?\s*tv[- ]?l\b|'
        r'\btv[- ]?l\b.{0,35}\b(?:e|eg)\s*[- ]?\s*(?:[1-9]|1[0-5])\b|'
        r'\bentgeltgruppe\s*(?:[1-9]|1[0-5])\b', texte, re.I)
    contrat_salarie = re.search(
        r'(?:vergütet(?:e|er|es|en)?|bezahlt(?:e|er|es|en)?|'
        r'salaried (?:position|employment)|paid (?:employee|position)|'
        r'arbeitsvertrag.{0,60}(?:vergütung|tv[- ]?l|gehalt))', texte, re.I)
    if tvl_grade:
        return True, 'rémunération tarifaire / grille TV-L indiquée'
    if contrat_salarie:
        return True, 'emploi explicitement présenté comme salarié'
    return False, ''


def noter(o, cfg):
    texte = normaliser(o.titre + ' ' + o.texte)
    exclusion = exclure(o.titre, o.texte)
    adequation = evaluer_adequation(o.titre, o.texte, cfg.get('adequation_v7', {}))
    trouve = [(g['nom'], pts) for g in cfg['competences']
              if (pts := points_competence(g, texte)) > 0]
    noms = {nom for nom, _ in trouve}
    branche = branche_offre(o.titre)
    bonus, justification = bonus_adequation(o.titre, branche, noms, cfg)
    skills = min(cfg['plafond_competences'], sum(pts for _, pts in trouve) + bonus)
    lieu, typ = geo(o.ville, texte)
    contrat, cond = points_contrat(texte)
    score = skills + lieu + contrat
    valide = not exclusion and typ != 'HORS ZONE'
    primaire = (valide and score >= cfg['seuil_total']
                and skills >= cfg['minimum_competences'])
    sec = cfg.get('alerte_secondaire', {})
    secondaire = (valide and not primaire and sec.get('active', False)
                  and score >= sec.get('seuil_total', 55)
                  and skills >= sec.get('minimum_competences', 10))
    priorite = 'PRIORITAIRE' if primaire else 'A_EXAMINER' if secondaire else 'ECARTEE'
    promotion_detectee, preuve_paie = offre_promotion_remuneree(
        o.titre, o.texte, cfg.get('promotion_remuneree', {}))
    # Le score technique reste lisible mais n'est plus suffisant à lui seul.
    # Hors profil => pas d'alerte. Incertain => revue humaine, jamais «prioritaire».
    if adequation['action'] == 'HORS_PROFIL':
        primaire = secondaire = False
        priorite = 'ECARTEE'
    elif adequation['action'] == 'EXAMEN' and primaire:
        primaire = False
        secondaire = True
        priorite = 'A_EXAMINER'
    # Circuit indépendant du score habituel : ne PAS déclarer le candidat éligible.
    # Les exclusions existantes, la zone et HORS_PROFIL restent éliminatoires.
    promotion_a_examiner = bool(promotion_detectee and valide and not primaire and
                               adequation['action'] != 'HORS_PROFIL')
    if promotion_a_examiner:
        primaire = False
        secondaire = True
        priorite = 'PROMOTION_A_EXAMINER'
    # Rattrapage prudent des offres IT expérimentées à intitulé inhabituel.
    # Aucun contournement des exclusions, de la zone ou du hors-profil.
    it_elargi, preuves_it = poste_it_elargi_a_examiner(o.titre, o.texte, cfg)
    if (it_elargi and valide and adequation['action'] != 'HORS_PROFIL'
            and not primaire and not promotion_a_examiner):
        secondaire = True
        priorite = 'A_EXAMINER'
    else:
        it_elargi = False
    # Voie supplémentaire : poste de recherche rémunéré dans un domaine
    # informatique, même si le score est faible et sans Promotion annoncée.
    recherche_it, preuve_recherche_it = recherche_it_revue_humaine(o.titre, o.texte, cfg)
    recherche_it = bool(recherche_it and valide and
                        adequation['action'] != 'HORS_PROFIL' and
                        not primaire and not promotion_a_examiner and not it_elargi)
    if recherche_it:
        secondaire = True
        priorite = 'RECHERCHE_IT_A_VERIFIER'
    motifs = []
    if recherche_it:
        motifs.append('RECOMMANDATION : vérification humaine de la recherche IT rémunérée (' +
                      preuve_recherche_it +
                      ') ; les connaissances spécialisées obligatoires peuvent manquer ; '
                      'possibilité de doctorat NON confirmée')
        # Mettre en évidence un prérequis obligatoire signalé par l'annonce.
        obligation = re.search(
            r'[^.!?]{0,160}(?:nachweislich\s+vorkenntnisse|'
            r'vorkenntnisse[^.!?]{0,100}erforderlich)[^.!?]{0,110}',
            normaliser(o.texte), re.I)
        if obligation:
            motifs.append('PRÉREQUIS OBLIGATOIRE À VÉRIFIER : ' +
                          normaliser(obligation.group(0))[:250])
    if it_elargi:
        motifs.append('intitulé IT compatible ; compétences vérifiées dans les missions : ' + preuves_it +
                      ' ; contrôle humain des exigences obligatoires')
    if promotion_a_examiner:
        motifs.append('poste doctoral rémunéré : ' + preuve_paie +
                      ' ; vérifier les prérequis universitaires, le cursus, les compétences obligatoires et la date limite')
    if exclusion: motifs.append(exclusion)
    if adequation['action'] == 'HORS_PROFIL':
        motifs.append('hors spécialité : ' + adequation['specialite'] +
                      ' (missions : ' + ', '.join(adequation['missions_specialisees'][:3]) +
                      ' ; prérequis : ' + ', '.join(adequation['exigences_specialisees'][:3]) + ')')
    if adequation['action'] == 'EXAMEN':
        motifs.append('adéquation à vérifier : ' + adequation['motif'])
    if typ == 'HORS ZONE': motifs.append('hors zone et pas de remote vérifiable')
    if not primaire:
        if skills < cfg['minimum_competences']:
            motifs.append(f"compétences {skills}/{cfg['minimum_competences']}")
        if score < cfg['seuil_total']:
            motifs.append(f"total {score}/{cfg['seuil_total']}")
    if secondaire and not promotion_a_examiner and not recherche_it:
        motifs.append('alerte secondaire : vérification humaine recommandée')
    return {'score':score, 'skills':skills, 'lieu':lieu, 'contrat':contrat,
            'conditions':cond, 'localite':typ, 'categorie':classe(o.titre,o.texte),
            'branche':branche, 'bonus_specialite':bonus, 'justification_bonus':justification,
            'competences':trouve, 'exclusion':exclusion, 'adequation':adequation, 'eligible':primaire,
            'alerte': primaire or secondaire, 'priorite':priorite,
            'voie_promotion':promotion_a_examiner, 'preuve_remuneration':preuve_paie if promotion_a_examiner else '',
            'motifs':motifs}


def decision_apres_verification(offre, note):
    """Aucune alerte si la description individuelle n'a pas été vérifiée."""
    if not offre.detail_verifie:
        note['eligible'] = False
        note['alerte'] = False
        note['priorite'] = 'ECARTEE'
        if 'fiche complète non vérifiée' not in note['motifs']:
            note['motifs'].append('fiche complète non vérifiée')
    return note


# --- Surveillance complémentaire des pages publiques de laboratoires ---
# Une page de laboratoire n'est PAS automatiquement une offre d'emploi :
# elle est consultée, puis seuls les appels explicites à candidatures sont signalés.

def extraits_recrutement_laboratoire(html_source):
    """Renvoie les passages de recrutement, en ignorant menus et pages d'information.

    Ne classe pas un programme de bourses ou une page de formation doctorale
    comme emploi rémunéré ; l'existence d'un salaire est vérifiée séparément.
    """
    soup = BeautifulSoup(html_source, 'html.parser')
    for el in soup.select('script, style, nav, footer, header, aside, form, noscript, '
                          '.breadcrumb, .breadcrumbs, .sidebar, .menu, .navigation'):
        el.decompose()
    root = (soup.select_one('main') or soup.select_one('article') or
            soup.select_one('#content') or soup.select_one('.main-content') or soup.body or soup)
    blocs = []
    # Ne pas inclure les menus/encarts, ni les titres isolés du type « Promotions ».
    for el in root.select('p, li'):
        contenu = normaliser(el.get_text(' ', strip=True))
        if 45 <= len(contenu) <= 2800 and contenu not in blocs:
            blocs.append(contenu)
    # Quelques sites construisent leur contenu dans un seul bloc sans <p>.
    if not blocs:
        texte = normaliser(root.get_text(' ', strip=True))
        if 80 < len(texte) <= 2000:
            blocs = [texte]
    passage_total = ' '.join(blocs).casefold()
    extraits = []
    for bloc in blocs:
        t = bloc.casefold()
        # Un véritable poste doctoral ouvert aux débutants, ou un recrutement
        # explicite pour une activité scientifique avec possibilité de thèse.
        promo = bool(re.search(
            r'promotionsstelle|promotionsmöglichkeit|promotionsperspektive|'
            r'promotion.{0,60}(?:interessiert|interesse|möglich|gegeben)|'
            r'(?:doktorand|doctoral researcher|phd position|phd candidate)', t, re.I))
        embauche = bool(re.search(
            r'wir suchen|gesucht|zu besetzen|stellenangebot|stellenausschreibung|'
            r'offene stelle|vacanc(?:y|ies)|we are hiring|we are looking for|'
            r'bewerben sie sich|bewerbungen.{0,65}(?:willkommen|gerne gesehen)|'
            r'initiativbewerbung', t, re.I))
        poste_recherche = bool(re.search(
            r'wissenschaftlich.{0,25}mitarbeit|research (?:associate|assistant|fellow)|'
            r'phd position|doctoral researcher|promotionsstelle', t, re.I))
        # Certaines pages (Jade) décrivent des modalités de bourses, sans
        # publier d'appel à candidatures ; jamais d'alerte sur cette seule base.
        if not (embauche and (promo or poste_recherche)):
            continue
        if re.search(r'postdoc|postdoctoral|post-doc|habilitation', t, re.I) and not re.search(
                r'promotionsstelle|doktorand|phd position|promotionsmöglichkeit|promotion.{0,45}interessiert', t):
            continue
        if re.search(r'studentische hilfskr[aä]fte|student assistants?', t) and not (promo or poste_recherche):
            continue
        # Pour les candidatures spontanées, une invitation générale dans une
        # AG d'informatique est recevable si elle évoque la carrière scientifique.
        if 'initiativbewerbung' in t and not (promo or poste_recherche):
            if re.search(r'promotion|wissenschaftlich|academic career', passage_total):
                extraits.append(bloc)
        else:
            extraits.append(bloc)
    # Cas AGRA : annonce « eine Stelle ... zu besetzen » suivie dans le
    # paragraphe suivant de « Die Möglichkeit zur Promotion ist gegeben ».
    # Déjà détecté comme poste de recherche ; conserver l'extrait le plus précis.
    return extraits[:4]


def statut_laboratoire(extraits):
    """Aucune preuve de salaire = financement NON confirmé."""
    texte = ' '.join(extraits).casefold()
    preuve_paie = re.search(
        r'(?:e|eg)\s*13\s*tv[- ]?l|tv[- ]?l\s*(?:e|eg)\s*13|'
        r'\b(?:bezahlte|vergütete|salaried) stelle\b|'
        r'\barbeitsvertrag\b.{0,40}\bvergütung\b', texte)
    if preuve_paie:
        return 'Poste salarié annoncé — conditions et éligibilité à vérifier'
    if re.search(r'stelle.{0,80}(?:wissenschaftlich|research)|wissenschaftlich.{0,40}stelle', texte):
        return 'Poste scientifique évoqué — rémunération à confirmer'
    return 'Invitation à candidater — aucune Stelle rémunérée confirmée'


def analyser_pages_laboratoires(collector, cfg, cache, selection_sources=None):
    """Vérifie CHAQUE page à chaque exécution, sans découverte automatique.

    L'état est dans analysed_university_jobs.json, déjà commité par le workflow.
    Un appel identique est signalé une fois ; nouvelle formulation / opportunité
    de recrutement => nouvelle alerte. Échec HTTP => l'état reste intact.
    """
    section = cfg.get('pages_laboratoires', {})
    if not section.get('active', False):
        return [], [], 0
    pages = cache.setdefault('pages_laboratoires', {})
    alertes, erreurs, compte = [], [], 0
    for conf in section.get('pages', []):
        source = conf['source']
        if selection_sources and source not in selection_sources:
            continue
        url = conf['url']
        nom = conf['nom']
        try:
            # Les faux collecteurs des tests peuvent exposer uniquement get().
            lecteur = getattr(collector, 'get_laboratoire', None) or collector.get
            response = lecteur(url)
            if 'pdf' in response.headers.get('Content-Type', '').lower():
                raise RuntimeError('format PDF non pris en charge dans la veille des pages')
            extraits = extraits_recrutement_laboratoire(response.text)
            compte += 1
            identifiant = empreinte(url)[:20]
            ancien = pages.get(identifiant, {})
            signature = empreinte(extraits) if extraits else None
            entree = dict(ancien)
            entree.update({'url': url, 'nom': nom, 'source': source,
                           'dernier_controle': date.today().isoformat(),
                           'signature_contenu_utile': signature})
            pages[identifiant] = entree
            if not extraits:
                log.info('LAB OK %s — aucun appel explicite détecté — %s', source, nom)
            elif signature != ancien.get('derniere_signature_notifiee'):
                alerte = {'id': identifiant, 'nom': nom, 'source': source, 'url': url,
                          'passages': extraits, 'statut': statut_laboratoire(extraits),
                          'signature': signature}
                alertes.append(alerte)
                log.info('LAB NOUVELLE OPPORTUNITÉ À EXAMINER %s — %s', source, nom)
            else:
                log.info('LAB DÉJÀ SIGNALÉ %s — %s', source, nom)
        except Exception as exc:
            log.warning('LAB NON VÉRIFIÉ %s — %s : %s', source, url, exc)
            erreurs.append(f'laboratoire {nom}: {exc}')
    return alertes, erreurs, compte


def emailer(resultats, destinataire, adresse, mot_de_passe, laboratoires=None):
    """Un SEUL e-mail, avec les Promotionsstellen rémunérées bien séparées."""
    laboratoires = laboratoires or []
    msg = EmailMessage()
    promotions = [(o,n) for o,n in resultats if n['priorite'] == 'PROMOTION_A_EXAMINER']
    principaux = [(o,n) for o,n in resultats if n['priorite'] == 'PRIORITAIRE']
    secondaires = [(o,n) for o,n in resultats if n['priorite'] == 'A_EXAMINER']
    recherches_it = [(o,n) for o,n in resultats if n['priorite'] == 'RECHERCHE_IT_A_VERIFIER']
    msg['Subject'] = (f'University Watcher : {len(principaux)} prioritaires, '
                      f'{len(secondaires)} à examiner' +
                      (f', {len(promotions)} promotions rémunérées à examiner' if promotions else '') +
                      (f', {len(recherches_it)} recherches IT à vérifier' if recherches_it else '') +
                      (f', {len(laboratoires)} pages de laboratoires' if laboratoires else ''))
    msg['From'] = adresse
    msg['To'] = destinataire
    lines = ['Offres universitaires — vérification humaine indispensable.',
             "Une alerte doctorale n'atteste PAS que tu remplis les prérequis.",
             "Les postes « RECHERCHE IT À VÉRIFIER » peuvent exiger des connaissances "
             "spécialisées ABSENTES de ton profil ; ils n'offrent pas nécessairement une Promotion.", '']
    groupes = [
        ('PROMOTIONSSTELLEN RÉMUNÉRÉES — À EXAMINER (sans seuil technique)', promotions),
        ('RECHERCHE INFORMATIQUE RÉMUNÉRÉE — VÉRIFICATION HUMAINE RECOMMANDÉE', recherches_it),
        ('AUTRES PRIORITAIRES (60/100 ET 20/55)', principaux),
        ('AUTRES À EXAMINER (55/100 ET 10/55)', secondaires),
    ]
    for titre, ensemble in groupes:
        if not ensemble:
            continue
        lines.extend([titre, '-' * len(titre), ''])
        for o,note in ensemble:
            lines.extend([f"{o.titre} — {note['score']}/100",
                          f"{o.source} — {o.ville} — {note['localite']} — {note['categorie']}",
                          f"Compétences {note['skills']}/55 (bonus {note['bonus_specialite']}) ; "
                          f"lieu {note['lieu']}/30 ; contrat {note['contrat']}/15 ({note['conditions']})",
                          'Correspondances : ' + (', '.join(n for n,_ in note['competences']) or 'aucune'),
                          'Métier principal : ' + note.get('adequation', {}).get('domaine', 'NON_CLASSE'),
                          'Preuves missions : ' + (', '.join((note.get('adequation', {}).get('missions_it', []) +
                            note.get('adequation', {}).get('missions_bpm_iot', []) +
                            note.get('adequation', {}).get('missions_specialisees', []))[:5]) or 'à confirmer'),
                          'Prérequis spécialisés à vérifier : ' +
                            (', '.join(note.get('adequation', {}).get('exigences_specialisees', [])[:5]) or 'aucun signalé'),
                          'Vérification : ' + note.get('adequation', {}).get('motif', ''),
                          'Raison(s) : ' + (' ; '.join(note['motifs']) or 'conditions de score remplies'),
                          f"Annonce : {o.url}", ''])
    if laboratoires:
        titre = 'PAGES DES LABORATOIRES — CONTACT OU POSTE À VÉRIFIER'
        lines.extend([titre, '-' * len(titre),
                      'Ce ne sont PAS nécessairement des emplois vacants ou rémunérés.', ''])
        for lab in laboratoires:
            lines.extend([lab['nom'] + ' (' + lab['source'] + ')', lab['statut'],
                          'Extrait : ' + ' | '.join(lab['passages'])[:1100],
                          'Page : ' + lab['url'], ''])
    msg.set_content('\n'.join(lines))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=25) as smtp:
        smtp.login(adresse, mot_de_passe)
        smtp.send_message(msg)

def empreinte(valeur):
    """Empreinte stable du contenu d'une annonce ou des règles de scoring."""
    valeur = valeur if isinstance(valeur, str) else json.dumps(valeur, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(valeur.encode('utf-8')).hexdigest()


def signature_liste(offre):
    """Détecte un changement visible dans le flux / la liste d'annonces."""
    return empreinte([offre.titre, offre.url, offre.ville, normaliser(offre.texte)])


def charger_analyses(path):
    """Ne jamais effacer silencieusement un cache endommagé."""
    if not path.exists():
        return {"version": 1, "offres": {}}
    contenu = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(contenu, dict) or contenu.get('version') != 1 or not isinstance(contenu.get('offres'), dict):
        raise RuntimeError('Format du cache universitaire inconnu : intervention requise')
    return contenu


def sauvegarder_json(path, contenu):
    temporaire = path.with_name(path.name + '.tmp')
    temporaire.write_text(json.dumps(contenu, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(temporaire, path)


def cache_a_rafraichir(offre, ancien, aujourd_hui, ttl):
    """Nouvelle annonce / changement dans la liste / contrôle périodique.

    Un changement uniquement dans les critères n'impose PAS de requête réseau :
    on peut re-noter sur la description complète déjà enregistrée.
    """
    if not ancien or not ancien.get('detail_verifie') or not ancien.get('detail_texte'):
        return True, 'nouvelle annonce ou détail incomplet'
    if ancien.get('signature_liste') != signature_liste(offre):
        return True, 'liste modifiée'
    try:
        dernier = date.fromisoformat(ancien['dernier_controle'])
    except (ValueError, TypeError, KeyError):
        return True, 'date de contrôle inconnue'
    if (aujourd_hui - dernier).days >= ttl or dernier > aujourd_hui:
        return True, 'revérification périodique'
    return False, 'cache inchangé'


def analyser_une_offre(offre, collector, src, ancien, cfg, aujourd_hui=None):
    """Retourne (note, entrée persistable, situation).

    Une annonce récente inchangée n'est ni téléchargée ni re-notée.
    Si les critères changent, elle est re-notée à partir du texte complet en cache.
    Une fiche dont le téléchargement échoue n'est jamais automatiquement qualifiée.
    """
    aujourd_hui = aujourd_hui or date.today()
    ttl = max(1, int(cfg.get('reverifier_apres_jours', 7)))
    actualiser, raison = cache_a_rafraichir(offre, ancien, aujourd_hui, ttl)
    entree = dict(ancien or {})
    entree.update({'url': offre.url, 'source': offre.source, 'titre': offre.titre,
                   'ville': offre.ville, 'signature_liste': signature_liste(offre),
                   'derniere_presence': aujourd_hui.isoformat()})
    regles = empreinte({'algorithme': 'v7.5-recherche-it-revue-humaine', **{k: cfg.get(k) for k in (
        'competences', 'minimum_competences', 'seuil_total', 'plafond_competences',
        'alerte_secondaire', 'bonus_specialite', 'adequation_v7',
        'promotion_remuneree', 'intitules_it_elargis', 'recherche_it_revue_humaine')}})
    if actualiser:
        if offre.url.split('#')[0] == src['url'].split('#')[0]:
            offre.detail_erreur = 'Le lien renvoie à la liste des annonces'
        else:
            try:
                detail = collector.page_text(offre.url)
                offre.texte += ' ' + detail
                offre.detail_verifie = True
                entree.update({'detail_verifie': True, 'detail_texte': detail,
                               'dernier_controle': aujourd_hui.isoformat(),
                               'empreinte_detail': empreinte(detail)})
                log.info('DETAIL OK %s — %d caractères ; %s', offre.source, len(detail), offre.url[:130])
            except Exception as exc:
                offre.detail_erreur = str(exc)
        if not offre.detail_verifie:
            # Ne pas valider une offre sur la base d'un détail périmé.
            # Au prochain passage, réessayer le téléchargement de cette fiche.
            entree.update({'detail_verifie': False, 'detail_texte': ''})
            log.warning('DETAIL NON VERIFIE %s — %s — %s', offre.source, offre.url[:130], offre.detail_erreur)
    else:
        offre.texte += ' ' + ancien['detail_texte']
        offre.detail_verifie = True
        log.info('CACHE HIT %s — sans nouveau téléchargement : %s', offre.source, offre.titre[:75])

    if (not actualiser and ancien.get('signature_regles') == regles
            and isinstance(ancien.get('note'), dict)):
        note = dict(ancien['note'])
        situation = 'cache réutilisé'
    else:
        note = decision_apres_verification(offre, noter(offre, cfg))
        entree.update({'note': note, 'signature_regles': regles})
        situation = ('analyse du détail' if actualiser else 'nouveaux critères : re-scoring du cache')
    # Ne jamais retenir une ancienne note éligible si la fiche est maintenant inaccessible.
    note = decision_apres_verification(offre, note)
    entree.update({'note': note, 'signature_regles': regles})
    return note, entree, situation


def compte_rendu_sources(sources_ok, sources_ko, erreurs):
    """Signale explicitement les sources dégradées dans GitHub Actions.

    Échec général seulement si TOUTES les sources sont en panne. Une panne
    isolée ne doit plus rendre rouge un run qui a analysé les autres sources,
    mais ne doit pas non plus être présentée comme une collecte complète.
    """
    if sources_ko:
        message = "Sources non vérifiées : " + ", ".join(sources_ko)
        log.warning("SURVEILLANCE PARTIELLE — %s", message)
        # Annotation visible sur la page du run GitHub Actions, même en sortie 0.
        print(f"::warning title=University Watcher - surveillance partielle::{message}", flush=True)
    rapport = os.getenv('GITHUB_STEP_SUMMARY')
    if rapport:
        lignes = ["## University Watcher — bilan des sources", "",
                  f"- Sources vérifiées : {', '.join(sources_ok) or 'aucune'}",
                  f"- Sources non vérifiées : {', '.join(sources_ko) or 'aucune'}",
                  ""]
        if erreurs:
            lignes.append("### Anomalies (une source non vérifiée n'est pas un résultat vide)")
            lignes.extend("- " + str(e).replace('\n', ' ')[:300] for e in erreurs)
        if sources_ko:
            lignes.extend(["", "**Surveillance partielle.** Les sources non vérifiées doivent être réparées ; "
                           "les autres offres ont été traitées."])
        try:
            with open(rapport, 'a', encoding='utf-8') as file:
                file.write('\n'.join(lignes) + '\n')
        except OSError as exc:
            log.warning('Impossible de rédiger le résumé GitHub Actions : %s', exc)


def main():
    ap = argparse.ArgumentParser(description='Offres universitaires : scoring + e-mail + cache')
    ap.add_argument('--dry-run', action='store_true', help='analyse sans envoyer ni enregistrer')
    ap.add_argument('--sources', nargs='*', choices=['oldenburg', 'bremen', 'jade', 'emden'], default=None)
    args = ap.parse_args()
    cfg = json.loads((BASE / 'criteres.json').read_text(encoding='utf-8'))
    etat = BASE / 'seen_university_jobs.json'  # Historique des notifications, conserve
    historiques = BASE / 'analysed_university_jobs.json'  # Nouveau cache d'analyse
    seen = set(json.loads(etat.read_text(encoding='utf-8'))) if etat.exists() else set()
    cache = charger_analyses(historiques)
    collector = Collecteur(cfg)
    offres, erreurs = {}, []
    sources_ok = []
    sources_ko = []
    for key, src in cfg['sources'].items():
        if args.sources and key not in args.sources:
            continue
        try:
            items = collector.collect(key, src)
            sources_ok.append(key)
            log.info('%s: %d offres brutes', key, len(items))
            for item in items:
                if not item.url.startswith('http'):
                    continue
                if item.identifiant in offres:
                    continue
                offres[item.identifiant] = (item, src)
        except Exception as exc:
            log.warning('Source %s NON VÉRIFIÉE : %s', key, exc)
            erreurs.append(f'{key}: {exc}')
            sources_ko.append(key)

    qualifiees = []
    stats = {'cache réutilisé': 0, 'analyse du détail': 0,
             'nouveaux critères : re-scoring du cache': 0}
    incompletes = []
    for identifiant, (offre, src) in offres.items():
        try:
            note, entree, situation = analyser_une_offre(
                offre, collector, src, cache['offres'].get(identifiant), cfg)
            cache['offres'][identifiant] = entree
            stats[situation] += 1
            if not offre.detail_verifie:
                incompletes.append(offre)
            raison = '; '.join(note['motifs']) if note['motifs'] else 'conditions remplies'
            log.info('%3d points (skills %2d) %s — %s — %s — %s [%s] ; DOMAINE=%s ; PREUVES=%s ; EXIGENCES=%s',
                     note['score'], note['skills'], offre.source,
                     note['priorite'], raison,
                     offre.titre[:110], situation,
                     note.get('adequation', {}).get('domaine','NON_CLASSE'),
                     ','.join((note.get('adequation', {}).get('missions_it', []) +
                               note.get('adequation', {}).get('missions_bpm_iot', []) +
                               note.get('adequation', {}).get('missions_specialisees', []))[:4]),
                     ','.join(note.get('adequation', {}).get('exigences_specialisees', [])[:4]))
            if note['alerte'] and identifiant not in seen:
                qualifiees.append((offre, note))
        except Exception as exc:
            log.error('Analyse impossible pour %s : %s', offre.url[:130], exc)
            erreurs.append(f'fiche {offre.source}: {exc}')

    # Pages permanentes des laboratoires : consultation à CHAQUE exécution.
    # Ce circuit est séparé de la notation et de l'historique des offres.
    alertes_lab, erreurs_lab, pages_lab_verifiees = analyser_pages_laboratoires(
        collector, cfg, cache, args.sources)
    erreurs.extend(erreurs_lab)
    if erreurs_lab:
        print(f'::warning title=University Watcher - laboratoires::'
              f'{len(erreurs_lab)} page(s) de laboratoires non vérifiée(s) ; consulter les logs.', flush=True)
    log.info('LABS: %d pages vérifiées, %d nouvelle(s) invitation(s), %d page(s) inaccessible(s)',
             pages_lab_verifiees, len(alertes_lab), len(erreurs_lab))

    # Pour ne pas perdre une Promotionsstelle dans la limite max_offres_email.
    ordre = {'PROMOTION_A_EXAMINER': 4, 'PRIORITAIRE': 3,
             'RECHERCHE_IT_A_VERIFIER': 2, 'A_EXAMINER': 1}
    qualifiees.sort(key=lambda x: (ordre.get(x[1]['priorite'], 0), x[1]['score'], x[1]['skills']), reverse=True)
    nombre = min(len(qualifiees), cfg['max_offres_email'])
    alertes_lab = alertes_lab[:max(0, int(cfg.get('pages_laboratoires', {}).get('max_alertes_email', 10)))]
    log.info('CACHE: %d réutilisées, %d analyses téléchargées, %d re-notées sur critères modifiés',
             stats['cache réutilisé'], stats['analyse du détail'],
             stats['nouveaux critères : re-scoring du cache'])
    log.info('%d offres à notifier (%d prioritaires, %d secondaires), %d dans e-mail ; %d source(s)/fiche(s) en échec ; %d fiche(s) incomplète(s)',
             len(qualifiees), sum(n['priorite']=='PRIORITAIRE' for _,n in qualifiees),
             sum(n['priorite'] in ('A_EXAMINER','PROMOTION_A_EXAMINER','RECHERCHE_IT_A_VERIFIER') for _,n in qualifiees),
             nombre, len(erreurs), len(incompletes))
    if incompletes:
        log.warning('Attention: %d fiches sans texte complet. Vérifier DETAIL NON VERIFIE.', len(incompletes))

    if args.dry_run:
        for offre, note in qualifiees[:nombre]:
            print(f"{note['priorite']} {note['score']:>3}/100 [{offre.source}] {offre.titre}: {offre.url}")
        for lab in alertes_lab:
            print(f"LAB À EXAMINER [{lab['source']}] {lab['nom']}: {lab['url']}")
        compte_rendu_sources(sources_ok, sources_ko, erreurs)
        return 2 if not sources_ok else 0

    # Garder le cache pour 90 jours après disparition des annonces.
    limite = date.today() - timedelta(days=90)
    for identifiant, entree in list(cache['offres'].items()):
        try:
            dernier = date.fromisoformat(entree.get('derniere_presence', ''))
        except ValueError:
            dernier = date.min
        if dernier < limite:
            del cache['offres'][identifiant]

    # En cas d'échec de SMTP, conserver quand même les évaluations dans le cache
    # afin de pouvoir retenter l'e-mail au prochain passage sans re-télécharger.
    if nombre or alertes_lab:
        secrets = ('GMAIL_ADDRESS', 'GMAIL_APP_PASSWORD', 'GMAIL_TO')
        if not all(os.getenv(k) for k in secrets):
            log.error('Secrets manquants : %s', ', '.join(k for k in secrets if not os.getenv(k)))
            sauvegarder_json(historiques, cache)
            return 1
        try:
            emailer(qualifiees[:nombre], os.environ['GMAIL_TO'], os.environ['GMAIL_ADDRESS'],
                    os.environ['GMAIL_APP_PASSWORD'], alertes_lab)
        except Exception:
            sauvegarder_json(historiques, cache)
            raise
        seen.update(offre.identifiant for offre, _ in qualifiees[:nombre])
        for lab in alertes_lab:
            cache['pages_laboratoires'][lab['id']]['derniere_signature_notifiee'] = lab['signature']
        log.info('Notification envoyée ; historique des notifications actualisé (offres et laboratoires)')
    sauvegarder_json(historiques, cache)
    sauvegarder_json(etat, sorted(seen))
    compte_rendu_sources(sources_ok, sources_ko, erreurs)
    if not sources_ok:
        log.error('Aucune source vérifiée : échec global de la surveillance')
        return 2
    return 0

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
