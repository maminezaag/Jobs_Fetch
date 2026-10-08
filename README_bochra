# Job watcher pour Bochra (chimie / laboratoire)

Script séparé de `job_watcher.py`, dans le même repo et le même workflow.
Il cherche des offres **Teilzeit** ou **Praktikum** en chimie / laboratoire,
dans un rayon de **20 km autour d'Oldenburg**, et envoie un e-mail Gmail pour
chaque nouvelle offre qui passe les critères.

## Fichiers

| Fichier | Rôle | À modifier ? |
|---|---|---|
| `job_watcher_bochra.py` | le script | rarement |
| `criteres_bochra.json` | mots-clés, éliminations, points, alertes | **souvent** (éditable dans GitHub) |
| `config_bochra.yaml` | rayon, seuils, API, e-mail | parfois |
| `seen_jobs_bochra.json` | historique des offres déjà vues | jamais à la main (réécrit par le script) |
| `.github/workflows/job_watcher.yml` | lance les deux scripts | une fois |

Aucun nouveau secret : les mêmes `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`,
`GMAIL_TO` que `job_watcher.py`. Aucun Google Sheet pour ce script.
`requirements.txt` doit contenir `requests` et `pyyaml` (déjà le cas si
`job_watcher.py` fonctionne avec `config.yaml`).

## Ce que fait le script (dans l'ordre)

1. Recherche par mot-clé sur l'API de l'Arbeitsagentur (offres des 7 derniers jours,
   pré-filtre code postal 26129 + 25 km). Les offres d'Ausbildung ne sont jamais demandées.
2. Pré-filtre : distance GPS > 20 km, titre d'Ausbildung, hors domaine chimie/labo.
3. Lecture du texte complet des nouvelles annonces restantes.
4. Classification : **Praktikum**, **Teilzeit**, sinon écartée (Vollzeit).
5. Éliminations :
   - statut étudiant/élève mentionné (Immatrikulation, Pflichtpraktikum, Werkstudent,
     Abschlussarbeit, Schülerpraktikum…), même « souhaité » ;
   - expérience exigée (Teilzeit uniquement ; pour un Praktikum elle est seulement signalée) ;
   - allemand C1/C2/langue maternelle (toujours) ;
   - allemand en termes vagues (« verhandlungssicher »…) sauf si B1/B2 est mentionné.
6. Score par groupes (un seul comptage par groupe), seuil : 8 points (Teilzeit), 6 (Praktikum).
7. E-mail avec les offres qualifiées, triées par score, avec alertes (B1/B2 mentionné,
   « abgeschlossene Ausbildung » mentionnée, expérience mentionnée pour un stage).

## Tester sans rien envoyer

En local ou dans une Action manuelle :

```
python job_watcher_bochra.py --config config_bochra.yaml --dry-run --ignore-seen --verbose
```

Le script affiche chaque décision (qualifiée / écartée + raison) et le texte de l'e-mail,
sans l'envoyer et sans toucher à `seen_jobs_bochra.json`.

## Si quelque chose ne marche pas

- **« Aucune recherche n'a abouti »** (code 2) : l'API a changé. Vérifier `search_path`
  et `api_key` dans `config_bochra.yaml`.
- **« Aucun détail d'annonce n'a pu être lu »** (code 3) : le endpoint de détail n'est pas
  officiellement documenté. Le log indique l'erreur reçue ; adapter `details_paths`.
- **Trop peu d'offres** : lancer le `--dry-run --verbose` et regarder les raisons d'élimination
  dans le bilan, puis ajuster `criteres_bochra.json` (ou baisser `seuil_points`).
- **Trop d'offres sans intérêt** : relever le seuil ou retirer un mot-clé de `recherche`.
- **Distances** : calculées à vol d'oiseau depuis le centre d'Oldenburg (`point_reference`).

## Quand Bochra aura son B2

Rien à changer : les offres qui mentionnent B1/B2 sont déjà gardées. Si on veut être plus
strict ou plus souple sur l'allemand, éditer `langue_souple` / `b2_accepte` dans
`criteres_bochra.json`.
