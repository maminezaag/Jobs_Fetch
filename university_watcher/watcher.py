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
    regles = empreinte({k: cfg[k] for k in ('competences', 'minimum_competences',
                                            'seuil_total', 'plafond_competences')})
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
    for key, src in cfg['sources'].items():
        if args.sources and key not in args.sources:
            continue
        try:
            items = collector.collect(key, src)
            log.info('%s: %d offres brutes', key, len(items))
            for item in items:
                if not item.url.startswith('http'):
                    continue
                if item.identifiant in offres:
                    continue
                offres[item.identifiant] = (item, src)
        except Exception as exc:
            log.error('Source %s non analysée : %s', key, exc)
            erreurs.append(f'{key}: {exc}')

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
            log.info('%3d points (skills %2d) %s — %s — %s — %s [%s]',
                     note['score'], note['skills'], offre.source,
                     'QUALIFIEE' if note['eligible'] else 'ECARTEE', raison,
                     offre.titre[:110], situation)
            if note['eligible'] and identifiant not in seen:
                qualifiees.append((offre, note))
        except Exception as exc:
            log.error('Analyse impossible pour %s : %s', offre.url[:130], exc)
            erreurs.append(f'fiche {offre.source}: {exc}')

    qualifiees.sort(key=lambda x: x[1]['score'], reverse=True)
    nombre = min(len(qualifiees), cfg['max_offres_email'])
    log.info('CACHE: %d réutilisées, %d analyses téléchargées, %d re-notées sur critères modifiés',
             stats['cache réutilisé'], stats['analyse du détail'],
             stats['nouveaux critères : re-scoring du cache'])
    log.info('%d offres qualifiées non notifiées, %d pour e-mail ; %d source(s)/fiche(s) en échec ; %d fiche(s) incomplète(s)',
             len(qualifiees), nombre, len(erreurs), len(incompletes))
    if incompletes:
        log.warning('Attention: %d fiches sans texte complet. Vérifier DETAIL NON VERIFIE.', len(incompletes))

    if args.dry_run:
        for offre, note in qualifiees[:nombre]:
            print(f"{note['score']:>3}/100 [{offre.source}] {offre.titre}: {offre.url}")
        if erreurs:
            return 2
        return 0

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
    if nombre:
        secrets = ('GMAIL_ADDRESS', 'GMAIL_APP_PASSWORD', 'GMAIL_TO')
        if not all(os.getenv(k) for k in secrets):
            log.error('Secrets manquants : %s', ', '.join(k for k in secrets if not os.getenv(k)))
            sauvegarder_json(historiques, cache)
            return 1
        try:
            emailer(qualifiees[:nombre], os.environ['GMAIL_TO'], os.environ['GMAIL_ADDRESS'],
                    os.environ['GMAIL_APP_PASSWORD'])
        except Exception:
            sauvegarder_json(historiques, cache)
            raise
        seen.update(offre.identifiant for offre, _ in qualifiees[:nombre])
        log.info('Notification envoyée ; historique des notifications actualisé')
    sauvegarder_json(historiques, cache)
    sauvegarder_json(etat, sorted(seen))
    if erreurs:
        print('ATTENTION : sources non vérifiées : ' + ' | '.join(erreurs), file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
