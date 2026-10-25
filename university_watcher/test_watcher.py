"""Tests isolés : ne consultent ni les sites ni Gmail."""
import json
from datetime import date, timedelta
from pathlib import Path
import unittest
from unittest.mock import Mock
from watcher import Offre, Collecteur, exclure, geo, noter, points_contrat

CFG = json.loads((Path(__file__).parent / "criteres.json").read_text(encoding="utf-8"))

class ScoringTests(unittest.TestCase):
    def test_local_bad_zwischenahn(self):
        self.assertEqual(geo("Bad Zwischenahn", "")[:2], (25, "LOCAL"))

    def test_local_oldenburg_bremen(self):
        self.assertEqual(geo("Oldenburg", "")[0], 30)
        self.assertEqual(geo("Bremen", "")[0], 28)

    def test_remote(self):
        self.assertEqual(geo("Munich", "100% remote")[0], 20)
        self.assertEqual(geo("Munich", "Präsenz vor Ort")[1], "HORS ZONE")

    def test_synonyms_count_once(self):
        offer = Offre("oldenburg", "Linux RHEL Ubuntu", "https://uol.de/job1", "Oldenburg")
        notes = noter(offer, CFG)
        self.assertEqual(notes["skills"], 5)

    def test_exclusions(self):
        self.assertTrue(exclure("Sysadmin", "Mindestens C1 Deutschkenntnisse erforderlich"))
        self.assertTrue(exclure("Sysadmin", "Verhandlungssichere Deutsch- und gute Englischkenntnisse"))
        self.assertFalse(exclure("Sysadmin", "Verhandlungssichere Deutschkenntnisse, Niveau B2 ausreichend"))
        self.assertFalse(exclure("Sysadmin", "Deutsch C1 wünschenswert, B2 ausreichend"))
        self.assertFalse(exclure("Doktorand IoT", "Gelegenheit zur Promotion"))
        self.assertTrue(exclure("Postdoc", "Linux"))
        self.assertFalse(exclure("Admin", "Verhandlungssicheres Englisch und Deutsch B2"))
        self.assertFalse(exclure("Admin", "Englisch C1 erforderlich, Deutsch B2 ausreichend"))
        self.assertTrue(exclure("Admin", "Verhandlungssichere Deutschkenntnisse. Englisch B2 erforderlich"))

    def test_contract(self):
        self.assertEqual(points_contrat("TV-L E13 75%")[0], 13)
        self.assertEqual(points_contrat("TV-L E13 Vollzeit")[0], 15)

    def test_science_relevance_and_email(self):
        offer = Offre("bremen", "Wissenschaftliche:r Mitarbeiter:in IoT und BPM", "https://uni-bremen.de/job1", "Bremen",
                      "BPMN, Informationssysteme, Linux, Prozessautomatisierung, MySQL. E13 TV-L, 75%. Deutsch B2 ausreichend")
        n = noter(offer, CFG)
        self.assertTrue(n["eligible"])
        self.assertGreaterEqual(n["score"],60)
        self.assertGreaterEqual(n["skills"],20)

    def test_exclusion_override_score(self):
        offer = Offre("oldenburg", "IT Systemadministrator Linux VMware Puppet", "https://uol.de/job2", "Oldenburg",
                      "Checkmk, Docker, Ansible, Bash, Terraform, Windows Server, 100%, E13 TV-L; zwingend Deutsch C2")
        n = noter(offer, CFG)
        self.assertFalse(n["eligible"])
        self.assertGreater(n["score"],60)

class ParsersTests(unittest.TestCase):
    def setUp(self):
        self.c = Collecteur(CFG)

    def test_rss(self):
        xml = b'<rss><channel><item><title>Wissenschaftliche Mitarbeiterin BPM</title><link>https://uol.de/job123</link><description>IoT</description></item></channel></rss>'
        self.c.get = Mock(return_value=Mock(content=xml))
        jobs = self.c.collect("oldenburg",CFG["sources"]["oldenburg"])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].titre,"Wissenschaftliche Mitarbeiterin BPM")

    def test_bremen(self):
        html = '<a href="/job-xyz">Bewerbungsfrist 11.11.2026 Neu: Informatiker:in Linux — Kennziffer: A01-26</a>'
        self.c.get = Mock(return_value=Mock(text=html))
        jobs = self.c.collect("bremen",CFG["sources"]["bremen"])
        self.assertEqual(len(jobs), 1)
        self.assertIn("Informatiker", jobs[0].titre)

    def test_coveto_no_false_empty(self):
        self.assertFalse(self.c.coveto_indique_aucune_offre("Stellenname Arbeitsort Kategorie"))
        self.assertTrue(self.c.coveto_indique_aucune_offre("Keine Stellenangebote"))

    def test_coveto_empty_header_structure(self):
        html = '<table><thead><tr><th>Stellenname</th><th>Arbeitsort</th><th>Kategorie</th></tr></thead><tbody></tbody></table>'
        self.assertTrue(self.c.coveto_table_reconnue(html))
        self.assertEqual(self.c.coveto_offres_depuis_html(html, "https://k60540.coveto.de/public/jobs/"), [])

    def test_coveto_unrelated_table_not_accepted(self):
        html = '<table><tr><th>Name</th><th>Stadt</th></tr></table>'
        self.assertFalse(self.c.coveto_table_reconnue(html))

    def test_coveto_loading_indicator(self):
        self.assertTrue(self.c.coveto_semble_charger("Bitte warten, Daten werden geladen"))
        self.assertFalse(self.c.coveto_semble_charger("Stellenname Arbeitsort Kategorie"))

    def test_coveto_html_table(self):
        html = '<table><tr><th>Stellenname</th><th>Arbeitsort</th></tr><tr><td><a href="/job-test-123.html">Wissenschaftlicher Mitarbeiter IoT</a></td><td>Leer</td><td>Technik</td></tr></table>'
        results = self.c.coveto_offres_depuis_html(html, "https://k60540.coveto.de/public/jobs/")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].ville, "Leer")
        self.assertIn("job-test-123.html", results[0].url)

    def test_jade_expired_and_ampersand_reference(self):
        expired = (date.today() - timedelta(days=3)).strftime("%d.%m.%Y")
        active = (date.today() + timedelta(days=10)).strftime("%d.%m.%Y")
        html = f'<table><tr><td>V 07/26</td><td><a href="/expired">Ancien Linux</a></td><td>{expired}</td></tr><tr><td>F&T 05/26</td><td><a href="/open">IoT und BPM</a></td><td>{active}</td></tr></table>' 
        self.c.get = Mock(return_value=Mock(text=html))
        jobs = self.c.collect("jade", CFG["sources"]["jade"])
        self.assertEqual(len(jobs), 1)
        self.assertIn('/open', jobs[0].url)

    def test_jade(self):
        html = '<table><tr><th>Kennziffer</th><th>Beschreibung</th></tr><tr><td>V 07/26</td><td><a href="/doc.pdf">Linux Informatiker E11 TV-L 100%</a></td><td>31.12.2026</td></tr></table>'
        self.c.get = Mock(return_value=Mock(text=html))
        jobs = self.c.collect("jade",CFG["sources"]["jade"])
        self.assertEqual(len(jobs),1)
        self.assertIn('doc.pdf', jobs[0].url)

if __name__ == "__main__": unittest.main()
