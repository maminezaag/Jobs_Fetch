"""Tests de non-régression V7, fondés notamment sur le faux positif Oldenburg QBL09."""
import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock
from watcher import Offre, noter, analyser_une_offre
from adequation import evaluer_adequation, extraire_parties

CFG=json.loads((Path(__file__).parent/'criteres.json').read_text(encoding='utf-8'))

# Version synthétisée des rubriques essentielles de l'annonce réelle d'Oldenburg.
# Aucune donnée personnelle ni adresse du responsable du recrutement.
QUANTUM = '''
About us. The Quantum Biology and Computational Physics Group works on molecular simulations.
Your tasks
Develop computational methods for investigating quantum effects at ambient temperatures.
Design, execute and analyse numerical simulations for research objectives.
Provide computational biophysical analyses and publish scientific research manuscripts.
Your profile
Required qualifications:
Advanced programming proficiency in Python and Bash for high-performance computing (HPC).
Strong experience with Linux servers, Docker and virtualization.
Background in molecular dynamics, quantum chemical calculations and spin dynamics.
Practical experience with quantum chemistry and Gaussian, NAMD and GROMACS.
Preferable/Desirable:
Enthusiasm for interdisciplinary collaborations.
We offer
TV-L E13, 50%, Oldenburg.
'''

IT_DANS_LABORATOIRE='''
About us: Physics and quantum biology research department.
Your tasks
Administration of Linux servers and server infrastructure for researchers.
Infrastructure automation with Puppet and Ansible. Server administration and monitoring.
Support scientific users, maintain Linux servers and virtual machines.
Your profile
Required qualifications:
Experience in Linux system administration, VMware, Bash, Ansible, Puppet and IT operations.
Knowledge of collaboration in a quantum chemistry laboratory is desirable.
We offer
TV-L E13, 100%, Oldenburg.
'''

BPM_IO_T='''
Your tasks
Design BPMN workflows for business process management and integrate IoT sensor networks.
Develop process automation and information systems research applications.
Your profile
Required qualifications:
Master in information systems or computer science, experience in BPMN and IoT,
Python, MySQL, process automation and research publications.
We offer
TV-L E13, 75%, Oldenburg.
'''

SCIENCE_AMBIGUE='''
Your tasks
Develop quantum effects models for an interdisciplinary project.
Your profile
Required qualifications:
Knowledge of physics simulations and numerical techniques.
We offer
TV-L E13, 100%, Oldenburg.
'''

class AnalyseMetierV7(unittest.TestCase):
    def new(self, title, body, ville='Oldenburg'):
        return Offre('oldenburg',title,'https://uol.de/job-validation','Oldenburg',body)

    def test_real_case_quantum_classified_out_of_specialty(self):
        a=evaluer_adequation('Research Assistant in the Quantum Biology and Computational Physics Group',
                             QUANTUM,CFG['adequation_v7'])
        self.assertEqual(a['action'],'HORS_PROFIL')
        self.assertEqual(a['domaine'],'RECHERCHE_HORS_SPECIALITE')
        self.assertTrue(a['sections_completes'])
        self.assertIn('hpc',a['exigences_specialisees'])

    def test_real_case_quantum_no_priority(self):
        n=noter(self.new('Research Assistant in the Quantum Biology and Computational Physics Group',
                         QUANTUM),CFG)
        self.assertEqual(n['priorite'],'ECARTEE')
        self.assertFalse(n['alerte'])
        self.assertIn('hors spécialité', ' '.join(n['motifs']))

    def test_generic_virtualization_not_specific_vmware(self):
        n=noter(self.new('Research Assistant Quantum Biology',QUANTUM),CFG)
        self.assertNotIn('VMware / Proxmox',dict(n['competences']))

    def test_actual_vmware_is_specific_vmware(self):
        n=noter(self.new('IT Administrator Linux VMware',IT_DANS_LABORATOIRE),CFG)
        self.assertEqual(dict(n['competences'])['VMware / Proxmox'],4)

    def test_it_admin_inside_quantum_lab_remains_eligible(self):
        n=noter(self.new('IT-Systemadministrator Linux',IT_DANS_LABORATOIRE),CFG)
        self.assertEqual(n['adequation']['domaine'],'ADMINISTRATION_IT')
        self.assertEqual(n['priorite'],'PRIORITAIRE')
        self.assertTrue(n['alerte'])

    def test_research_bpm_iot_remains_eligible(self):
        n=noter(self.new('Wissenschaftlicher Mitarbeiter BPMN und IoT', BPM_IO_T),CFG)
        self.assertEqual(n['adequation']['domaine'],'RECHERCHE_BPM_IOT')
        self.assertTrue(n['alerte'])
        self.assertEqual(n['priorite'],'PRIORITAIRE')

    def test_ambiguous_science_is_review_not_rejected(self):
        a=evaluer_adequation('Research Assistant Computational Biology',SCIENCE_AMBIGUE,CFG['adequation_v7'])
        self.assertEqual(a['action'],'EXAMEN')
        self.assertNotEqual(a['action'],'HORS_PROFIL')

    def test_no_sections_title_quantum_forces_review(self):
        a=evaluer_adequation('Research Assistant in Quantum Biology',
            'Linux Python Docker TV-L E13 Vollzeit. Institut Physics',CFG['adequation_v7'])
        self.assertEqual(a['action'],'EXAMEN')

    def test_no_sections_it_administrator_not_automatically_rejected(self):
        a=evaluer_adequation('IT Administrator Linux Ansible',
            'IT support, Linux, Ansible, Bash. TV-L E13 Vollzeit',CFG['adequation_v7'])
        self.assertEqual(a['action'],'OK')

    def test_separate_required_from_desirable(self):
        parts=extraire_parties('Your tasks Linux server administration. Your profile Required qualifications: '
            'Linux and Bash. Preferable/Desirable: Molecular dynamics, Gaussian. We offer benefits.')
        self.assertIn('Linux',parts['exigences'])
        self.assertNotIn('Gaussian',parts['exigences'])

    def test_geochemistry_away_from_profile_when_compulsory(self):
        t=('Ihre Aufgaben: isotope geochemistry, marine geochemistry and geochemical analyses. '
           'Ihr Profil: Kenntnisse in mass spectrometry and isotope geochemistry '
           'and geochemical analysis. Wir bieten: TV-L E13, 100%.')
        a=evaluer_adequation('Wissenschaftlicher Mitarbeiter Meeresgeochemie',t,CFG['adequation_v7'])
        self.assertEqual(a['action'],'HORS_PROFIL')

    def test_reassessment_old_cached_detail_on_criteria_change(self):
        o=self.new('Research Assistant in Quantum Biology', 'TV-L E13 50%')
        col=Mock(); col.page_text.return_value=QUANTUM
        note, cache, why=analyser_une_offre(o,col,CFG['sources']['oldenburg'],None,CFG,date(2026,10,25))
        self.assertEqual(note['priorite'],'ECARTEE')
        self.assertEqual(why,'analyse du détail')
        o2=self.new('Research Assistant in Quantum Biology', 'TV-L E13 50%')
        old=dict(cache)
        old['signature_regles']='v6-ancien-cache'
        note2, cache2, why2=analyser_une_offre(o2,col,CFG['sources']['oldenburg'],old,CFG,date(2026,10,26))
        self.assertEqual(why2,'nouveaux critères : re-scoring du cache')
        self.assertEqual(note2['priorite'],'ECARTEE')
        self.assertEqual(col.page_text.call_count,1)

    def test_uncertain_high_score_downgraded_to_human_review(self):
        txt='Your tasks\n Develop quantum effects models, manage linux servers with Ansible. ' \
            'Your profile\n Required qualifications: advanced Python, Bash, Docker and Linux. ' \
            'Some experience in molecular dynamics and HPC. We offer TV-L E13 100%.'
        n=noter(self.new('Research Assistant Quantum Effects IT System Administrator',txt),CFG)
        self.assertEqual(n['adequation']['action'],'EXAMEN')
        self.assertEqual(n['priorite'],'A_EXAMINER')
        self.assertFalse(n['eligible'])

    def test_no_auto_reject_by_physics_lab_name_alone(self):
        txt=('Our institute performs quantum chemistry. Your tasks Linux server administration and '
             'server infrastructure support. Your profile Required qualifications: '
             'Linux, Ansible, VMware and Bash. We offer E13 Vollzeit.')
        a=evaluer_adequation('IT Administrator Physics Lab',txt,CFG['adequation_v7'])
        self.assertNotEqual(a['action'],'HORS_PROFIL')

    def test_cache_signature_v7_includes_new_rules(self):
        o=self.new('IT Administrator Linux VMware Ansible Bash','TV-L E13, 100%')
        col=Mock();col.page_text.return_value=IT_DANS_LABORATOIRE
        _,old,_=analyser_une_offre(o,col,CFG['sources']['oldenburg'],None,CFG,date(2026,10,25))
        config2=json.loads(json.dumps(CFG))
        config2['adequation_v7']['missions_it'].append('custom technical role')
        o2=self.new('IT Administrator Linux VMware Ansible Bash','TV-L E13, 100%')
        _,new,situation=analyser_une_offre(o2,col,CFG['sources']['oldenburg'],old,config2,date(2026,10,26))
        self.assertEqual(situation,'nouveaux critères : re-scoring du cache')
        self.assertEqual(col.page_text.call_count,1)

if __name__=='__main__':unittest.main()
