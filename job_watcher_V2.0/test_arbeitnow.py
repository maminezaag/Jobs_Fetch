"""Tests hors réseau. Depuis la racine : python -m unittest discover -s job_watcher_V2.0 -p 'test_*.py' -v"""
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from requests import ConnectionError

import arbeitnow_collector as m

TODAY = "2026-11-01"
EPOCH = int(datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp())
BA_CONFIG = {"periode_jours": 90,
             "titres_exclus": ["entwickler", "developer", "security", "consultant", "architekt", "architect"],
             "titres_domaine_force": [r"\blinux[\s/-]+engineer\b"],
             "titres_marche_force": [r"\bcloud[\s/-]+administrator\b"]}
LANG_CFG = {"kategorien": {"c1_plus": {"zaehlt_als_c1": True}},
            "niveaux_info": [{"name": "B2", "muster": [r"Deutsch.{0,30}\bB2\b"]},
                             {"name": "German required", "muster": [r"Deutschkenntnisse"]}]}
EXPR = [{"name": "C1", "kategorie": "c1_plus"},
        {"name": "verhandlungssicher", "kategorie": "c1_plus"}]
THEMES = [{"name": "Linux", "muster": [re.compile(r"\blinux\b", re.I)]}]


def make_job(slug="a", title="IT-Systemadministrator", company="Beispiel GmbH", location="Bremen",
             description="Wir betreuen Linux Server. Deutsch B2.", remote=False):
    return {"slug": slug, "title": title, "company_name": company, "location": location,
            "description": "<p>" + description + "</p>", "remote": remote,
            "created_at": EPOCH, "url": "https://www.arbeitnow.com/jobs/" + slug}


def classer(t, txt, expr):
    corpus = t + " " + txt
    results = []
    for item in expr:
        name = item["name"]
        pat = r"\bC1\b" if name == "C1" else r"verhandlungssicher\w*"
        if re.search(pat, corpus, re.I):
            results.append(item)
    return results


class Response:
    def __init__(self, obj):
        self.obj = obj
    def raise_for_status(self):
        return None
    def json(self):
        return self.obj


class Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
    def get(self, url, params, timeout, headers):
        page = params["page"]
        self.calls.append(page)
        item = self.pages[page-1]
        if isinstance(item, Exception):
            raise item
        return Response(item)


def listing(*jobs, next_link=None):
    return {"data": list(jobs), "links": {"next": next_link}}


class CollectorTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.original_root = m.ROOT
        m.ROOT = Path(td.name)
        self.addCleanup(setattr, m, "ROOT", self.original_root)
        (m.ROOT/"archive").mkdir()
        (m.ROOT/"config.json").write_text(json.dumps({"max_pages": 4,
                     "pause_seconds": 0, "timeout_seconds": 2,
                     "retries": 1, "max_rows_sheet": 100}),encoding="utf-8")

    def run_collect(self, session, ba=None, save=True):
        tab = m.collecter_onglet(BA_CONFIG, ba or [], EXPR, LANG_CFG, THEMES,
                                TODAY, classer, session=session, save_archive=save)
        self.assertEqual(list(tab), ["offre arbeitsnow"])
        return tab["offre arbeitsnow"]["values"]

    def test_one_sheet_pagination_and_filters(self):
        s = Session([listing(make_job("first"), make_job("bad", title="Web Developer"),
                             make_job("foreign", location="London"),
                             next_link="page2"),
                     listing(make_job("second", title="Linux Engineer", location="Oldenburg"))])
        table = self.run_collect(s)
        self.assertEqual(s.calls, [1,2])
        self.assertEqual(len(table), 3)
        self.assertEqual(len(m._archive_read()), 2)
        self.assertEqual(table[0][5], "Mention de langue")

    def test_no_ba_duplicate_on_same_employer_title_city(self):
        ba = [{"firma": "Beispiel GmbH", "titel": "IT Systemadministrator (m/w/d)", "ort": "Bremen"}]
        table = self.run_collect(Session([listing(make_job())]), ba)
        self.assertEqual(len(table), 1)
        self.assertEqual(len(m._archive_read()), 1)

    def test_duplicate_munich_muenchen_identity_management(self):
        ba = [{"firma": "My Humancapital GmbH", "titel":
               "IT-Systemadministrator Identity & Access Management (m/w/d)", "ort": "München"}]
        job = make_job("iam", title="IT-Systemadministrator Identity & Access Management (m/w/d)",
                       company="MY Humancapital GmbH", location="Munich")
        table = self.run_collect(Session([listing(job)]), ba)
        self.assertEqual(len(table), 1)  # en-tête uniquement

    def test_duplicate_munich_muenchen_web_apps(self):
        ba = [{"firma": "My Humancapital GmbH", "titel":
               "IT-Systemadministrator Web & Applikationen (m/w/d)", "ort": "München"}]
        job = make_job("apps", title="IT-Systemadministrator Web & Applikationen (m/w/d)",
                       company="MY Humancapital GmbH", location="Munich, Germany")
        table = self.run_collect(Session([listing(job)]), ba)
        self.assertEqual(len(table), 1)

    def test_duplicate_title_appends_city_bochum(self):
        ba = [{"firma": "think about IT GmbH", "titel":
               "Senior IT-Administrator (m/w/d), Bochum", "ort": "Bochum"}]
        job = make_job("senior", title="Senior IT Administrator (m/w/d)",
                       company="think about IT GmbH", location="Bochum")
        table = self.run_collect(Session([listing(job)]), ba)
        self.assertEqual(len(table), 1)

    def test_different_specialization_same_city_kept(self):
        ba = [{"firma": "My Humancapital GmbH", "titel":
               "IT-Systemadministrator Identity & Access Management (m/w/d)", "ort": "München"}]
        job = make_job("apps", title="IT-Systemadministrator Web & Applikationen (m/w/d)",
                       company="MY Humancapital GmbH", location="Munich")
        table = self.run_collect(Session([listing(job)]), ba)
        self.assertEqual(len(table), 2)  # deux rôles vraiment distincts

    def test_title_city_other_city_not_merged(self):
        ba = [{"firma": "think about IT GmbH", "titel":
               "Senior IT-Administrator (m/w/d), Bochum", "ort": "Bochum"}]
        job = make_job("senior", title="Senior IT Administrator (m/w/d)",
                       company="think about IT GmbH", location="Berlin")
        table = self.run_collect(Session([listing(job)]), ba)
        self.assertEqual(len(table), 2)

    def test_no_false_city_substring_match(self):
        self.assertFalse(m._city_compatible("Essen", "Grossen"))
        self.assertEqual(m._city_normalized("Köln"), m._city_normalized("Cologne, Germany"))

    def test_distinct_city_not_filtered_as_ba_duplicate(self):
        ba = [{"firma": "Beispiel GmbH", "titel": "IT-Systemadministrator", "ort": "Hamburg"}]
        table = self.run_collect(Session([listing(make_job())]), ba)
        self.assertEqual(len(table), 2)

    def test_language_c1_and_b2(self):
        rows = self.run_collect(Session([listing(make_job("c1", description="Deutsch C1 und Linux"),
                                                make_job("b2", company="Test Andere GmbH", description="Deutsch B2 und Linux"))]))
        self.assertEqual({r[5] for r in rows[1:]}, {"c1", "b2"})
        self.assertEqual(rows[0][6], "Justification langue")

    def test_english_c1_does_not_mean_german_c1(self):
        rows = self.run_collect(Session([listing(make_job(description="English C1, German B2, Linux"))]))
        self.assertEqual(rows[1][5], "b2")

    def test_remote_without_german_location_excluded(self):
        rows = self.run_collect(Session([listing(make_job(location="Remote", description="Linux remote globally."))]))
        self.assertEqual(len(rows), 1)

    def test_no_double_notifications_on_subsequent_runs(self):
        self.run_collect(Session([listing(make_job())]))
        self.run_collect(Session([listing(make_job())]))
        self.assertEqual(len(m._archive_read()), 1)

    def test_no_partial_archive_write_after_page_failure(self):
        with patch.object(m.time, "sleep", return_value=None):
            rows = self.run_collect(Session([listing(make_job(), next_link="page2"),
                                             ConnectionError("API down")]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(m._archive_read(), {})

    def test_api_failure_keeps_previous_archive(self):
        self.run_collect(Session([listing(make_job())]))
        with patch.object(m.time, "sleep", return_value=None):
            rows = self.run_collect(Session([ConnectionError("API down")]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(m._archive_read()), 1)

    def test_same_employer_title_city_deduped(self):
        rows = self.run_collect(Session([listing(make_job("a"), make_job("b"))]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(m._archive_read()), 2)

    def test_hypersonica_five_non_it_engineering_roles_excluded_from_api(self):
        # Le contenu de l'annonce prime sur l'entreprise ; ne pas bloquer Hypersonica.
        samples = [
            ("Seeker System Engineer", "Design seekers, missile guidance and avionics systems."),
            ("Ground Segment System Engineer", "Design telemetry ground segment and flight control equipment."),
            ("Flight Control System Engineer", "Design aircraft flight control and guidance systems."),
            ("Communication System Engineer", "Design RF communications, ground segment telemetry and vehicle avionics."),
            ("Avionics System Engineer", "Develop flight avionics and spacecraft electronics."),
        ]
        jobs = [make_job(str(i), title=title, company="Hypersonica", location="Munich",
                         description=desc) for i, (title, desc) in enumerate(samples)]
        table = self.run_collect(Session([listing(*jobs)]))
        self.assertEqual(len(table), 1)
        self.assertFalse(m._archive_read())  # les nouvelles faux-positives ne sont pas archivées

    def test_hypersonica_real_it_admin_is_retained(self):
        job = make_job("it", title="IT-Systemadministrator Linux", company="Hypersonica",
                       location="Munich", description="Administration Linux Server, VMware, AD. Aerospace company.")
        table = self.run_collect(Session([listing(job)]))
        self.assertEqual(len(table), 2)
        self.assertEqual(table[1][2], "Hypersonica")

    def test_system_engineer_citrix_kept_even_at_aerospace_company(self):
        job = make_job("citrix", title="System Engineer Citrix", company="Hypersonica",
                       location="Munich", description="VMware Horizon, Windows Server, aviation company.")
        table = self.run_collect(Session([listing(job)]))
        self.assertEqual(len(table), 2)

    def test_ground_segment_system_engineer_real_it_admin_retained(self):
        job = make_job("groundit", title="Ground Segment System Engineer", company="Satellite IT GmbH",
                       location="Munich", description="Administration Windows Server, VMware and IT infrastructure.")
        table = self.run_collect(Session([listing(job)]))
        self.assertEqual(len(table), 2)

    def test_communication_system_engineer_it_description_is_retained(self):
        job = make_job("itnetwork", title="Communication System Engineer", company="Autre GmbH",
                       location="Munich", description="Administration Windows Server and IT infrastructure in datacenter.")
        table = self.run_collect(Session([listing(job)]))
        self.assertEqual(len(table), 2)

    def test_generic_system_engineer_aerospace_filtered(self):
        job = make_job("space", title="System Engineer (ISOS Host)", company="Exploration GmbH",
                       location="Munich", description="Spacecraft command and control, avionics telemetry and launch vehicle.")
        table = self.run_collect(Session([listing(job)]))
        self.assertEqual(len(table), 1)

    def test_archived_non_it_removed_from_sheet_not_from_archive(self):
        # Cas décisif pour le fichier existant : les cinq offres avaient déjà
        # été sauvegardées. On conserve l'archive mais on recalcule l'onglet.
        samples = [
            ("Seeker System Engineer", "Missile seeker, telemetry, avionics"),
            ("Ground Segment System Engineer", "Ground segment, flight control and launch vehicle"),
            ("Flight Control System Engineer", "Flight control and avionics"),
            ("Communication System Engineer", "RF communications, telemetry and avionics"),
            ("Avionics System Engineer", "Avionics satellite mission"),
        ]
        archive = []
        for i, (title, desc) in enumerate(samples):
            job = m._convert(make_job(str(i), title=title, company="Hypersonica",
                                      description=desc, location="Munich"), BA_CONFIG, TODAY)
            # Simuler l'ancien code qui classait ces postes Cœur IT.
            archive.append({"refnr": "arbeitnow:old" + str(i), "titel": title,
                            "firma": "Hypersonica", "location": "Munich", "datum": "2026-10-31",
                            "text": desc, "remote": False, "url": "https://www.arbeitnow.com/jobs/" + str(i),
                            "segment": "Cœur IT", "premiere_vue": "2026-11-01"})
        archive.append({"refnr": "arbeitnow:realIT", "titel": "IT-Systemadministrator Linux",
                        "firma": "Hypersonica", "location": "Munich", "datum": "2026-10-31",
                        "text": "Administration von Linux Servern und VMware.", "remote": False,
                        "url": "https://www.arbeitnow.com/jobs/realIT", "segment": "Cœur IT",
                        "premiere_vue": "2026-11-01"})
        (m.ROOT / "archive" / "2026-11.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in archive), encoding="utf-8")
        rows = self.run_collect(Session([listing()]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], "IT-Systemadministrator Linux")
        self.assertEqual(len(m._archive_read()), 6)

    def test_five_hypersonica_without_company_blacklist(self):
        self.assertIsNone(m._segment("Avionics System Engineer", BA_CONFIG, "Avionics system"))
        self.assertEqual(m._segment("IT-Systemadministrator Linux", BA_CONFIG, "Aerospace company"), "Cœur IT")

    def test_formula_injection_escaped(self):
        rows = self.run_collect(Session([listing(make_job(company='=HYPERLINK("x","bad")'))]))
        self.assertTrue(rows[1][2].startswith("'="))


if __name__ == "__main__":
    unittest.main()
