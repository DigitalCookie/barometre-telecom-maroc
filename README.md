# Baromètre des prix télécoms Maroc

Relevé mensuel automatisé des offres **fibre, forfaits mobiles et box** de
Maroc Telecom (IAM), Orange Maroc et inwi, depuis leurs **sites officiels**
— façon baromètre Ariase.

**Dashboard** : https://digitalcookie.github.io/barometre-telecom-maroc/ —
comparateur d'offres, graphiques, analyse du mois, table triable (prix à
l'unité DH/Go et DH/Mbps), vues partageables par URL (`?mois=…&cat=…&ops=…`),
version arabe (bouton عربية), thème clair/sombre,
[flux RSS des changements de grille](https://digitalcookie.github.io/barometre-telecom-maroc/data/changements.xml)
et CSV téléchargeable.

## Installation (une fois)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # navigateur headless pour les pages JS
```

## Utilisation

```bash
python barometre.py check          # les sources répondent-elles ? (à faire avant tout run)
python barometre.py test           # valider les parsers (hors ligne)
python barometre.py run            # relevé complet du mois
python barometre.py run --no-js    # rapide : uniquement les pages HTML
python barometre.py run --only iam inwi
python barometre.py replay         # re-parser les dumps data/raw, sans réseau
python barometre.py backfill --from 2023-01 --write
                                   # reconstruire l'historique via web.archive.org
python barometre.py diff           # changements entre les 2 derniers mois
python barometre.py compare        # écarts entre le dernier relevé et la référence
python barometre.py feed           # régénérer flux RSS + résumés mensuels
```

`backfill` interroge l'API CDX de la Wayback Machine et re-parse les captures
des pages **rendues serveur** (IAM fibre/forfaits/box, grille pro Orange,
fibre inwi) pour les mois absents de la base. Les lignes sortent en
`fiabilite=officiel_archive` avec la capture exacte en `source`. L'API est
très rate-limitée : le run est lent (backoff automatique), c'est un one-off.

`check` sert à trancher la question qui revient à chaque anomalie : **site
refondu ou source injoignable ?** Un `run` qui ne ramène rien après un `check`
tout vert est un vrai signal de refonte ; sinon c'est le réseau.

`replay` rejoue les parsers sur les dumps déjà enregistrés dans `data/raw/` :
c'est la boucle de mise au point des parsers (aucune requête, résultat
immédiat, et on ne re-sollicite pas les sites des opérateurs). Sans `--write`
il affiche seulement un aperçu ; avec `--write` il réécrit le relevé du mois.

## Sorties

| Fichier | Contenu |
|---|---|
| `data/barometre.csv` | base cumulée, 1 ligne par offre et par mois (séparateur `;`, UTF-8 BOM → s'ouvre proprement dans Excel FR) |
| `data/releve_YYYY-MM.csv` | snapshot du mois |
| `data/changements.xml` | flux RSS des changements de grille (un item par mois) |
| `data/resumes.json` | résumé éditorial par mois — affiché par le dashboard et baké dans la page au déploiement (SEO) |
| `data/raw/YYYY-MM/*.txt` | texte brut de chaque page — piste d'audit et matière pour raffiner les parsers |
| `data/reference_manuelle_2026-08.csv` | relevé manuel de référence (août 2026), utilisé par `compare` |

Relancer `run` dans le même mois **remplace** le relevé du mois (pas de doublons).

Chaque relevé passe par des garde-fous : valeur de `fiabilite` conforme au
schéma, prix numérique et dans une fourchette plausible (20–3000 DH),
détection des doublons, décompte des lignes restant à fiabiliser.

## Planification mensuelle

Le workflow GitHub Actions `.github/workflows/barometre.yml` fait le relevé
le **2 de chaque mois à 8h (heure Maroc)** depuis un runner GitHub, puis
committe `data/` dans le dépôt. Il se lance aussi à la main : onglet
**Actions → Baromètre télécoms Maroc → Run workflow** (option `no_js` pour un
run rapide sans Playwright). Un run qui n'extrait rien fait échouer le job —
c'est l'alerte. Le détail complet s'affiche dans le résumé du job.

Alternative cron sur une machine perso :

```cron
# le 2 de chaque mois à 8h (laisser passer les changements du 1er)
0 8 2 * * cd /chemin/vers/barometre && .venv/bin/python barometre.py run >> run.log 2>&1
```

## Architecture des sources (constats du dry run 16/08/2026)

| Source | Méthode | Fiabilité |
|---|---|---|
| iam.ma fibre + forfaits + box | Playwright (Liferay bloque les IP datacenter en HTTP) | parsers dédiés, testés |
| pro.orange.ma (grille fibre) | HTTP simple | parser dédié, testé — grille identique au résidentiel, à surveiller |
| orange.ma résidentiel | fetch des cartes SVG (`20go.svg`…`1000go.svg`) | contrôle croisé du pro ; paliers sans prix texte ignorés |
| inwi.ma fibre | HTTP simple | parser dédié, testé |
| boutique.orange.ma (forfaits, Dar Box), inwi forfaits, yoxo.ma | Playwright | parsers dédiés (cartes + slugs d'URL), testés |

## Après le premier run réel

1. Ouvrir `data/releve_YYYY-MM.csv` et vérifier les lignes `officiel_js_generique`.
2. Lancer `python barometre.py compare` : les écarts avec le relevé de
   référence d'août 2026 pointent soit une vraie évolution tarifaire, soit un
   parser à corriger. En cas d'écart, **c'est le site officiel qui fait foi**.
3. S'appuyer sur les dumps `data/raw/` pour écrire un parser dédié par page
   (remplacer `parse_generic` dans le registre `PAGES` de `barometre.py`),
   en itérant avec `python barometre.py replay`.
4. Une page qui rend **0 offre** est signalée en fin de run : après un `check`
   vert, c'est le signal qu'un opérateur a changé la structure de son site.

## Dashboard

`dashboard/index.html` (page unique, zéro dépendance runtime) est déployé sur
GitHub Pages par `.github/workflows/pages.yml` à chaque commit touchant
`dashboard/` ou `data/`. Le déploiement copie les CSV, le flux RSS et les
résumés dans `_site/data/`, et bake le résumé du dernier mois dans le HTML
servi (placeholder `RESUME_SEO`).
