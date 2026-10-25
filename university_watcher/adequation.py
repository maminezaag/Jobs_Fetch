"""V7 — Estimation EXPLICABLE du métier principal, sans service d'IA.

Ne prétend pas certifier l'éligibilité du candidat : les motifs restent visibles.
Une spécialisation éloignée n'est éliminatoire que si les MISSIONS ET les
EXIGENCES OBLIGATOIRES présentent plusieurs indices spécifiques concordants.
Sans ces deux parties, le programme s'abstient de rejeter automatiquement.
"""
from __future__ import annotations
import re
from typing import Dict, List


def nettoyer(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def contient(texte, terme):
    """Mot/expression entier(e), insensible à la casse et aux accents inchangés."""
    return bool(re.search(r'(?<!\w)' + re.escape(terme.casefold()) + r'(?!\w)', texte.casefold()))


# Les blocs standard des annonces de recrutement allemandes/anglaises.
DEBUT_MISSIONS = [
    'your tasks', 'your responsibilities', 'main responsibilities',
    'ihre aufgaben', 'deine aufgaben', 'aufgabengebiet',
    'ihre tätigkeiten', 'ihre taetigkeiten', 'your duties',
]
DEBUT_EXIGENCES = [
    'required qualifications', 'essential requirements', 'required skills',
    'your profile', 'qualifications and experience', 'person specification',
    'ihr profil', 'dein profil', 'anforderungsprofil', 'das bringen sie mit',
    'sie bringen mit', 'unsere anforderungen', 'was wir erwarten',
]
DEBUT_OPTIONNEL = [
    'preferable/desirable', 'desirable qualifications', 'preferred qualifications',
    'nice to have', 'wünschenswert', 'wuenschenswert',
    'we offer', 'wir bieten', 'das bieten wir', 'our offer',
    'our standards', 'further information', 'apply now', 'bewerbung',
]


def extraire_parties(texte: str) -> Dict[str, str]:
    """Sections distinctes, même si l'extracteur HTML a aplati les sauts de ligne.

    Les titres intermédiaires 'Your profile' et 'Required qualifications'
    peuvent se suivre ; la seconde marque le début précis des exigences.
    """
    t = nettoyer(texte)
    marques = []
    for genre, termes in [('missions', DEBUT_MISSIONS), ('exigences', DEBUT_EXIGENCES),
                          ('fin', DEBUT_OPTIONNEL)]:
        for terme in termes:
            pattern = r'(?<!\w)' + re.escape(terme) + r'(?!\w)'
            for m in re.finditer(pattern, t, re.I):
                marques.append((m.start(), m.end(), genre, terme))
    marques.sort(key=lambda x: (x[0], -(x[1]-x[0])))
    mission_start = next((x for x in marques if x[2] == 'missions'), None)
    mission_fin = len(t)
    if mission_start:
        next_req = [x for x in marques if x[2] in {'exigences','fin'} and x[0] > mission_start[1]]
        if next_req: mission_fin = min(x[0] for x in next_req)
    # Préférer le titre 'Required qualifications' à 'Your profile' lorsque présents.
    req_candidates = [x for x in marques if x[2] == 'exigences' and
                      (not mission_start or x[0] > mission_start[0])]
    req_start = next((x for x in req_candidates if x[3] in {
        'required qualifications','essential requirements','required skills'}), None)
    if req_start is None and req_candidates:
        req_start = req_candidates[0]
    req_fin = len(t)
    if req_start:
        limites = [x for x in marques if x[2] == 'fin' and x[0] > req_start[1]]
        if limites: req_fin = min(x[0] for x in limites)
    missions = t[mission_start[1]:mission_fin].strip() if mission_start else ''
    exigences = t[req_start[1]:req_fin].strip() if req_start else ''
    return {'missions': missions[:22000], 'exigences': exigences[:22000],
            'missions_identifiees': bool(mission_start),
            'exigences_identifiees': bool(req_start)}


def termes_reperes(texte: str, termes: List[str]) -> List[str]:
    return [t for t in termes if contient(texte, t)]


def evaluer_adequation(titre: str, texte: str, regles: dict) -> dict:
    """Verdict de priorité fondé sur tâches et exigences, pas sur les buzzwords.

    Décisions: OK / EXAMEN / HORS_PROFIL. EXAMEN garde l'alerte secondaire.
    HORS_PROFIL exige des preuves fortes : 2 missions spécialisées distinctes,
    2 exigences spécialisées distinctes, et aucune mission IT/BPM centrale.
    """
    parties = extraire_parties(texte)
    missions = parties['missions']
    exigences = parties['exigences']
    it = termes_reperes(missions, regles.get('missions_it', []))
    cible = termes_reperes(missions, regles.get('missions_bpm_iot', []))
    domaines = []
    for spec in regles.get('domaines_scientifiques_eloignes', []):
        missions_spec = termes_reperes(missions, spec.get('missions', []))
        req_spec = termes_reperes(exigences, spec.get('exigences_obligatoires', []))
        if missions_spec or req_spec:
            domaines.append((spec.get('nom', 'Spécialité scientifique'), missions_spec, req_spec))
    domaines.sort(key=lambda entry: len(entry[1]) * 2 + len(entry[2]), reverse=True)
    principal = domaines[0] if domaines else None
    t = titre.casefold()
    titre_it = bool(re.search(r'(?<!\w)(?:it[ -]?administrator|it[ -]?systemadministrator|'
                              r'linux[ -]?administrator|system[ -]?administrator|'
                              r'systemadministration|it[ -]?infrastruktur|devops|'
                              r'infrastructure engineer|it support engineer)(?!\w)', t))
    titre_bpm = any(contient(t, terme) for terme in ('bpm', 'bpmn', 'iot', 'workflow',
                             'business process', 'informationssysteme', 'information systems'))
    complet = bool(parties['missions_identifiees'] and parties['exigences_identifiees']
                   and len(missions) >= 45 and len(exigences) >= 45)
    statut = 'INCERTAIN'
    verdict = 'OK'
    raison = ''
    # Protection des chercheurs/administrateurs travaillant dans un laboratoire de physique.
    it_central = len(it) >= 2 or (len(it) >= 1 and titre_it)
    cible_central = len(cible) >= 2 or (len(cible) >= 1 and titre_bpm)
    if (principal and len(principal[1]) >= 1 and len(principal[2]) >= 1
            and (it_central or cible_central)):
        statut = 'INTERDISCIPLINAIRE_A_VERIFIER'
        verdict = 'EXAMEN'
        raison = 'missions IT/BPM mêlées à des exigences scientifiques spécialisées'
    elif it_central and (not principal or len(it) >= len(principal[1])):
        statut = 'ADMINISTRATION_IT'
        raison = 'missions IT centrales détectées'
    elif cible_central and (not principal or len(cible) >= len(principal[1])):
        statut = 'RECHERCHE_BPM_IOT'
        raison = 'missions BPM/IoT centrales détectées'
    elif principal and complet and len(principal[1]) >= 2 and len(principal[2]) >= 2 \
            and not it_central and not cible_central:
        statut = 'RECHERCHE_HORS_SPECIALITE'
        verdict = 'HORS_PROFIL'
        raison = ('spécialisation scientifique dominante dans les missions ET '
                  'dans les qualifications obligatoires')
    elif principal and len(principal[1]) >= 1 and complet:
        statut = 'INTERDISCIPLINAIRE_A_VERIFIER'
        verdict = 'EXAMEN'
        raison = 'exigences scientifiques spécifiques à examiner manuellement'
    elif it_central or titre_it:
        statut = 'ADMINISTRATION_IT' if it_central else 'IT_PRESUMEE'
        raison = 'missions IT' if it_central else 'titre IT ; missions non concluantes'
    elif cible_central or titre_bpm:
        statut = 'RECHERCHE_BPM_IOT' if cible_central else 'RECHERCHE_CIBLE_PRESUMEE'
        raison = 'missions BPM/IoT' if cible_central else 'sujet BPM/IoT ; missions non concluantes'
    else:
        # Un domaine inconnu n'est pas une preuve de compatibilité :
        # suffisamment de mots techniques peuvent être trouvés dans une spécialité éloignée.
        # Toujours permettre l'examen humain lorsque les seuils le justifient.
        verdict = 'EXAMEN'
        raison = 'domaine principal non identifié ; vérification humaine nécessaire'
    # Même si les rubriques sont absentes, un titre de spécialisation clairement
    # éloignée ne doit pas générer une alerte PRIORITAIRE par simple comptage IT.
    titre_specialise = any(
        contient(t, mot)
        for spec in regles.get('domaines_scientifiques_eloignes', [])
        for mot in spec.get('missions', [])
    )
    titre_recherche_generique = bool(re.search(
        r'(?:research assistant|scientific researcher|doctoral researcher|'
        r'wissenschaftlich.{0,18}mitarbeiter|doktorand|phd)', t))
    if not complet and (principal or titre_specialise or
                       (titre_recherche_generique and not titre_bpm and not titre_it)):
        # Ne jamais éliminer sur un descriptif partiel.
        verdict = 'EXAMEN'
        raison = 'missions ou exigences non isolées ; vérification humaine nécessaire'
    return {
        'domaine': statut,
        'action': verdict,
        'motif': raison,
        'missions_it': it[:5],
        'missions_bpm_iot': cible[:5],
        'specialite': principal[0] if principal else '',
        'missions_specialisees': principal[1][:5] if principal else [],
        'exigences_specialisees': principal[2][:7] if principal else [],
        'sections_completes': complet,
    }
