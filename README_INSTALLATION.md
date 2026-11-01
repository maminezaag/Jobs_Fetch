# Jobs Fetch — Correctif ciblage des spécialités infrastructure (01.11.2026)

## Objectif

Réduire les faux rejets parmi les annonces IT locales sous le seuil de 12 points,
sans élargir le recrutement aux métiers hors infrastructure.

## Contenu

- `job_watcher.py` — **remplacer** le fichier de même nom à la racine de `Jobs_Fetch`.
- `criteres.json` — **remplacer** le fichier de même nom à la racine de `Jobs_Fetch`.
- `test_job_watcher_specialites.py` — **ajouter**, facultatif, à la racine pour les tests.

Ce paquet part du correctif précédent : `Jobs_Fetch_correctif_francais_doublons_priorites_2026-11-01.zip`.
Il conserve les améliorations français, dédoublonnage entre références BA et priorité métier.

## Changements

- Recherches de postes supplémentaires : VMware, Citrix, Storage/Backup, IT-Anwendungsbetreuer.
- Bonus métier +5 ciblé sur VMware, virtualisation, Citrix, stockage et sauvegarde.
- Pour les titres génériques, confirmation IT nécessaire (technologies détectées ou précision IT dans le titre).
- `IT-Anwendungsbetreuer` : bonus conditionnel à un contexte technique, par exemple « Klinische IT-Systeme ».
- Priorités métier « Très adapté » pour l'infrastructure spécialisée ; « Adapté » pour Anwendungsbetreuer.
- **Inchangés** : seuil normal 12, seuil métier 5, français, C1 allemand, 70 km, remote 100 %, pénalités DevOps, dédoublonnage et notifications.

## Vérifications (sur exemples de l'audit)

- « Senior Specialist Virtualisierung - VMware & Nutanix » : bonus +5, admissible.
- « Infrastructure Specialist - Storage & Backup » : bonus +5, admissible.
- « Administrator Technisches Verfahrensmanagement Citrix-Infrastruktur » : bonus +5, admissible.
- « IT-Anwendungsbetreuer:in ... Klinische IT-Systeme » : bonus conditionnel +5, admissible.

Les calculs ci-dessus correspondent aux règles du programme, testées sur des cas représentatifs.
Les annonces réelles dépendent aussi de leur description intégrale, des éventuelles exigences C1 et de leur localisation.

## Installation

1. Dans GitHub, branche `main`, remplacer **seulement** `job_watcher.py` et `criteres.json` à la racine.
2. Facultatif : ajouter `test_job_watcher_specialites.py` à la racine.
3. Cliquer sur `Commit changes`.
4. Lancer **Actions → Audit anciennes offres IT → Run workflow** avec `review_limit = 2000`.
5. Télécharger l'artifact `audit-anciennes-offres` et examiner les nouvelles offres dans la feuille « A verifier ».
6. Le watcher normal prendra les nouveaux critères en compte à son prochain lancement.

Ne pas supprimer ni remplacer `seen_jobs.json`, et ne pas toucher aux autres modules.
Le workflow d'audit, s'il a été installé précédemment, ne nécessite aucune modification.

## Tests

```
python -m unittest -v test_job_watcher_ciblage.py test_job_watcher_doublons_priorites.py test_job_watcher_specialites.py
```

Résultat lors de la construction : 88 tests réussis. Aucun appel réseau ou e-mail réel.
