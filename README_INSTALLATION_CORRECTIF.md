# Jobs_Fetch principal — correction français / doublons / priorité métier

Version : 01 novembre 2026. **Basée sur le correctif scoring/ciblage installé pour le run Job Watcher #147** (code du ZIP `Jobs_Fetch_correctif_ciblage_scoring_2026-11-01.zip`).

## Installation GitHub

Dézipper, puis dans la racine du dépôt `maminezaag/Jobs_Fetch` :

1. **Remplacer** `job_watcher.py`.
2. **Remplacer** `criteres.json`.
3. **Ajouter** `test_job_watcher_doublons_priorites.py` (recommandé, pas nécessaire au workflow).
4. `test_job_watcher_ciblage.py` est fourni comme suite de régression facultative : tu peux le laisser tel quel s'il existe déjà.
5. Faire **Commit changes** puis lancer **Actions → Job Watcher → Run workflow**.

**Ne pas remplacer** `config.yaml`, `seen_jobs.json`, `.github/workflows/job_watcher.yml`, ni aucun autre fichier ou dossier. Ne pas toucher aux modules Bochra, université, stats, Arbeitnow ou taux de change. Le workflow existant suffit.

## Modifications

### 1) Offres avec français

- L'exception nationale est conservée **dans toute l'Allemagne**, sans limite de 70 km, et sans score minimum si le métier correspond.
- Le simple mot `Französischkenntnisse` ne suffit plus : intitulé du poste et contexte métier doivent être liés à l'administration IT, infrastructure, réseau, support, Cloud / exploitation.
- `Inhouse Consultant SAP S/4 HANA Service` et `Inhouse Consultant SAP S/4 FI` sont désormais écartés, même si une description mentionne « 1st Level Support ».
- Une offre située à **Rothrist (Suisse)** n'est pas prise grâce à la dérogation française : lieu allemand non confirmé.
- Un vrai IT Support / Linux Admin avec français à Berlin ou à Wetzlar est toujours admis.
- Exigences allemand C1/C2/natif restent éliminatoires.
- **Important :** en l'absence de pays, de Land, de code postal allemand ou de ville allemande reconnue, le script écarte l'offre française distante *par précaution*. Cela peut aussi masquer une annonce allemande mal renseignée. Consulter les logs pour la revoir.

### 2) Plusieurs références pour la même annonce BA

- Deux annonces sont fusionnées seulement si **employeur, ville, titre, proximité de publication et contenu descriptif** coïncident fortement.
- Exemple : `Fachinformatiker für Systemintegration (m/w/d)` et `Fachinformatiker für Systemintegration`, Stadt Vechta, même ville, description très similaire, dates proches → un seul e-mail.
- Deux emplois différents dans une même entreprise sont préservés.
- Un registre additionnel `notifications` est ajouté automatiquement **dans** `seen_jobs.json` après l'envoi SMTP **réussi**, afin de reconnaître les nouvelles références de cette même annonce les jours suivants.
- **La clé historique `vus` reste au format antérieur** et n'est pas vidée ni convertie.
- L'ancien historique ne distinguait pas les annonces rejetées des annonces notifiées : impossible de reconstituer parfaitement les doublons déjà envoyés. Le nouveau registre commence à partir de l'installation du correctif. Les anciens e-mails ne sont pas modifiés.
- Comparaison volontairement prudente pour ne pas supprimer des emplois différents ayant des titres voisins : certains doublons peu ressemblants peuvent encore passer.

### 3) Priorité métier séparée du score

Le mail affiche pour chaque offre :

- **Très adapté** : métiers principaux (Linux/Windows/IT Administration, IT Operations, Datacenter / plateforme, réseau, M365, infrastructure, etc.), y compris `Head of IT Operations`.
- **Adapté** : Systemintegration, IT Support, IAM, assistance, terrain IT, etc.
- **À examiner** : métiers informatiques périphériques (ex. Python Developer), gardés si le score existant est suffisant.

Les offres sont triées d'abord par priorité métier, puis par score. **Le score technique, le seuil normal de 12, le seuil spécial métier de 5, la distance, la pénalité DevOps/CI-CD et le contrôle allemand B2/C1 sont inchangés**.

## Vérifications hors ligne

```bash
python -m py_compile job_watcher.py
python -m json.tool criteres.json > /dev/null
python -m unittest -q test_job_watcher_ciblage.py test_job_watcher_doublons_priorites.py
```

La nouvelle suite n'utilise ni réseau ni SMTP réels. Tests réalisés dans le contexte de préparation : **69 réussis** (44 précédents + 25 nouveaux). Ce n'est pas une garantie de couverture de toutes les annonces de l'API.

### Premier run après mise à jour

Les 18 annonces du run #147 **ne seront pas renvoyées**, car leurs références sont déjà dans `seen_jobs.json`. C'est normal. Les nouvelles règles s'appliqueront aux prochaines offres. **Ne pas effacer** `seen_jobs.json` pour forcer un réenvoi.

Pour un examen hors ligne de notifications (sans envoi ni modification historique) :

```bash
python job_watcher.py --config config.yaml --dry-run --review-seen --review-limit 150
```

Cette commande consulte l'API et ne revoit que les annonces encore visibles dans les sept derniers jours de la recherche ; elle n'envoie pas d'e-mails.
