"""Non-régression du tri par expérience et du mail unique Jobs Fetch.

Exécution locale : python -m unittest -v test_job_watcher_compatibilite.py
Aucun accès API, aucun SMTP réel ; pas de modification du fichier seen_jobs.json.
"""
from __future__ import annotations

import argparse
import copy
import json
from email.parser import Parser
from email.header import decode_header
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import job_watcher as w

CFG = w.load_config(Path(__file__).resolve().parent / "config.yaml")


def annonce(titre: str, description: str, ref: str = "TEST-REF", *, lat=53.075, lon=8.807) -> w.JobResult:
    j = w.JobResult(refnr=ref, title=titre, employer="Employeur de test GmbH",
                    ort="Bremen", plz="28195", region="BREMEN", published="2026-11-17",
                    latitude=lat, longitude=lon, description=description,
                    localisations=[{"breite": lat, "laenge": lon,
                                    "adresse": {"ort": "Bremen", "plz": "28195", "region": "BREMEN"}}])
    w.evaluate(j, CFG)
    w.classify_source(j, CFG)
    return j


CAS_17_NOV = [
    ("IT-Administrator (m/w/d) – Windows Server & Active Directory", "VMware Active Directory Windows Server Netzwerktechnik Firewall Microsoft 365", "prioritaire"),
    ("IT-Administrator (m/w/d)", "Proxmox Active Directory Windows Server Grafana", "prioritaire"),
    ("IT-Systemadministrator/in (m/w/d)", "Linux Veeam Windows Server Office 365", "prioritaire"),
    ("Senior Cloud Native Storage Engineer – Ceph", "Linux Ansible Git Python Kubernetes Remote 100%", "hors_profil"),
    ("IT-Systemadministrator*in (m/w/d) / IT-Administrator*in (m/w/d)", "VMware Microsoft 365 Exchange Online", "prioritaire"),
    ("IT-Systemadministrator Windows & Citrix", "Active Directory Windows Server", "prioritaire"),
    ("IT-Systemmanager/ Systemadministrator 1st & 2nd Level Support", "2nd Level Support VMware", "prioritaire"),
    ("Systemadministrator:in", "Linux 2nd Level Support Python", "prioritaire"),
    ("IT-Systemadministrator (m/w/d)", "Linux", "prioritaire"),
    ("Network Infrastructure Engineer (m/w/d)", "Active Directory", "a_examiner"),
    ("(Senior) Network Engineer (m/w/d)", "Linux Bash Ansible GitLab Netzwerkinfrastruktur", "a_examiner"),
    ("DevOps Engineer (alle Geschlechter)", "Linux Ansible Terraform Git Grafana Python Docker Kubernetes", "a_examiner"),
    ("(Senior) DevOps Engineer / Consultant (m/w/d)", "Ansible Terraform Python Docker Kubernetes", "a_examiner"),
    ("Fachinformatiker Systemintegration (m/w/d) vor-Ort-Support", "Windows Server LAN/WAN Support", "prioritaire"),
    ("DevOps Engineer (m/w/d)", "Linux VMware Ansible GitLab Python Docker", "a_examiner"),
    ("IT-Administration und Betreuung von (Fach-)Verfahren/Services", "RedHat Linux Bash MariaDB Python Kubernetes", "prioritaire"),
    ("IT Specialist Workplace Services", "Microsoft 365 Azure AD Intune PowerShell Azure", "a_examiner"),
    ("IT System Specialist (m/w/d) - IT DevOps", "2nd Level Support Docker Kubernetes", "a_examiner"),
    ("IT- Systemmanager 3rd Level", "VMware Active Directory Exchange Server", "prioritaire"),
    ("Datenbankadministrator MongoDB & Linux", "Red Hat Linux MariaDB MongoDB", "a_examiner"),
    ("DevOps Software Engineer", "Linux Git Python Docker", "hors_profil"),
]


class CompatibiliteProfil(unittest.TestCase):
    def test_les_21_cas_du_17_novembre(self):
        self.assertEqual(len(CAS_17_NOV), 21)
        for i, (titre, desc, attendu) in enumerate(CAS_17_NOV):
            with self.subTest(titre=titre):
                j = annonce(titre, desc, f"SIMU-{i}")
                self.assertEqual(j.categorie_profil, attendu, (titre, j.motifs_profil))
                self.assertFalse(j.eliminated)

    def test_ceph_score_eleve_mais_hors_profil(self):
        j = annonce("Senior Cloud Native Storage Engineer – Ceph", "Linux Ansible Git Python Kubernetes")
        self.assertGreater(j.score, 12)
        self.assertEqual(j.categorie_profil, "hors_profil")
        self.assertEqual(j.priorite_metier, "À examiner")
        self.assertIn("Senior Cloud Native Storage", " ".join(j.motifs_profil))

    def test_bkk_troisieme_niveau_prioritaire(self):
        j = annonce("IT-Systemmanager 3rd Level", "VMware Active Directory Exchange Online")
        self.assertEqual(j.priorite_metier, "Très adapté")
        self.assertEqual(j.categorie_profil, "prioritaire")

    def test_backup_classique_mais_pas_cloud_native(self):
        a = annonce("Infrastructure Specialist Storage & Backup", "Veeam VMware")
        b = annonce("Senior Cloud Native Storage Engineer – Ceph", "Linux Veeam VMware")
        self.assertEqual(a.categorie_profil, "prioritaire")
        self.assertEqual(b.categorie_profil, "hors_profil")

    def test_exigence_avancee_obligatoire_mais_pas_optionnelle(self):
        obligatoire = annonce("IT-Systemadministrator Linux", "Linux Server. Fundierte Kenntnisse Kubernetes erforderlich.")
        optionnel = annonce("IT-Systemadministrator Linux", "Linux Server. Kubernetes wünschenswert.")
        self.assertEqual(obligatoire.categorie_profil, "a_examiner")
        self.assertEqual(optionnel.categorie_profil, "prioritaire")

    def test_m365_only_vs_m365_plus_ad(self):
        nouveau = annonce("IT Specialist Workplace Engineer", "Microsoft 365, Intune, Azure")
        hybride = annonce("IT-Systemadministrator Windows & M365", "Windows Server, Active Directory, Intune")
        self.assertEqual(nouveau.categorie_profil, "a_examiner")
        self.assertEqual(hybride.categorie_profil, "prioritaire")

    def test_pas_de_suppression_et_seuils_inchanges(self):
        self.assertEqual(CFG["criteres"]["seuil_points"], 12)
        self.assertEqual(CFG["criteres"]["seuil_points_cibles"], 5)
        j = annonce("DevOps Engineer", "Linux Ansible Terraform Python Docker")
        self.assertGreaterEqual(j.score, 12)
        self.assertEqual(j.categorie_profil, "a_examiner")
        self.assertIn("Compatibilité profil : À EXAMINER", "\n".join(w._lignes_offre(j)))

    def test_allemand_c1_est_toujours_elimine(self):
        j = annonce("Linux Administrator", "Linux. Deutsche Sprachkenntnisse C1 erforderlich")
        self.assertTrue(j.eliminated)

    def test_mail_unique_deux_sections_smtp_factice(self):
        a = annonce("Linux Administrator", "Linux", "P")
        b = annonce("DevOps Engineer", "Linux Ansible Terraform Python Docker", "S")
        c = annonce("Senior Cloud Native Storage Engineer – Ceph", "Linux Kubernetes", "H")
        messages = []
        class SMTPFactice:
            def __init__(self, host, port, timeout): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def starttls(self): pass
            def login(self, *args): pass
            def sendmail(self, addr, rec, msg): messages.append(msg)
        env = {"GMAIL_ADDRESS": "fake@example.invalid", "GMAIL_TO": "test@example.invalid",
               "GMAIL_APP_PASSWORD": "dummy"}
        with patch.object(w.smtplib, "SMTP", SMTPFactice), patch.dict(w.os.environ, env):
            self.assertTrue(w.send_email(CFG, [a, b]))
        self.assertEqual(len(messages), 1)
        parsed = Parser().parsestr(messages[0])
        segments = decode_header(parsed["Subject"])
        subject = "".join(x.decode(charset or "utf-8") if isinstance(x, bytes)
                          else x for x, charset in segments)
        self.assertIn("1 prioritaire(s), 1 à examiner", subject)
        body = parsed.get_payload()[0].get_payload(decode=True).decode("utf-8")
        self.assertIn("OFFRES PRIORITAIRES (1)", body)
        self.assertIn("OFFRES À EXAMINER (1)", body)
        self.assertLess(body.index("OFFRES PRIORITAIRES"),body.index("OFFRES À EXAMINER"))
        self.assertNotIn(c.title, body)

    def test_hors_profil_precis_et_pas_faux_positif(self):
        hors = annonce("Senior Network Engineer", "Fundierte Kenntnisse in BGP erforderlich. Linux.")
        prudent = annonce("Senior Network Engineer", "BGP wünschenswert. Linux.")
        generaliste = annonce("DevOps Engineer", "Linux Ansible Python Terraform")
        infra = annonce("IT-Systemadministrator Linux", "Kubernetes wünschenswert. Linux Server")
        devops_expert = annonce("DevOps Engineer", "Fundierte Erfahrung CI/CD erforderlich. "
                               "Sehr gute Kenntnisse Kubernetes und Terraform erforderlich.")
        self.assertEqual(hors.categorie_profil, "hors_profil")
        self.assertEqual(prudent.categorie_profil, "a_examiner")
        self.assertEqual(generaliste.categorie_profil, "a_examiner")
        self.assertEqual(infra.categorie_profil, "prioritaire")
        self.assertEqual(devops_expert.categorie_profil, "hors_profil")

    def test_head_it_operations_reste_prioritaire(self):
        j = annonce("Head of IT Operations", "Linux VMware Windows Server")
        self.assertEqual(j.categorie_profil, "prioritaire")


class SauvegardeEtEnvoi(unittest.TestCase):
    def _jouer(self, etat, messages, reponses, *, dry_run=False, references=None):
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp) / "seen_jobs.json"
            st.write_text(json.dumps(etat), encoding="utf-8")
            cfg = copy.deepcopy(CFG)
            cfg["etat"] = {"fichier": str(st)}
            offre = lambda ref,titre: {"referenznummer": ref, "stellenangebotsTitel": titre,
                     "firma": "Société " + ref,
                     "datumErsteVeroeffentlichung": "2026-11-17",
                     "stellenlokationen": [{"breite": 53.075, "laenge": 8.807,
                        "adresse": {"ort": "Bremen", "plz": "28195", "region": "BREMEN"}}]}
            annonces = [offre("PRIMARY", "Linux Administrator"),
                        offre("SECONDARY", "DevOps Engineer"),
                        offre("EXCLUDED", "Senior Cloud Native Storage Engineer – Ceph")]
            if references is not None:
                annonces = [j for j in annonces if j["referenznummer"] in references]
            def texte(_session, ref):
                if ref == "PRIMARY": return "Linux Windows Server VMware"
                if ref == "SECONDARY": return "Linux Terraform Python Docker"
                return "Linux Ansible Git Python Kubernetes"
            args = argparse.Namespace(config=Path("config.yaml"), dry_run=dry_run,
                    reset=False, review_seen=False, review_limit=300)
            def ecrire_email(*a, **kwargs):
                messages.append([j.refnr for j in a[1]])
                return reponses.pop(0)
            with patch.object(w,"parse_args",return_value=args), \
                 patch.object(w,"load_config",return_value=cfg), \
                 patch.object(w,"search_jobs",return_value=annonces), \
                 patch.object(w,"fetch_description",side_effect=texte), \
                 patch.object(w,"load_recent_companies",return_value=[]), \
                 patch.object(w,"send_email",side_effect=ecrire_email), \
                 patch.object(w.time,"sleep"):
                w.main()
            return json.loads(st.read_text())

    def test_un_mail_pour_deux_sections_et_hors_profil_archive(self):
        appels = []
        etat = self._jouer({"vus": {}}, appels, [True])
        self.assertEqual(appels, [["PRIMARY", "SECONDARY"]])
        self.assertEqual(set(etat["vus"]), {"PRIMARY", "SECONDARY", "EXCLUDED"})
        self.assertEqual(len(etat["notifications"]), 2)
        self.assertNotIn("EXCLUDED", [n.get("refnr") for n in etat["notifications"]])

    def test_smtp_en_echec_retentable_et_hors_profil_archive(self):
        appels = []
        etat = self._jouer({"vus": {}}, appels, [False])
        self.assertEqual(len(appels), 1)
        self.assertNotIn("PRIMARY", etat["vus"])
        self.assertNotIn("SECONDARY", etat["vus"])
        self.assertIn("EXCLUDED", etat["vus"])
        self.assertFalse(etat.get("notifications"))

    def test_hors_profil_seul_aucun_email(self):
        appels = []
        etat = self._jouer({"vus": {}}, appels, [], references={"EXCLUDED"})
        self.assertEqual(appels, [])
        self.assertEqual(set(etat["vus"]), {"EXCLUDED"})
        self.assertFalse(etat.get("notifications"))

    def test_dry_run_ne_change_pas_historique(self):
        appels = []
        etat = self._jouer({"vus": {}}, appels, [], dry_run=True)
        self.assertEqual(etat, {"vus": {}})
        self.assertEqual(appels, [])

if __name__ == "__main__":
    unittest.main()
