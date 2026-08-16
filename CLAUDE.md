# CLAUDE.md — Baromètre des prix télécoms Maroc

## Objectif
Relevé mensuel automatisé des offres **fibre, forfaits mobiles et box** de
Maroc Telecom (IAM), Orange Maroc et inwi, extraites de leurs **sites
officiels**, façon baromètre Ariase (ariase.com/barometre-prix).
À terme : un dashboard branché sur `data/barometre.csv`.

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
point des parsers, `--write` pour enregistrer) · `diff` (2 derniers mois) ·
`compare` (relevé vs référence).

## Cartographie des sources (constats vérifiés)
| Page | Méthode | État du parser |
|---|---|---|
| iam.ma/fibre-optique | HTTP (Liferay, rendu serveur) | dédié, testé |
| iam.ma/forfaits-mobile | HTTP | dédié, testé |
| pro.orange.ma/Fixe-et-Internet/Business-Box-Fibre | HTTP | dédié, testé — grille identique au résidentiel B2C aujourd'hui, **à surveiller si divergence** |
| orange.ma résidentiel fibre | cartes SVG `fibre-cards/20go.svg`…`1000go.svg` | contrôle croisé ; **vérifier que le prix est bien en texte dans le SVG** |
| inwi.ma fibre | HTTP | dédié, testé |
| boutique.orange.ma (forfaits Yo Max, Dar Box 5G/4G+) | Playwright (Next.js client) | `parse_generic` — à promouvoir en parser dédié |
| iam.ma/box-el-manzil-5g, /box-4g | Playwright | `parse_generic` — à promouvoir |
| inwi.ma forfaits mobile | Playwright | `parse_generic` — à promouvoir |
| yoxo.ma | Playwright | `parse_generic` — à promouvoir |

Astuce boutique Orange : les slugs d'URL encodent l'offre
(`forfait-yo-max-99dh-25go-1h-d-appel`) — extractibles sans exécuter le JS.

## Conventions à respecter
- CSV séparateur `;`, encodage `utf-8-sig` (Excel FR).
- Schéma : date_releve;operateur;categorie;offre;debit_ou_data;
  appels_inclus;prix_dh_mois;remarques;source;fiabilite
- `fiabilite` ∈ officiel_site | officiel_svg | officiel_catalogue |
  officiel_js_generique | a_completer. Objectif : tout en `officiel_site`.
- Chaque run sauvegarde le texte brut de chaque page dans
  `data/raw/YYYY-MM/` (piste d'audit — ne pas supprimer).
- Relancer `run` le même mois remplace le relevé du mois (pas de doublon).
- Une page qui rend 0 offre = signal que l'opérateur a refondu son site.

## Backlog (dans l'ordre)
1. **Bloqué ici, à faire sur une machine avec accès aux domaines `.ma`** :
   `pip install -r requirements.txt` + `playwright install chromium`, puis
   `python barometre.py check`, `test` et premier `run` réel.
   (Les deux premières étapes — install et `test` — sont faites et validées.)
2. `python barometre.py compare` après le premier run ; investiguer tout écart
   (outillé, il ne reste qu'à le lancer sur de vraies données).
3. Remplacer `parse_generic` par des parsers dédiés pour : boutique Orange
   (forfaits + Dar Box), forfaits inwi, box IAM, Yoxo — en s'appuyant sur
   les dumps `data/raw/` et en itérant avec `python barometre.py replay`.
4. Vérifier l'extraction des SVG Orange ; sinon, garder pro.orange.ma comme
   source principale et le noter en remarque. Le parser accepte un
   téléchargeur injectable (`fetch=`) : le test le couvre déjà hors ligne.
5. Bonus : archiver les catalogues PDF mensuels d'IAM (liens « Catalogue des
   offres » sur iam.ma) dans `data/catalogues/`.
6. Plus tard (ne pas commencer sans demande explicite) : dashboard —
   d'abord Excel/TCD, ensuite éventuellement Flask + Chart.js.
