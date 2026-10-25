# University Watcher — module indépendant de Jobs_Fetch

Surveille quatre établissements : Universität Oldenburg (RSS), Universität Bremen
(page des annonces), Jade Hochschule (tableau des salariés), Hochschule Emden/Leer
(portail Coveto dans Chromium). Test de pertinence identique pour les **trois catégories**
(poste doctoral, recherche, IT), sans bonus systématique pour l'une d'elles.

## Les critères

- **60/100 points minimum** et **20 points minimum pour les compétences**.
- Compétences : maximum 55, groupes de synonymes (un seul match/groupe).
- Localité : Oldenburg 30, Brême 28, Bad Zwischenahn / Emden / Leer /
  Wilhelmshaven / Elsfleth / Westerstede / Rastede / Brake / Delmenhorst 25.
- Autres localités : « remote » explicite 20, hybride à vérifier 10,
  sans possibilité de travailler à distance = exclu.
- Contrat : maximum 15, points TV-L et temps de travail, inconnues à 0.
- Exclusions : allemand C1/C2 obligatoire, allemand natif, verhandlungssicher
  sauf B2 explicitement accepté, titre exigeant un doctorat déjà obtenu,
  postdoctorat, professorat, formation, doctorat explicitement non rémunéré.
- Il s'agit d'un *filtre par mots-clés*, pas d'une appréciation fiable des
  chances d'admission ou de la possibilité effective de doctorat.

Tout le barème est dans **`criteres.json`**. Modifier le fichier pour ajouter,
retirer, ou repondérer les groupes et le seuil.

## Installation dans Jobs_Fetch

1. Décompresser cette archive.
2. Copier le dossier `university_watcher/` à la racine du dépôt `Jobs_Fetch`.
3. Copier `.github/workflows/university_watcher.yml` dans le répertoire
   `.github/workflows/` du dépôt. **Ne pas remplacer** les autres workflows.
4. Les trois secrets `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `GMAIL_TO` sont
   ceux déjà configurés dans Jobs_Fetch ; ne jamais les écrire dans le code.
5. GitHub → Actions → « University Watcher » → « Run workflow ».
6. Quand le run manuel a été validé, utiliser crontab.org pour déclencher
   l'event `repository_dispatch` **`university_watcher`** (différent de celui
   du Job Watcher principal), quotidiennement. Ce webhook nécessite un token
   GitHub disposant de la permission `Contents: write` pour ce dépôt :

   POST `https://api.github.com/repos/maminezaag/Jobs_Fetch/dispatches`
   Headers : `Authorization: Bearer <TOKEN>` ; `Accept: application/vnd.github+json`
   Corps : `{ "event_type": "university_watcher" }`

   Ne pas exposer ce token dans un dépôt ou des logs publics.

## Test manuel local (Linux / Windows)

```bash
python -m pip install -r university_watcher/requirements.txt
python -m playwright install chromium
python university_watcher/watcher.py --dry-run
# Pour isoler une source :
python university_watcher/watcher.py --dry-run --sources bremen
```

La simulation `--dry-run` n'envoie aucun e-mail et ne marque aucune offre comme vue.
Le déclenchement automatique ne fonctionne qu'une fois l'archive copiée dans
GitHub, les secrets présents et le workflow activé.

## Limites importantes

- Les pages changent : chaque collecteur est spécifique à l'HTML actuel.
- Emden/Leer repose sur un portail Coveto dynamique : si ses résultats ne sont
  pas lisibles après chargement, le script **signale un échec** plutôt que
  prétendre qu'il n'y a pas d'offres. Ce collecteur devra être validé sur un
  runner avec accès réseau réel avant de lui faire confiance.
- Les appels consultent `robots.txt`; un refus bloque la collecte du site.
- Une description en PDF ne peut être exploitée que si le lien PDF est public
  et textuel. Le script ne fait pas d'OCR.
- Des annonces avec descriptif inaccessible peuvent être sous-évaluées.
- Les descriptions pouvant contenir plusieurs sites, la ville inférée reste une
  approximation, notamment pour Jade et Emden/Leer. Le lien officiel doit être
  vérifié avant de postuler.
- Un passage via GitHub Actions + Playwright peut prendre quelques minutes.
- Le code a des tests unitaires hors ligne ; **la collecte réelle des quatre
  plateformes n'a pas encore été validée depuis ce contexte**.
