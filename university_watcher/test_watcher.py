"""Tests isolés : ne consultent ni les sites ni Gmail."""
import json
from datetime import date, timedelta
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import tempfile
import os
import watcher
from watcher import Offre, Collecteur, exclure, geo, noter, points_contrat, decision_apres_verification, signature_liste, cache_a_rafraichir, analyser_une_offre, empreinte, charger_analyses, sauvegarder_json

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

    def test_c1_language_requirement_real_bremen_formulation(self):
        # L'expression exacte observée dans une fiche publique de Brême.
        t = "Sichere Beherrschung der deutschen Sprache in Wort und Schrift (mind. CEF-Level C1)"
        self.assertEqual(exclure("Informatiker:in", t), "allemand C1/C2 requis")

    def test_english_c1_does_not_reject_german_b2(self):
        self.assertFalse(exclure("IT Admin", "Englisch (mind. CEF-Level C1); Deutsch B2 ausreichend"))

    def test_optional_c1_not_rejected(self):
        self.assertFalse(exclure("Sysadmin", "Deutsch C1 wünschenswert, B2 ausreichend"))

    def test_real_bremen_job_is_excluded_even_if_good_technical_score(self):
        texte = ("EG 13 TV-L Teilzeit 50%. Administration eines Rechenservers; "
                 "Datensicherung und Backup, Linux, Python, Datenbanken; "
                 "Sichere Beherrschung der deutschen Sprache in Wort und Schrift (mind. CEF-Level C1)")
        offer = Offre("bremen", "Informatiker:in", "https://uni-bremen.de/job/4543", "Bremen", texte)
        n = noter(offer, CFG)
        self.assertGreaterEqual(n["skills"], 20)
        self.assertFalse(n["eligible"])
        self.assertEqual(n["exclusion"], "allemand C1/C2 requis")

    def test_contract_50pct_not_misread_vollzeit_salary_comparison(self):
        contract = ("EG 13 TV-L, Teilzeit 50%. "
                    "Jahresbruttogehalt für eine Vollzeitstelle 57.112 Euro")
        self.assertEqual(points_contrat(contract), (11, "TV-L E13, 50%"))

    def test_no_email_without_complete_description(self):
        offer = Offre("oldenburg", "IT-Systemadministrator Linux Puppet VMware Docker",
                      "https://uol.de/jobs/test", "Oldenburg",
                      "Backup, Checkmk, IoT, BPM, 100%, EG 13 TV-L")
        result = noter(offer, CFG)
        self.assertTrue(result["eligible"])
        decision_apres_verification(offer, result)
        self.assertFalse(result["eligible"])
        self.assertIn("fiche complète non vérifiée", result["motifs"])
        offer.detail_verifie = True
        result2 = decision_apres_verification(offer, noter(offer, CFG))
        self.assertTrue(result2["eligible"])

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

    def test_individual_detail_in_main_excludes_navigation(self):
        html = ("<html><nav>Linux VMware Puppet</nav><main><h1>Informatiker:in</h1>"
                "<h2>Stellenbeschreibung</h2><p>" + "Aufgaben für Softwareentwicklung und Datensicherung. " * 5
                + "Deutsch mindestens C1 ist eine zwingende Voraussetzung.</p></main></html>")
        self.c.get = Mock(return_value=Mock(text=html, headers={"Content-Type": "text/html"},
                                            url="https://www.uni-bremen.de/job/4543",content=html.encode()))
        detail = self.c.page_text("https://www.uni-bremen.de/job/4543")
        self.assertIn("Deutsch mindestens C1", detail)
        self.assertNotIn("VMware Puppet", detail)

    def test_missing_details_raise_clear_error(self):
        html = "<html><main>Seite nicht gefunden.</main></html>"
        self.c.get = Mock(return_value=Mock(text=html, headers={"Content-Type": "text/html"},
                                            url="https://www.uni-bremen.de/job/4543",content=html.encode()))
        with self.assertRaisesRegex(RuntimeError, "Description individuelle"):
            self.c.page_text("https://www.uni-bremen.de/job/4543")

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

class HistoryV5Tests(unittest.TestCase):
    def setUp(self):
        self.src = CFG["sources"]["oldenburg"]
        self.today = date(2026, 10, 25)
        self.offre = Offre("oldenburg", "IT-Systemadministrator Linux", "https://uol.de/jobs/it1", "Oldenburg", "IoT")
        self.c = Mock()
        self.c.page_text.return_value = "Linux, Puppet, Checkmk, VMware, BPM, IoT, TV-L E13, Vollzeit. Deutsch B2 ausreichend"

    def premiere_analyse(self):
        note, entry, status = analyser_une_offre(self.offre, self.c, self.src, None, CFG, self.today)
        self.assertTrue(note["eligible"])
        self.assertEqual(status, "analyse du détail")
        self.assertTrue(entry["detail_verifie"])
        self.assertEqual(self.c.page_text.call_count, 1)
        return entry

    def test_initial_is_downloaded(self):
        self.premiere_analyse()

    def test_same_listing_skips_network_and_scoring(self):
        old = self.premiere_analyse()
        new_offre = Offre("oldenburg", "IT-Systemadministrator Linux", self.offre.url, "Oldenburg", "IoT")
        result, entry, status = analyser_une_offre(new_offre, self.c, self.src, old, CFG, self.today+timedelta(days=1))
        self.assertEqual(self.c.page_text.call_count, 1, "Pas de second téléchargement")
        self.assertEqual(status, "cache réutilisé")
        self.assertTrue(result["eligible"])
        self.assertEqual(entry["dernier_controle"], self.today.isoformat())

    def test_list_changed_forces_recheck(self):
        old = self.premiere_analyse()
        second = Offre("oldenburg", self.offre.titre, self.offre.url, "Oldenburg", "IoT : nouveau projet")
        _, entry, reason = analyser_une_offre(second, self.c, self.src, old, CFG, self.today+timedelta(days=1))
        self.assertEqual(self.c.page_text.call_count, 2)
        self.assertEqual(reason, "analyse du détail")
        self.assertEqual(entry["dernier_controle"], (self.today+timedelta(days=1)).isoformat())

    def test_after_7_days_forces_recheck(self):
        old = self.premiere_analyse()
        second = Offre("oldenburg", self.offre.titre, self.offre.url, "Oldenburg", "IoT")
        _, _, reason = analyser_une_offre(second, self.c, self.src, old, CFG, self.today+timedelta(days=7))
        self.assertEqual(self.c.page_text.call_count, 2)
        self.assertEqual(reason, "analyse du détail")

    def test_criteria_change_rescores_without_reloading(self):
        old = self.premiere_analyse()
        cfg_changed = dict(CFG)
        cfg_changed["seuil_total"] = 101
        second = Offre("oldenburg", self.offre.titre, self.offre.url, "Oldenburg", "IoT")
        note, entry, status = analyser_une_offre(second, self.c, self.src, old, cfg_changed, self.today+timedelta(days=1))
        self.assertEqual(self.c.page_text.call_count, 1)
        self.assertEqual(status, "nouveaux critères : re-scoring du cache")
        self.assertFalse(note["eligible"])
        self.assertNotEqual(entry["signature_regles"], old["signature_regles"])

    def test_missing_details_retried_next_time(self):
        self.c.page_text.side_effect = RuntimeError("PDF inaccessible")
        _, entry, _ = analyser_une_offre(self.offre, self.c, self.src, None, CFG, self.today)
        self.assertFalse(entry["detail_verifie"])
        self.c.page_text.side_effect = None
        self.c.page_text.return_value = "Linux, Puppet, Checkmk, VMware, BPM, IoT, TV-L E13, Vollzeit"
        second = Offre("oldenburg", self.offre.titre, self.offre.url, "Oldenburg", "IoT")
        note, refreshed, _ = analyser_une_offre(second, self.c, self.src, entry, CFG, self.today+timedelta(days=1))
        self.assertEqual(self.c.page_text.call_count, 2)
        self.assertTrue(refreshed["detail_verifie"])
        self.assertTrue(note["eligible"])

    def test_stale_fetch_failure_blocks_cached_alert(self):
        old = self.premiere_analyse()
        self.c.page_text.side_effect = RuntimeError("Erreur HTTP")
        second = Offre("oldenburg", self.offre.titre, self.offre.url, "Oldenburg", "IoT")
        note, entry, _ = analyser_une_offre(second, self.c, self.src, old, CFG, self.today+timedelta(days=7))
        self.assertFalse(note["eligible"])
        self.assertFalse(entry["detail_verifie"])
        self.assertIn('fiche complète non vérifiée', note['motifs'])

    def test_cache_persistent_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/"analysed_university_jobs.json"
            self.assertEqual(charger_analyses(f), {"version":1,"offres":{}})
            value = {"version":1,"offres":{"id1":{"titre":"Techniker"}}}
            sauvegarder_json(f, value)
            self.assertEqual(charger_analyses(f), value)

    def test_cache_invalid_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/"analysed_university_jobs.json"
            f.write_text('[]')
            with self.assertRaises(RuntimeError):
                charger_analyses(f)

    def test_unchanged_identity_is_stable(self):
        other = Offre(self.offre.source, self.offre.titre, self.offre.url+'#ancre', self.offre.ville, self.offre.texte)
        self.assertEqual(self.offre.identifiant, other.identifiant)

class IntegrationHistoryTests(unittest.TestCase):
    def test_two_runs_only_download_once_and_email_once(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            cfg = dict(CFG)
            cfg['sources'] = {'oldenburg': CFG['sources']['oldenburg']}
            (base/'criteres.json').write_text(json.dumps(cfg, ensure_ascii=False))
            (base/'seen_university_jobs.json').write_text('[]')
            collector = Mock()
            def collect(*args):
                return [Offre('oldenburg','IT-Systemadministrator Linux', 'https://uol.de/jobs/it1','Oldenburg','IoT')]
            collector.collect.side_effect = collect
            collector.page_text.return_value = 'Linux, Puppet, Checkmk, VMware, BPM, IoT, TV-L E13, Vollzeit. Deutsch B2 ausreichend'
            with (patch.object(watcher,'BASE',base), patch.object(watcher,'Collecteur',return_value=collector),
                  patch.object(watcher,'emailer') as send,
                  patch.dict(os.environ, {'GMAIL_ADDRESS':'test@example.com',
                                          'GMAIL_APP_PASSWORD':'test-password',
                                          'GMAIL_TO':'dest@example.com'}),
                  patch('sys.argv',['watcher.py'])):
                self.assertEqual(watcher.main(),0)
                self.assertEqual(watcher.main(),0)
                self.assertEqual(collector.page_text.call_count,1)
                self.assertEqual(send.call_count,1)
                hist = json.loads((base/'seen_university_jobs.json').read_text())
                analyses = json.loads((base/'analysed_university_jobs.json').read_text())
                self.assertEqual(len(hist),1)
                self.assertEqual(len(analyses['offres']),1)

    def test_change_criteria_notifies_new_match_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            cfg=dict(CFG)
            cfg['sources']={'oldenburg':CFG['sources']['oldenburg']}
            cfg['seuil_total']=101
            (base/'criteres.json').write_text(json.dumps(cfg,ensure_ascii=False))
            (base/'seen_university_jobs.json').write_text('[]')
            collector=Mock()
            collector.collect.side_effect=lambda *args: [Offre('oldenburg','IT-Systemadministrator Linux','https://uol.de/jobs/it1','Oldenburg','IoT')]
            collector.page_text.return_value='Linux, Puppet, Checkmk, VMware, BPM, IoT, TV-L E13, Vollzeit. Deutsch B2 ausreichend'
            with (patch.object(watcher,'BASE',base), patch.object(watcher,'Collecteur',return_value=collector),
                  patch.object(watcher,'emailer') as send,
                  patch.dict(os.environ, {'GMAIL_ADDRESS':'test@example.com', 'GMAIL_APP_PASSWORD':'fake', 'GMAIL_TO':'dest@example.com'}),
                  patch('sys.argv',['watcher.py'])):
                self.assertEqual(watcher.main(),0)
                self.assertEqual(send.call_count,0)
                cfg['seuil_total']=60
                (base/'criteres.json').write_text(json.dumps(cfg,ensure_ascii=False))
                self.assertEqual(watcher.main(),0)
                self.assertEqual(collector.page_text.call_count,1)
                self.assertEqual(send.call_count,1)

if __name__ == "__main__": unittest.main()
