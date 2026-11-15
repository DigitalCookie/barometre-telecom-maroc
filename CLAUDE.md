# CLAUDE.md — Baromètre des prix télécoms Maroc

## Objectif
Relevé mensuel automatisé des offres **fibre, forfaits mobiles et box** de
Maroc Telecom (IAM), Orange Maroc et inwi, extraites de leurs **sites
officiels**, façon baromètre Ariase (ariase.com/barometre-prix).
Dashboard en production : https://digitalcookie.github.io/barometre-telecom-maroc/
(page unique `dashboard/index.html`, FR/AR, vues partageables par URL,
flux RSS, résumé éditorial mensuel, prix à l'unité DH/Go et DH/Mbps).

## Règle d'or du projet
**Seuls les sites officiels des opérateurs font foi comme source de prix.**
Ordre de préférence : HTML officiel > assets officiels (SVG, catalogues PDF
datés d'IAM) > rendu navigateur (Playwright) > presse spécialisée
(Médias24, TIC Maroc, LeBrief) UNIQUEMENT comme alerte de contrôle, jamais
comme valeur écrite dans le baromètre.

## État au 16/08/2026
- `barometre.py` fonctionne, `python barometre.py test` passe (parsers
  validés sur des extraits HTML réels des sites, capturés le 16/08/2026,
  plus des cas de non-régression ajoutés depuis).
- **Aucun run réel n'a encore été fait.** Retenté le 16/08/2026 depuis
  l'environnement Claude Code : les 6 hôtes du registre répondent
  `403 Forbidden` au CONNECT du proxy d'egress — refus de politique réseau,
  pas une refonte des sites. `python barometre.py check` rejoue ce
  diagnostic en 10 s. **Le premier `run` réel doit être lancé depuis une
  machine ayant une sortie vers les domaines `.ma`.**
- Le relevé manuel de référence (août 2026) est versionné dans
  `data/reference_manuelle_2026-08.csv` : grille fibre IAM 400/500/800/1000 DH
  (100M/200M/500M/1G), Orange et inwi alignés 249/299/349/449/749/949 DH
  (20M→1G). `python barometre.py compare` confronte automatiquement le
  dernier relevé à cette référence (rapprochement sur opérateur + catégorie +
  débit normalisé).
- Attention : ce fichier de référence contient des valeurs de presse
  (`fiabilite=presse_*`, hors schéma volontairement). Il sert **d'alerte de
  contrôle uniquement** — jamais de source écrite dans `barometre.csv`.

## Commandes
`check` (préflight réseau) · `test` (parsers, hors ligne) · `run` (relevé) ·
`replay` (re-parse les dumps `data/raw/` sans réseau — boucle de mise au
point des parsers, `--write` pour enregistrer, dates du relevé existant
préservées) · `backfill --from YYYY-MM [--write]` (historique reconstruit
depuis web.archive.org, pages rendues serveur uniquement, sortie
`officiel_archive` — API CDX très rate-limitée, backoff automatique) ·
`diff` (2 derniers mois) · `compare` (relevé vs référence) · `feed`
(régénère `data/changements.xml` + `data/resumes.json` — aussi fait par
`run` et `backfill --write`).

## Cartographie des sources (constats vérifiés)
| Page | Méthode | État du parser |
|---|---|---|
| iam.ma/fibre-optique | HTTP (Liferay, rendu serveur) | dédié, testé |
| iam.ma/forfaits-mobile | HTTP | dédié, testé |
| iam.ma/forfaits-exclusifs-5g (gamme Liberté, dès 59 DH) | Playwright | dédié (`parse_iam_liberte`), testé — ajouté 11/2026, la page n'était pas relevée avant |
| iam.ma/illimites-mobile | Playwright | dédié (`parse_iam_illimites`), testé — ajouté 11/2026 |
| pro.orange.ma/Fixe-et-Internet/Business-Box-Fibre | HTTP | dédié, testé — grille identique au résidentiel B2C aujourd'hui, **à surveiller si divergence** |
| orange.ma résidentiel fibre | cartes SVG `fibre-cards/20go.svg`…`1000go.svg` | contrôle croisé ; **vérifier que le prix est bien en texte dans le SVG** |
| inwi.ma fibre | HTTP | dédié, testé |
| boutique.orange.ma (forfaits Yo Max, Dar Box 5G/4G+) | Playwright (Next.js client) | dédié (cartes + slugs), testé |
| iam.ma/box-el-manzil-5g, /box-4g | Playwright | dédié (`parse_iam_box`), testé |
| inwi.ma forfaits mobile | Playwright | dédié, testé |
| yoxo.ma | Playwright | dédié (cartes + slugs), testé |

Astuce boutique Orange : les slugs d'URL encodent l'offre
(`forfait-yo-max-99dh-25go-1h-d-appel`) — extractibles sans exécuter le JS.

## Conventions à respecter
- CSV séparateur `;`, encodage `utf-8-sig` (Excel FR).
- Schéma : date_releve;operateur;categorie;offre;debit_ou_data;
  appels_inclus;prix_dh_mois;remarques;source;fiabilite
- `fiabilite` ∈ officiel_site | officiel_svg | officiel_catalogue |
  officiel_js_generique | officiel_archive | a_completer. Objectif : tout
  en `officiel_site` (ou `officiel_archive` pour l'historique backfillé).
- Chaque run sauvegarde le texte brut de chaque page dans
  `data/raw/YYYY-MM/` (piste d'audit — ne pas supprimer).
- Relancer `run` le même mois remplace le relevé du mois (pas de doublon).
- Une page qui rend 0 offre = signal que l'opérateur a refondu son site.

## Constats du premier run réel (16/08/2026, runner GitHub Actions)
- Le relevé mensuel tourne via `.github/workflows/barometre.yml` (le 2 du
  mois, 8h Maroc) et committe `data/` sur main. Runner GitHub : tous les
  domaines `.ma` joignables **sauf** les pages HTTP de iam.ma (403 sur IP
  datacenter) — mais iam.ma sert normalement le navigateur headless, donc
  ces pages sont passées en méthode `js`.
- Boutique Orange : prix rendus « ‎49,00 DH/mois » (décimales + marque
  U+200E) ; les grilles pro.orange.ma et inwi sont rendues 2-3× par page
  (desktop/mobile/éditorial) → dedup systématique dans les parsers.
- SVG Orange résidentiel : les paliers sont détectés mais le prix est
  vectorisé (pas de texte) → depuis 09/2026 ces paliers sont **ignorés**
  (plus de lignes vides), pro.orange.ma fait foi.

## iam.ma bloqué par Cloudflare (depuis 10/2026)
- iam.ma refuse tout accès automatisé (curl 403, Playwright headless et
  headed, runner GitHub comme IP résidentielle) ; aucune capture Wayback
  depuis 05/2026. Dès 10/2026 les pages fibre et El Manzil 5G rendaient la
  page de vérification (dumps de 305 octets) : IAM fibre et El Manzil
  manquent au relevé d'octobre. **Ne pas contourner la protection.**
- Rattrapage : lire les 6 pages IAM dans un vrai navigateur, déposer le
  texte dans `data/raw/YYYY-MM/iam-*.txt` (noms = `slugify(label)`), puis
  `replay --month YYYY-MM --write` et `feed`. Fait pour 2026-11 le 15/11.
- iam.ma affiche désormais les appels en heures (« 3H 30 min », « 14 H ») :
  les parsers les réécrivent en minutes (signature d'offre stable).
- `diff_changes` (et les diffs JS des deux pages) bornent nouveau/retiré au
  périmètre (opérateur, catégorie, **page source**) : une page ajoutée au
  relevé ou injoignable un mois n'est pas un mouvement de grille.

## Dashboard (dashboard/index.html)
- Page unique sans framework ; déployée par `.github/workflows/pages.yml`
  (copie CSV + changements.xml + resumes.json + og.png, bake le résumé du
  dernier mois dans le placeholder `RESUME_SEO` du HTML servi).
- i18n FR/AR : dictionnaire `I18N` dans le JS, bascule `lang-btn`
  (localStorage `bt-lang`, `dir=rtl`). Les VALEURS de données restent en
  français ; seuls les libellés d'affichage sont traduits (`catLabel`).
- État des filtres dans l'URL (`?mois&cat&ops&besoin&crit&q&lang`) —
  `readURL()` à l'init, `updateURL()` (replaceState) à chaque rendu.
- Thème clair/sombre : `bt-theme` en localStorage, re-render au toggle
  (les couleurs SVG sont lues via getComputedStyle au rendu).

## Garde-fou & diff
- `garde_fou(rows, month)` avant toute écriture dans `run` : compare au
  dernier mois complet non-archive ; seuils 40 % prix / 30 % disparus /
  50 % volume → publication bloquée (`--force` ou input `force` du
  workflow pour outrepasser une vraie refonte tarifaire).
- `diff_changes` détecte aussi `contenu` (shrinkflation : même prix,
  volume modifié — y compris via appariement nouveau×retiré au même
  prix) et `renomme` (même prix, même volume, autre nom).
- Indice Baromètre (dashboard, JS) : panier fibre ≥100 Mb/s + forfait
  ≥20 Go + box, base 100 au premier mois à couverture complète ; les
  mois d'archives partiels sont exclus du calcul.
- Annotations : `data/annotations.json` `[{date, texte, texte_ar}]` —
  pointillés sur le graphique d'évolution + liste dessous.
- Paniers d'usage (dashboard, JS `renderPaniers`) : comparaison des forfaits
  mobiles à besoin égal, pour neutraliser le biais des heures d'appels
  incluses (le DH/Go les ignore). Profils dans `data/paniers.json`
  `[{id, nom, nom_ar, go, heures, appels_illimites}]` (éditables via Pages
  CMS) ; par profil et par opérateur, le forfait le moins cher qui couvre
  data ET appels. Règles : `heuresOf` ne compte pas « illimité vers inwi »
  (on-net), lit « Appels illimités » dans le nom si le champ est vide ;
  offres sans Go ou sans heures publiés exclues (note sous le tableau).
- Prix moyen du Go à besoin égal (1er graphique du dashboard, `renderGoMoyen`) :
  par opérateur et par mois, moyenne sur les profils de (prix du forfait le
  moins cher couvrant le profil ÷ Go du profil) ; sélecteur par profil.
  Un opérateur n'a de point que si toutes ses pages de forfaits (référence =
  dernier relevé) sont présentes ce mois-là et qu'il couvre le(s) profil(s).
  Série possible depuis 2026-08 seulement (heures absentes des archives).
  Bascule « Prix brut (historique long) » : moyenne de prix ÷ Go par
  opérateur, abscisse proportionnelle au temps, points creux = relevé partiel
  (archives ou pages manquantes), pas de trait à travers un trou > 3 mois.
  Ligne « National » (pointillés) : moyenne des 3 opérateurs pondérée par
  leur parc Internet mobile ANRT (`anrt.json`, dernier trimestre clos),
  tracée seulement quand les 3 sont comparables (hors points partiels).
- Repère ANRT (`data/anrt.json`, sous le même graphique) : indicateurs
  trimestriels officiels — ARPM, facture Internet moyenne, minutes (open data
  data.gov.ma, ODbL, XLSX mis à jour chaque trimestre) + usage data par
  client et revenu moyen par Go (mémos PDF anrt.ma « Analyse des marchés »,
  relevés à la main ; le revenu/Go n'est publié que depuis T3-2025). À
  compléter à chaque nouveau mémo. Dérogation assumée à la règle d'or : ce
  sont des agrégats du régulateur, jamais des prix d'offres.
- Commentaires de l'admin : `data/commentaires.json` `{signature, mois:
  [{mois, texte, texte_ar, remplacer}], analyse: {essentiel, s1…s5, methodo}}`
  — édité par formulaire via Pages CMS (`.pages.yml`, app.pagescms.org),
  chaque enregistrement = commit sur main = redéploiement. Affiché dans
  « L'analyse du mois » (au-dessus du résumé auto, ou à sa place si
  `remplacer`) et en encadrés sur analyse.html. Jamais écrit par
  `barometre.py` : un relevé ne l'écrase pas.
- `backfill.yml` : cron le 15 du mois (rattrapage archives, sans
  Playwright) ; « rien à reconstruire » = exit 0.

## Backlog (dans l'ordre — détail dans README « À faire plus tard »)
1. Pack SEO (JSON-LD Dataset, sitemap, hreflang, pages mensuelles).
2. data/barometre.json (mini-API).
3. health.json « état des sources ».
4. % du SMIG.
5. Prépayé & pass (gros chantier — le vrai trou de couverture).
6. Comparaison internationale (source tierce en annexe).
7. Profils d'usage dans le finder.
8. Bonus : archiver les catalogues PDF mensuels d'IAM dans
   `data/catalogues/`.
