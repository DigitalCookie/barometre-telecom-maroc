# Baromètre des prix télécoms Maroc

Relevé mensuel automatisé des offres **fibre, forfaits mobiles et box** de
Maroc Telecom (IAM), Orange Maroc et inwi, depuis leurs **sites officiels**
— façon baromètre Ariase.

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
python barometre.py diff           # changements entre les 2 derniers mois
python barometre.py compare        # écarts entre le dernier relevé et la référence
```

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
| `data/raw/YYYY-MM/*.txt` | texte brut de chaque page — piste d'audit et matière pour raffiner les parsers |
| `data/reference_manuelle_2026-08.csv` | relevé manuel de référence (août 2026), utilisé par `compare` |

Relancer `run` dans le même mois **remplace** le relevé du mois (pas de doublons).

Chaque relevé passe par des garde-fous : valeur de `fiabilite` conforme au
schéma, prix numérique et dans une fourchette plausible (20–3000 DH),
détection des doublons, décompte des lignes restant à fiabiliser.

## Planification mensuelle (cron)

```cron
# le 2 de chaque mois à 8h (laisser passer les changements du 1er)
0 8 2 * * cd /chemin/vers/barometre && .venv/bin/python barometre.py run >> run.log 2>&1
```

## Architecture des sources (constats du dry run 16/08/2026)

| Source | Méthode | Fiabilité |
|---|---|---|
| iam.ma fibre + forfaits | HTTP simple (rendu serveur) | parser dédié, testé |
| pro.orange.ma (grille fibre) | HTTP simple | parser dédié, testé — grille identique au résidentiel, à surveiller |
| orange.ma résidentiel | fetch des cartes SVG (`20go.svg`…`1000go.svg`) | contrôle croisé du pro |
| inwi.ma fibre | HTTP simple | parser dédié, testé |
| boutique.orange.ma (forfaits, Dar Box), inwi forfaits, box IAM, yoxo.ma | Playwright | **parser générique** : lignes marquées `officiel_js_generique`, à relire au 1er run |

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

## Étape suivante (à décider)

Dashboard de visualisation branché sur `data/barometre.csv` : Excel + TCD
pour commencer, ou petite app Flask/Chart.js pour la version site.
