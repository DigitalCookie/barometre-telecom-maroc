#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baromètre des prix télécoms Maroc — IAM / Orange / inwi
========================================================
Extraction mensuelle des offres fibre, forfaits mobiles et box depuis les
SITES OFFICIELS des opérateurs (règle du projet : jamais la presse comme
source de valeur, uniquement comme alerte de contrôle).

Stratégie par source (constats du dry run du 16/08/2026) :
  - iam.ma ................... rendu serveur (Liferay)  -> requests + regex
  - pro.orange.ma ............ rendu serveur            -> requests + regex
  - orange.ma (résidentiel) .. prix dans des SVG        -> fetch des SVG + parse
  - boutique.orange.ma ....... Next.js côté client      -> Playwright
  - inwi.ma fibre ............ rendu serveur            -> requests + regex
  - inwi.ma forfaits, box .... rendu JS                 -> Playwright

Usage :
  python barometre.py check               # joignabilité des sources (préflight)
  python barometre.py run                 # relevé complet du mois
  python barometre.py run --only iam inwi # filtrer par opérateur
  python barometre.py run --no-js         # sauter les pages Playwright
  python barometre.py replay              # re-parser les dumps data/raw (hors ligne)
  python barometre.py backfill --from 2023-01 --write
                                          # historique via web.archive.org
  python barometre.py test                # valider les parsers sur échantillons
  python barometre.py diff                # comparer les 2 derniers relevés
  python barometre.py compare             # confronter le relevé au releve de reference
  python barometre.py feed                # régénérer flux RSS + résumés

Sorties :
  data/barometre.csv            base cumulée (une ligne par offre et par mois)
  data/releve_YYYY-MM.csv       snapshot du mois
  data/changements.xml          flux RSS des changements de grille
  data/resumes.json             résumé éditorial par mois (dashboard + SEO)
  data/raw/YYYY-MM/*.txt        texte brut de chaque page (audit / débogage)
"""

import argparse
import csv
import datetime as dt
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration générale
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
# Dumps des captures d'archives (backfill) : même boucle d'itération des
# parsers que data/raw, mais pour les pages historiques.
RAW_ARCHIVE_DIR = DATA_DIR / "raw_archive"
MASTER_CSV = DATA_DIR / "barometre.csv"
REFERENCE_CSV = DATA_DIR / "reference_manuelle_2026-08.csv"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HTTP_HEADERS = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"}
TIMEOUT = 30
RETRIES = 3            # tentatives HTTP (backoff 2s, 4s)

FIELDNAMES = [
    "date_releve", "operateur", "categorie", "offre", "debit_ou_data",
    "appels_inclus", "prix_dh_mois", "remarques", "source", "fiabilite",
]

# Valeurs autorisées pour la colonne fiabilite (cf. CLAUDE.md).
# officiel_archive : relevé rétrospectif reconstruit depuis une capture
# web.archive.org d'une page officielle (commande `backfill`).
FIABILITES = {"officiel_site", "officiel_svg", "officiel_catalogue",
              "officiel_js_generique", "officiel_archive", "a_completer"}

# Adresse publique du dashboard (flux RSS, liens du résumé).
SITE_URL = "https://digitalcookie.github.io/barometre-telecom-maroc/"

# Fourchette plausible d'un prix mensuel en DH : hors bornes = extraction
# suspecte (un numéro de téléphone, un débit, un prix d'équipement…).
PRIX_MIN, PRIX_MAX = 20, 3000


def today() -> str:
    return dt.date.today().isoformat()


def slugify(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", txt.lower()).strip("-")


def norm(text: str) -> str:
    """Normalise le texte extrait : espaces insécables, blancs multiples."""
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    return re.sub(r"\s+", " ", text)


def row(op, cat, offre, debit="", appels="", prix="", remarques="",
        source="", fiabilite="officiel_site"):
    return {
        "date_releve": today(), "operateur": op, "categorie": cat,
        "offre": offre, "debit_ou_data": debit, "appels_inclus": appels,
        "prix_dh_mois": prix, "remarques": remarques, "source": source,
        "fiabilite": fiabilite,
    }

# --------------------------------------------------------------------------
# Récupération des pages
# --------------------------------------------------------------------------


def fetch_html(url: str, retries: int = RETRIES) -> str:
    """GET avec retries : les sites opérateurs coupent parfois la connexion."""
    delay, last = 2, None
    for essai in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last = exc
            if essai < retries:
                time.sleep(delay)
                delay *= 2
    raise last


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator="\n")


class Navigateur:
    """Navigateur Playwright partagé par toutes les pages JS d'un run.

    Un lancement de Chromium par page coûtait ~3 s inutiles ; on ouvre le
    navigateur une seule fois et on ferme juste l'onglet entre deux pages."""

    def __init__(self):
        self._pw = self._browser = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _ensure(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        return self._browser

    def text(self, url: str, settle_ms: int = 5000, clicks=(),
             links: bool = False) -> str:
        """Texte visible d'une page après exécution du JavaScript.

        `clicks` : libellés d'onglets à cliquer après le chargement — le texte
        de chaque état est concaténé, les parsers dédoublonnent. Best effort.
        `links` : ajoute en fin de dump la liste des URL des liens de la page
        (section LIENS) — utile quand les slugs encodent l'offre (boutique
        Orange) et que le contenu correspondant n'est pas rendu sans clic."""
        page = self._ensure().new_page(user_agent=UA, locale="fr-FR")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Bannières cookies connues (IAM, inwi, Orange) — best effort.
            for label in ("Tout accepter", "J'ACCEPTE", "J’ACCEPTE",
                          "Accepter", "Accept all"):
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=1500)
                    break
                except Exception:
                    pass
            page.wait_for_timeout(settle_ms)
            text = page.inner_text("body")
            for label in clicks:
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=2500)
                    page.wait_for_timeout(1200)
                    text += "\n" + page.inner_text("body")
                except Exception:
                    pass
            if links:
                try:
                    hrefs = page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)")
                    text += "\nLIENS\n" + "\n".join(dict.fromkeys(hrefs))
                except Exception:
                    pass
            return text
        finally:
            page.close()

    def close(self):
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()
            self._pw = self._browser = None


def rendered_text(url: str, settle_ms: int = 5000) -> str:
    """Version autonome (un navigateur jetable) — pratique en usage manuel."""
    with Navigateur() as nav:
        return nav.text(url, settle_ms)

# --------------------------------------------------------------------------
# Parsers — chaque fonction reçoit le TEXTE d'une page, rend des lignes CSV.
# Les regex sont validées sur les échantillons réels du 16/08/2026 (cf. test).
# --------------------------------------------------------------------------


def parse_iam_fibre(text, url):
    """iam.ma/fibre-optique — motif « 400 DH/mois 100 Mb/s … »."""
    t = norm(text)
    rows = []
    pat = re.compile(
        r"(\d[\d ]{0,5})\s*DH/mois\s+(\d+)\s*(Gb/s|Mb/s)(.{0,160}?)"
        r"(?:Frais Installation|Acheter|\Z)", re.S)
    for prix, debit, unite, suite in pat.findall(t):
        appels = norm(suite)
        appels = re.sub(r"\[.*?\]|\(.*?\)", "", appels).strip(" -*·")
        rows.append(row("Maroc Telecom", "Fibre",
                        f"Fibre Optique {debit}{'G' if unite.startswith('G') else 'M'}",
                        f"{debit} {unite}", appels[:120],
                        prix.replace(" ", ""), source=url))
    return rows


def parse_iam_forfaits(text, url):
    """iam.ma/forfaits-mobile — « 165 DH/mois 30 Go 180 min [1 Go Roaming] »."""
    t = norm(text)
    rows = []
    pat = re.compile(r"(\d+)\s*DH/mois\s+(\d+)\s*Go\s+(\d+)\s*min"
                     r"(?:\s*(\d+)\s*Go\s*Roaming)?")
    for prix, go, mn, roam in pat.findall(t):
        rem = f"+{roam} Go roaming Z1" if roam else ""
        rows.append(row("Maroc Telecom", "Forfait mobile",
                        f"Liberte Plus {prix} ({go} Go)",
                        f"{go} Go", f"{mn} min", prix, rem, url))
    return rows


def parse_iam_forfaits_liberte(text, url):
    """iam.ma/forfaits-mobile — ANCIEN format (gamme « Forfait Liberté »,
    observé sur les captures d'archives jusqu'à début 2026) :
    « Forfait Liberté 99 DH/mois 20 Go 1 Heure Pass et options Acheter ».
    Go / Heures / SMS apparaissent dans un ordre variable, et plusieurs
    bundles différents existent au même prix — le libellé embarque la data
    (ou les heures) pour les distinguer."""
    t = norm(text)
    rows = []
    pat = re.compile(r"Forfait Libert[ée]\s+(\d{2,3})\s*DH/mois\s*(.{0,80}?)"
                     r"(?:Pass et options|Acheter|Forfait Libert|\Z)", re.S)
    for prix, corps in pat.findall(t):
        go = re.search(r"(\d+)\s*Go\b", corps)
        heures = re.search(r"(\d+)\s*Heures?\b", corps)
        sms = re.search(r"(\d+)\s*SMS\b", corps)
        distingue = f"{go.group(1)} Go" if go else \
                    f"{heures.group(1)}h" if heures else ""
        appels = f"{heures.group(1)}h" if heures else ""
        rem = f"{sms.group(1)} SMS" if sms else ""
        rows.append(row("Maroc Telecom", "Forfait mobile",
                        f"Forfait Liberte {prix} ({distingue})".strip(),
                        go.group(1) + " Go" if go else "", appels, prix,
                        rem, url))
    return dedup(rows, key=lambda r_: (r_["offre"], r_["prix_dh_mois"],
                                       r_["appels_inclus"]))


def parse_iam_box(text, url, produit):
    """iam.ma box — « El Manzil 5G 400 DH/mois 100 Mb/s* » et
    « Box 4G+ 199 DH/mois 60 min 60 Go Acheter » (dumps réels 09/2026).
    Les pass internet en bas de page (« 20 DH 2 Go ») n'ont pas de
    « /mois » : l'ancre du prix mensuel les écarte d'office."""
    t = norm(text)
    rows = []
    pat = re.compile(
        rf"{re.escape(produit)}\s+(\d{{2,4}})\s*DH/mois\s*(.{{0,100}}?)"
        r"(?:Acheter|Frais|\Z)", re.I | re.S)
    for prix, corps in pat.findall(t):
        debit = " / ".join(dict.fromkeys(
            re.findall(r"\d+\s*(?:Go|Mb/s|M[ée]ga)", corps)))[:40]
        minutes = " / ".join(dict.fromkeys(
            re.findall(r"\d+\s*min\b", corps)))[:30]
        rows.append(row("Maroc Telecom", "Box", f"{produit} {prix} DH",
                        debit, minutes, prix, source=url))
    return dedup(rows)


def dedup(rows, key=lambda r_: (r_["offre"], r_["prix_dh_mois"])):
    """Les pages rendent souvent la même grille plusieurs fois (desktop +
    mobile, carrousels) : on garde la première occurrence de chaque offre."""
    vus, uniq = set(), []
    for r_ in rows:
        k = key(r_)
        if k not in vus:
            vus.add(k)
            uniq.append(r_)
    return uniq


def parse_orange_pro_fibre(text, url):
    """pro.orange.ma Business Box Fibre — « 20 Méga 249 Dh /mois » (run réel
    08/2026 : libellé éclaté sur plusieurs lignes, espace avant /mois, et
    grille rendue deux fois — desktop + mobile)."""
    t = norm(text)
    rows = []
    for mega, prix in re.findall(
            r"(\d+)\s*M[ée]ga\s*[·:\-\s]*(\d+)\s*Dh?s?\s*/\s*mois", t, re.I):
        rows.append(row("Orange", "Fibre", f"Fibre {mega} Mega",
                        f"{mega} Mb/s", "", prix,
                        "Grille pro.orange.ma (identique au residentiel au 08/2026 - a surveiller)",
                        url))
    return dedup(rows)


def parse_orange_svg_fibre(html, url, fetch=None):
    """orange.ma résidentiel — les cartes tarifaires sont des SVG nommés
    « 20go.svg » … « 1000go.svg ». Le nom donne le palier, le contenu XML
    du SVG contient normalement le prix en texte. Contrôle croisé du pro.

    `fetch` est injectable pour tester hors ligne."""
    fetch = fetch or fetch_html
    rows = []
    # On garde l'URL réellement présente dans la page (le CDN peut changer de
    # nom d'hôte) et on trie les paliers numériquement, pas alphabétiquement.
    trouves = re.findall(
        r"(https://[\w.\-]+/FibreOrange/fibre-cards/(\d+)go\.svg)", html, re.I)
    paliers = sorted({(int(mega), u) for u, mega in trouves})
    for mega_int, svg_url in paliers:
        mega = str(mega_int)
        prix, note = "", "prix non trouve dans le SVG - a inspecter"
        try:
            svg = fetch(svg_url)
            svg_text = norm(BeautifulSoup(svg, "html.parser").get_text(" "))
            # premier nombre a 3-4 chiffres qui n'est pas le palier lui-meme
            for cand in re.findall(r"\b(\d{3,4})\b", svg_text):
                if cand != mega:
                    prix, note = cand, "extrait du SVG officiel"
                    break
        except Exception as exc:  # SVG inaccessible : on garde le palier
            note = f"SVG non recupere ({exc.__class__.__name__})"
        if not prix:
            # Prix dessiné en tracés vectoriels (pas de texte dans le SVG) :
            # une ligne sans prix polluait le baromètre pour rien — la grille
            # pro.orange.ma sert déjà de source résidentielle. On journalise.
            print(f"    (svg) palier {mega} Mega ignoré : {note}")
            continue
        rows.append(row("Orange", "Fibre", f"Fibre {mega} Mega (residentiel)",
                        f"{mega} Mb/s", "", prix, note, svg_url,
                        "officiel_svg"))
    return rows


def parse_inwi_fibre(text, url):
    """inwi.ma fibre — « Forfait 20 Méga 249dh » + palier Giga éventuel."""
    t = norm(text)
    rows = []
    for mega, prix in re.findall(r"(\d+)\s*M[ée]ga\s*[:\-\s]*(\d+)\s*dh", t, re.I):
        rows.append(row("inwi", "Fibre", f"Fibre optique {mega} Mega",
                        f"{mega} Mb/s", "", prix, source=url))
    for prix in re.findall(r"1\s*Gi?ga?(?:bps)?\s*[:\-\sà]*(\d{3,4})\s*dh", t, re.I):
        rows.append(row("inwi", "Fibre", "Fibre optique 1G", "1 Gb/s", "",
                        prix, source=url))
    # Run réel 08/2026 : la grille apparaît 3 fois dans la page (texte
    # éditorial + deux rendus de cartes) — d'où le dedup.
    return dedup(rows)


def parse_orange_boutique_forfaits(text, url):
    """boutique.orange.ma/offres-mobile — cartes « Forfait YO 3h + 3Go 49 Dh
    … ‎49,00 DH/mois » (run réel 08/2026). Le titre porte le nom et le prix,
    le corps de carte les Go/heures ; le prix « ,00 » confirme la carte."""
    t = norm(text)
    rows = []
    # On ancre sur le prix mensuel (« ‎49,00 DH/mois » — jamais présent dans
    # le menu de navigation, qui liste pourtant « Forfait Yo Max 5G 99 Dh »)
    # puis on remonte au dernier titre de carte qui le précède. Ancrer sur le
    # titre absorbait la première carte quand un titre de menu traînait avant.
    titre_pat = re.compile(r"Forfait\s+(?:YO|Yo)[^‎]{0,60}?\d{2,3}\s*Dh\b", re.I)
    for m in re.finditer(r"(\d{2,3})[.,]\d{2}\s*DH\s*/\s*mois", t):
        prix = m.group(1)
        fenetre = t[max(0, m.start() - 300):m.start()]
        titres = list(titre_pat.finditer(fenetre))
        if not titres:
            continue
        titre = norm(titres[-1].group(0)).strip()
        if not re.search(rf"\b{prix}\s*Dh$", titre, re.I):
            continue                # titre et prix mensuel discordants : suspect
        corps = fenetre[titres[-1].end():]
        data = " / ".join(dict.fromkeys(
            re.findall(r"\d+\s*Go", corps, re.I)))[:60]
        heures = " / ".join(dict.fromkeys(
            re.findall(r"\d+\s*[Hh]\b(?:\s*d'appels)?", corps)))[:40]
        rem = "illimite reseaux sociaux" if re.search(
            r"R[ée]seaux Sociaux|WhatsApp illimit", corps, re.I) else ""
        rows.append(row("Orange", "Forfait mobile", titre,
                        data, heures, prix, rem, url))
    rows = dedup(rows)

    # Les cartes Yo Max ne sont pas rendues sans clic d'onglet, mais les
    # slugs d'URL encodent chaque offre. On ne garde un slug que s'il n'a
    # pas déjà été vu en carte (même prix + même data) pour ne pas doubler.
    vus = {(r_["prix_dh_mois"], m.group(0)) for r_ in rows
           for m in [re.search(r"\d+(?=\s*Go)", r_["debit_ou_data"], re.I)] if m}
    for slug in set(re.findall(r"forfait-yo-max[\w-]*", text, re.I)):
        offre = decoder_slug_yomax(slug)
        if offre and (offre["prix_dh_mois"],
                      offre["debit_ou_data"].split(" ")[0]) not in vus:
            rows.append(offre | {"source": url})
    return dedup(rows)


def decoder_slug_yomax(slug):
    """Décode un slug boutique Yo Max, quel que soit l'ordre des jetons
    (constats du run du 16/08/2026) :
      forfait-yo-max-99dh-25go-1h-d-appel
      forfait-yo-max-52go-10h-d-appels-199dh
      forfait-yo-max-80go-2go-roaming-299dh
      forfait-yo-max-illimite-national-120go-5go-roaming-399dh
      forfait-yo-max-tout-illimite-10go-roaming-649dh-5-services"""
    s = slug.lower()
    prix_m = re.search(r"(\d{2,3})-?dhs?\b", s)
    if not prix_m:
        return None
    prix = prix_m.group(1)
    roam = re.search(r"(\d{1,2})go-roaming", s)
    gos = [g for g in re.findall(r"(\d{1,3})go\b", s)
           if not (roam and g == roam.group(1))]
    if gos:
        data = f"{gos[0]} Go"
    elif "tout-illimite" in s:
        data = "Tout illimite"
    else:
        data = ""
    h = re.search(r"\b(\d{1,2})h(?:-d-appels?)?\b", s)
    appels = ("Illimite national" if "illimite-national" in s
              else "Tout illimite" if "tout-illimite" in s
              else f"{h.group(1)}h" if h else "")
    rem = f"+{roam.group(1)} Go roaming" if roam else ""
    return row("Orange", "Forfait mobile", f"Forfait Yo Max {prix} Dh",
               data, appels, prix,
               (rem + " - detail du slug boutique").strip(" -"), "")


def parse_orange_darbox(text, url, variante):
    """boutique.orange.ma Dar Box — carte « Dar Box 5G 299Dh Internet illimité
    50 Méga 3H d'appels … ‎299,00 DH/mois … Frais de mise en service 299 Dh »
    (run réel 08/2026). `variante` : « 5G » ou « 4G+ »."""
    t = norm(text)
    rows = []
    pat = re.compile(
        rf"Dar Box {re.escape(variante)}\s*(\d{{2,3}})\s*(?:Dh)?\b"
        r"(.{0,220}?)‎?(\d{2,3})[.,]\d{2}\s*DH\s*/\s*mois"
        r"(?:.{0,80}?Frais de mise en service\s*(\d{2,3})\s*Dh)?", re.I)
    for prix_titre, corps, prix, frais in pat.findall(t):
        if prix_titre != prix:
            continue
        debit = " / ".join(re.findall(r"\d+\s*M[ée]ga", corps))[:30] \
            or "Internet illimite"
        heures = " / ".join(re.findall(r"\d+\s*H\b", corps))[:30]
        rem = f"Frais de mise en service {frais} DH" if frais else ""
        rows.append(row("Orange", "Box", f"Dar Box {variante} {prix} DH",
                        debit, heures, prix, rem, url))
    return dedup(rows)


def parse_yoxo(text, url):
    """yoxo.ma — carte « 20GO 1H d'appels* SMS illimité* 50 DHS /mois »
    (run réel 08/2026). La page ne rend que les premiers paliers : les
    autres arrivent par les slugs (« forfait-yoxo-200dhs »), prix seul."""
    t = norm(text)
    rows = []
    pat = re.compile(r"(\d{1,3})\s*GO\s+(\d{1,2})\s*H\s*d'appels.{0,80}?"
                     r"(\d{2,3})\s*DHS?\s*/\s*mois", re.I)
    for go, heures, prix in pat.findall(t):
        rows.append(row("Orange", "Forfait mobile", f"Yoxo {prix} DH",
                        f"{go} Go", f"{heures}h",
                        prix, "100% digital sans engagement", url))
    rows = dedup(rows)
    deja = {r_["prix_dh_mois"] for r_ in rows}
    for prix in set(re.findall(r"forfait-yoxo-(\d{2,3})dhs?", text, re.I)):
        if prix not in deja:
            rows.append(row("Orange", "Forfait mobile", f"Yoxo {prix} DH",
                            "", "", prix,
                            "prix du slug - detail data a completer", url))
    return dedup(rows)


def parse_inwi_forfaits(text, url):
    """inwi.ma forfaits mobile — carte « Forfait 18Go + 5H + WhatsApp Illimité
    99 Dhs/mois … 18Go d'internet … 2h d'appels » (run réel 08/2026).
    Le corps de carte fait foi pour les Go/heures ; le titre sert de libellé."""
    t = norm(text)
    rows = []
    # « Forfait » avec F majuscule uniquement : en insensible à la casse, le
    # bouton « JE CHOISIS MON FORFAIT » et l'entête « Les forfaits Max… »
    # de la carte précédente contaminaient le titre (constat replay 08/2026).
    pat = re.compile(
        r"Forfait\s+([^\s].{2,60}?)\s+(\d{2,4})\s*[Dd]hs?\s*/\s*mois"
        r"(.*?)(?=JE CHOISIS|Forfait\s+[^\s].{2,60}?\d{2,4}\s*[Dd]hs?\s*/|\Z)",
        re.S)
    for titre, prix, corps in pat.findall(t):
        titre = re.sub(r"^(?:Nouveau|FLEXI)\s+(?=Forfait\s)|^Forfait\s+", "",
                       norm(titre).strip())
        data = " / ".join(dict.fromkeys(
            re.findall(r"(\d+\s*Go)(?=\s*(?:d'internet|Roaming|en roaming))",
                       corps, re.I)))[:60]
        heures = " / ".join(dict.fromkeys(
            re.findall(r"\d+\s*[Hh]\b(?=\s*d'appels)", corps)))[:40]
        appels = heures
        if re.search(r"[Aa]ppels illimit[ée]s vers (?:les num[ée]ros )?inwi", corps):
            appels = (appels + " + illimite vers inwi").strip(" +")
        rem = "WhatsApp/RS illimites" if re.search(
            r"Whatsapp illimit|R[ée]seaux sociaux illimit", corps, re.I) else ""
        rows.append(row("inwi", "Forfait mobile", f"Forfait {norm(titre).strip()}",
                        data, appels, prix, rem, url))
    return dedup(rows)


def parse_generic(text, url, operateur, categorie, offre_prefixe):
    """Filet générique pour les pages JS dont la structure n'est pas encore
    connue (box IAM…) : repère chaque prix mensuel et capture le contexte
    (Go / Méga / heures) autour.
    Les lignes sortent en fiabilite=officiel_js_generique : à relire au
    premier run, puis à promouvoir en parser dédié."""
    t = norm(text)
    rows = []
    # (?<![\d.,]) : ne pas démarrer au milieu d'un nombre — « ‎49,00 DH/mois »
    # capturait « 00 » (run réel 08/2026). Décimales optionnelles ensuite.
    prix_pat = re.compile(r"(?<![\d.,])(\d[\d ]{0,4})(?:[.,]\d{1,2})?"
                          r"\s*(?:Dh|DH|dhs?)\s*(?:TTC\s*)?/\s*mois",
                          re.I)
    matches = list(prix_pat.finditer(t))
    if not matches:
        return rows

    def rattacher(pattern, portee):
        """Chaque caractéristique va au prix le plus proche, dans la limite de
        `portee` caractères. Sans ce rattachement au plus proche, les offres
        empilées dans le DOM se contaminent (les Go du voisin arrivent ici)."""
        paniers = [[] for _ in matches]
        for tok in re.finditer(pattern, t):
            dists = [min(abs(tok.start() - m.end()), abs(m.start() - tok.end()))
                     for m in matches]
            i = min(range(len(matches)), key=lambda k: dists[k])
            if dists[i] <= portee:
                paniers[i].append(tok.group(0).strip())
        return paniers

    data_par_prix = rattacher(r"\d+\s*(?:Go|Mo|M[ée]ga|Mb/s|Gb/s)", 90)
    heures_par_prix = rattacher(r"\d+\s*[Hh](?:eures)?\b|\d+\s*min", 90)
    for i, m in enumerate(matches):
        prix = m.group(1).replace(" ", "")
        data = " / ".join(dict.fromkeys(data_par_prix[i]))[:60]
        heures = " / ".join(dict.fromkeys(heures_par_prix[i]))[:40]
        rows.append(row(operateur, categorie,
                        f"{offre_prefixe} {prix} DH", data, heures, prix,
                        "extraction generique - verifier et raffiner le parser",
                        url, "officiel_js_generique"))
    # dédoublonnage (le même prix apparaît souvent 2x dans le DOM)
    seen, uniq = set(), []
    for r_ in rows:
        key = (r_["offre"], r_["debit_ou_data"])
        if key not in seen:
            seen.add(key)
            uniq.append(r_)
    return uniq

# --------------------------------------------------------------------------
# Registre des pages à relever
# --------------------------------------------------------------------------

PAGES = [
    # ------------------------------------------------------ Maroc Telecom
    # Run réel 08/2026 : iam.ma répond 403 aux requêtes HTTP simples depuis
    # les IP datacenter mais sert normalement le navigateur headless — les
    # pages Liferay passent donc par Playwright (contenu identique).
    dict(op="iam", label="IAM fibre", method="js",
         url="https://www.iam.ma/fibre-optique",
         parse=lambda txt, u: parse_iam_fibre(txt, u)),
    dict(op="iam", label="IAM forfaits mobile", method="js",
         url="https://www.iam.ma/forfaits-mobile",
         parse=lambda txt, u: parse_iam_forfaits(txt, u)),
    dict(op="iam", label="IAM Box El Manzil 5G", method="js",
         url="https://www.iam.ma/box-el-manzil-5g",
         parse=lambda txt, u: parse_iam_box(txt, u, "El Manzil 5G")),
    dict(op="iam", label="IAM Box 4G+", method="js",
         url="https://www.iam.ma/box-4g",
         parse=lambda txt, u: parse_iam_box(txt, u, "Box 4G+")),
    # ------------------------------------------------------------- Orange
    dict(op="orange", label="Orange fibre (grille pro, HTML)", method="http",
         url="https://pro.orange.ma/Fixe-et-Internet/Business-Box-Fibre",
         parse=lambda txt, u: parse_orange_pro_fibre(txt, u)),
    dict(op="orange", label="Orange fibre residentiel (SVG)", method="html_raw",
         url="https://www.orange.ma/WiFi-a-la-Maison/Fibre-d-Orange/Offres-Fibre-d-Orange",
         parse=lambda html, u: parse_orange_svg_fibre(html, u)),
    # Les cartes Yo Max sont derrière un onglet non rendu au chargement et
    # les clics sont sans effet (constat des runs du 16/08/2026) : on
    # collecte les liens de la page, leurs slugs encodent chaque offre.
    dict(op="orange", label="Orange forfaits (boutique)", method="js",
         url="https://boutique.orange.ma/offres-mobile", links=True,
         parse=lambda txt, u: parse_orange_boutique_forfaits(txt, u)),
    dict(op="orange", label="Orange Dar Box 5G", method="js",
         url="https://boutique.orange.ma/offres-dar-box/dar-box-5g",
         parse=lambda txt, u: parse_orange_darbox(txt, u, "5G")),
    dict(op="orange", label="Orange Dar Box 4G+", method="js",
         url="https://boutique.orange.ma/dar-box",
         parse=lambda txt, u: parse_orange_darbox(txt, u, "4G+")),
    dict(op="orange", label="Yoxo (digital)", method="js",
         url="https://www.yoxo.ma/", links=True,
         parse=lambda txt, u: parse_yoxo(txt, u)),
    # --------------------------------------------------------------- inwi
    dict(op="inwi", label="inwi fibre", method="http",
         url="https://inwi.ma/fr/particuliers/offres-internet/wifi-a-la-maison/fibre-optique",
         parse=lambda txt, u: parse_inwi_fibre(txt, u)),
    dict(op="inwi", label="inwi forfaits mobile", method="js",
         url="https://inwi.ma/fr/particuliers/offres-mobiles/forfait-mobile",
         parse=lambda txt, u: parse_inwi_forfaits(txt, u)),
]

# --------------------------------------------------------------------------
# Exécution d'un relevé
# --------------------------------------------------------------------------


def pages_selectionnees(only=None, no_js=False):
    for page in PAGES:
        if only and page["op"] not in only:
            continue
        if no_js and page["method"] == "js":
            print(f"  [skip JS] {page['label']}")
            continue
        yield page


def run(only=None, no_js=False, force=False):
    month = today()[:7]
    raw_month_dir = RAW_DIR / month
    raw_month_dir.mkdir(parents=True, exist_ok=True)

    all_rows, failures = [], []
    with Navigateur() as nav:
        for page in pages_selectionnees(only, no_js):
            print(f"  [{page['method']:>8}] {page['label']} …", end=" ", flush=True)
            try:
                if page["method"] == "http":
                    content = html_to_text(fetch_html(page["url"]))
                elif page["method"] == "html_raw":
                    content = fetch_html(page["url"])
                else:  # js
                    content = nav.text(page["url"], clicks=page.get("clicks", ()),
                                       links=page.get("links", False))
                (raw_month_dir / f"{slugify(page['label'])}.txt").write_text(
                    content, encoding="utf-8")
                rows = page["parse"](content, page["url"])
                all_rows.extend(rows)
                print(f"{len(rows)} offre(s)")
                if not rows:
                    failures.append((page["label"], "0 offre extraite — "
                                     "structure de page modifiée ?"))
            except Exception as exc:
                print(f"ECHEC ({exc.__class__.__name__}: {exc})")
                failures.append((page["label"], str(exc)))

    if not all_rows:
        print("\nAucune donnée extraite — rien n'est écrit.")
        print("Vérifier d'abord la joignabilité des sources : "
              "python barometre.py check")
        return 1

    alertes = garde_fou(all_rows, month)
    if alertes:
        print("\n=== GARDE-FOU DE PUBLICATION ===")
        for a_ in alertes:
            print(f"  ! {a_}")
        if not force:
            print("\nDivergence massive vs le dernier relevé : rien n'est "
                  "écrit.\nSi ces changements sont réels (refonte tarifaire "
                  "générale), relancer :\n  python barometre.py run --force "
                  "(ou l'option force du workflow).")
            return 2
        print("  (--force : publication malgré les alertes)")
    ecrire_releve(all_rows, month)
    if failures:
        print("\nPoints d'attention :")
        for label, msg in failures:
            print(f"  - {label}: {msg}")
    signaler_anomalies(all_rows)
    diff()
    comparer_reference(all_rows)
    ecrire_feed()
    ecrire_resumes()
    return 0


# Seuils du garde-fou de publication : au-delà, on suspecte un parser qui
# déraille (refonte de site) plutôt qu'un vrai mouvement de marché.
GARDE_FOU_PRIX = 0.40      # > 40 % des prix communs modifiés
GARDE_FOU_DISPARUS = 0.30  # > 30 % des offres disparues (périmètre couvert)
GARDE_FOU_VOLUME = 0.50    # relevé < 50 % du volume de référence


def garde_fou(rows, month, master=None):
    """Anti-garbage : un parser qui déraille après une refonte produit des
    données PLAUSIBLES (les pages à 0 offre échouent déjà, pas les mauvaises
    extractions). On compare au dernier mois complet non-archive : divergence
    massive => publication bloquée (run --force pour outrepasser).
    Rend la liste des alertes (vide = publication autorisée)."""
    master = read_master() if master is None else master
    ref = None
    for m in sorted({r_["date_releve"][:7] for r_ in master
                     if r_["date_releve"][:7] != month}, reverse=True):
        sel = [r_ for r_ in master if r_["date_releve"][:7] == m]
        if sum(r_["fiabilite"] != "officiel_archive" for r_ in sel) >= len(sel) / 2:
            ref = m
            break
    if not ref:
        return []
    prev = [r_ for r_ in master if r_["date_releve"][:7] == ref]

    def cle(r_):
        return (r_["operateur"], r_["categorie"], r_["offre"])
    a = {cle(r_): r_ for r_ in prev if r_["prix_dh_mois"]}
    b = {cle(r_): r_ for r_ in rows if r_["prix_dh_mois"]}
    alertes = []
    commun = set(a) & set(b)
    if commun:
        chg = sum(a[k]["prix_dh_mois"] != b[k]["prix_dh_mois"] for k in commun)
        if chg / len(commun) > GARDE_FOU_PRIX:
            alertes.append(f"{chg}/{len(commun)} prix communs modifiés "
                           f"(seuil {GARDE_FOU_PRIX:.0%}) vs {ref}")
    scope_b = {(op, cat) for op, cat, _ in b}
    disparus = [k for k in a if k not in b and (k[0], k[1]) in scope_b]
    if a and len(disparus) / len(a) > GARDE_FOU_DISPARUS:
        alertes.append(f"{len(disparus)}/{len(a)} offres disparues "
                       f"(seuil {GARDE_FOU_DISPARUS:.0%}) vs {ref}")
    if b and len(b) < GARDE_FOU_VOLUME * len(a):
        alertes.append(f"volume du relevé effondré : {len(b)} offres "
                       f"vs {len(a)} en {ref} (seuil {GARDE_FOU_VOLUME:.0%})")
    return alertes


def fusion_mois(existants, nouveaux):
    """Union par offre au sein d'un mois : une nouvelle ligne remplace la
    même (opérateur, catégorie, offre) ; tout le reste du mois est conservé.
    Indispensable quand un mois d'archives est assemblé par plusieurs runs
    (pages différentes, --only, catalogues) — un run partiel ne doit JAMAIS
    effacer ce que les autres sources ont apporté."""
    cles = {(r_["operateur"], r_["categorie"], r_["offre"]) for r_ in nouveaux}
    return [x for x in existants
            if (x["operateur"], x["categorie"], x["offre"]) not in cles] \
        + nouveaux


def ecrire_releve(rows, month):
    """Snapshot du mois + base cumulée (un même mois relancé est remplacé)."""
    snap = DATA_DIR / f"releve_{month}.csv"
    write_csv(snap, rows, mode="w")
    master_rows = [r_ for r_ in read_master() if r_["date_releve"][:7] != month]
    write_csv(MASTER_CSV, master_rows + rows, mode="w")
    print(f"\n{len(rows)} lignes écrites -> {snap.name} + barometre.csv")
    return snap


# --------------------------------------------------------------------------
# Préflight réseau : distinguer « site refondu » de « source injoignable »
# --------------------------------------------------------------------------


def check(only=None):
    hotes, ordre = {}, []
    for page in pages_selectionnees(only):
        h = urlsplit(page["url"]).netloc
        if h not in hotes:
            hotes[h] = page["url"]
            ordre.append(h)

    print(f"Joignabilité des {len(ordre)} hôtes du registre :\n")
    ko = []
    for h in ordre:
        print(f"  {h:<26}", end=" ", flush=True)
        try:
            r = requests.get(hotes[h], headers=HTTP_HEADERS, timeout=15,
                             allow_redirects=True)
            etat = "OK " if r.ok else "HTTP"
            print(f"{etat} {r.status_code}  {len(r.content) // 1024} Ko")
            if not r.ok:
                ko.append((h, f"HTTP {r.status_code}"))
        except Exception as exc:
            print(f"INJOIGNABLE ({exc.__class__.__name__})")
            ko.append((h, exc.__class__.__name__))

    if ko:
        print("\nSources injoignables — un `run` ne produira rien pour elles :")
        for h, motif in ko:
            print(f"  - {h}: {motif}")
        print("\nCauses habituelles : pas de sortie réseau vers les domaines .ma "
              "(proxy/pare-feu d'entreprise), ou blocage géographique.\n"
              "Tant que ce n'est pas résolu, itérer hors ligne : "
              "python barometre.py replay")
    else:
        print("\nToutes les sources répondent — `run` peut être lancé.")
    return 1 if ko else 0


# --------------------------------------------------------------------------
# Replay : re-parser les dumps data/raw sans réseau (mise au point des parsers)
# --------------------------------------------------------------------------


def mois_disponibles():
    return sorted(p.name for p in RAW_DIR.glob("*")
                  if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}", p.name))


def replay(month=None, only=None, write=False):
    mois = mois_disponibles()
    if not mois:
        print(f"Aucun dump dans {RAW_DIR} — lancer d'abord `run` "
              "(ou y déposer les pages enregistrées à la main).")
        return 1
    month = month or mois[-1]
    raw_month_dir = RAW_DIR / month
    if not raw_month_dir.is_dir():
        print(f"Pas de dump pour {month}. Disponibles : {', '.join(mois)}")
        return 1

    print(f"Replay des dumps de {month} (hors ligne) :\n")
    all_rows, manquants = [], []
    for page in pages_selectionnees(only):
        dump = raw_month_dir / f"{slugify(page['label'])}.txt"
        if not dump.exists():
            manquants.append(page["label"])
            continue
        print(f"  [{page['method']:>8}] {page['label']} …", end=" ", flush=True)
        try:
            rows = page["parse"](dump.read_text(encoding="utf-8"), page["url"])
            all_rows.extend(rows)
            print(f"{len(rows)} offre(s)")
        except Exception as exc:
            print(f"ECHEC ({exc.__class__.__name__}: {exc})")

    if manquants:
        print(f"\nDumps absents : {', '.join(manquants)}")
    if not all_rows:
        print("\nAucune offre extraite des dumps.")
        return 1

    signaler_anomalies(all_rows)
    if write:
        # Les parsers stampent la date du jour : pour un mois passé, cela
        # enverrait les lignes dans le mauvais mois du master. On reprend la
        # date du relevé existant (ou le 1er du mois du dump à défaut).
        snap = DATA_DIR / f"releve_{month}.csv"
        date_ref = today() if month == today()[:7] else f"{month}-01"
        if snap.exists():
            with open(snap, newline="", encoding="utf-8-sig") as fh:
                dates = [r_["date_releve"] for r_ in
                         csv.DictReader(fh, delimiter=";") if r_.get("date_releve")]
            if dates:
                date_ref = max(set(dates), key=dates.count)
        for r_ in all_rows:
            r_["date_releve"] = date_ref
        ecrire_releve(all_rows, month)
        comparer_reference(all_rows)
    else:
        print(f"\n{len(all_rows)} offre(s) extraites — rien n'est écrit "
              "(ajouter --write pour enregistrer le relevé).")
        apercu(all_rows)
    return 0


def apercu(rows, limite=40):
    print()
    for r_ in rows[:limite]:
        print(f"  {r_['operateur']:<14} {r_['categorie']:<14} "
              f"{r_['offre'][:38]:<38} {r_['debit_ou_data'][:16]:<16} "
              f"{r_['prix_dh_mois']:>5} DH  [{r_['fiabilite']}]")
    if len(rows) > limite:
        print(f"  … et {len(rows) - limite} autre(s)")


# --------------------------------------------------------------------------
# Contrôles qualité sur un relevé
# --------------------------------------------------------------------------


def signaler_anomalies(rows):
    """Garde-fous : schéma respecté, prix plausibles, doublons, lignes à finir."""
    alertes = []
    vus = {}
    for r_ in rows:
        cle = (r_["operateur"], r_["categorie"], r_["offre"])
        vus.setdefault(cle, []).append(r_)
        if r_["fiabilite"] not in FIABILITES:
            alertes.append(f"fiabilite inconnue « {r_['fiabilite']} » "
                           f"sur {' / '.join(cle)}")
        prix = r_["prix_dh_mois"]
        if prix:
            if not prix.isdigit():
                alertes.append(f"prix non numérique « {prix} » sur {' / '.join(cle)}")
            elif not (PRIX_MIN <= int(prix) <= PRIX_MAX):
                alertes.append(f"prix hors fourchette ({prix} DH) sur {' / '.join(cle)}")
    for cle, doublons in vus.items():
        if len(doublons) > 1:
            alertes.append(f"{len(doublons)} lignes identiques pour {' / '.join(cle)}")

    a_relire = [r_ for r_ in rows if r_["fiabilite"] != "officiel_site"]
    print(f"\nQualité : {len(rows)} ligne(s), "
          f"{len(rows) - len(a_relire)} en officiel_site, "
          f"{len(a_relire)} à relire ({', '.join(sorted({r_['fiabilite'] for r_ in a_relire})) or '-'}).")
    if alertes:
        print("Anomalies :")
        for a in alertes[:20]:
            print(f"  ! {a}")
        if len(alertes) > 20:
            print(f"  … et {len(alertes) - 20} autre(s)")
    return alertes


# --------------------------------------------------------------------------
# Comparaison au relevé manuel de référence (validation du premier run)
# --------------------------------------------------------------------------


def norm_debit(txt):
    """« 100 Mb/s », « 100 Méga », « 1 Gb/s » -> 100 / 100 / 1000 (en Mb/s).
    Pour la data mobile, « 25 Go » -> 25Go. Rend une clé comparable."""
    t = norm(txt or "").strip()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(Gb/s|Gbps|Giga|Mb/s|Mbps|M[ée]ga|Go|Mo)", t, re.I)
    if not m:
        return ""
    val, unite = float(m.group(1).replace(",", ".")), m.group(2).lower()
    if unite.startswith(("gb", "gi")):
        return f"{int(val * 1000)}Mb"
    if unite.startswith(("mb", "mé", "me")):
        return f"{int(val)}Mb"
    if unite == "go":
        return f"{int(val)}Go"
    return f"{int(val)}Mo"


def lire_reference():
    if not REFERENCE_CSV.exists():
        return []
    with open(REFERENCE_CSV, newline="", encoding="utf-8-sig") as fh:
        return [r_ for r_ in csv.DictReader(fh, delimiter=";") if r_.get("operateur")]


def comparer_reference(rows=None):
    """Confronte un relevé au relevé manuel d'août 2026 (backlog n°2).
    Clé de rapprochement : opérateur + catégorie + débit/data normalisé."""
    ref = lire_reference()
    if not ref:
        print(f"\n(compare) Pas de relevé de référence dans {REFERENCE_CSV.name}.")
        return
    if rows is None:
        rows = derniers_releves()
        if not rows:
            print("\n(compare) Aucun relevé en base — lancer `run` d'abord.")
            return

    def index(src):
        idx = {}
        for r_ in src:
            d = norm_debit(r_["debit_ou_data"])
            if not d or not r_["prix_dh_mois"].strip().isdigit():
                continue
            idx.setdefault((r_["operateur"], r_["categorie"], d), set()).add(
                int(r_["prix_dh_mois"]))
        return idx

    a, b = index(ref), index(rows)
    concordent, ecarts, absents, nouveaux = [], [], [], []
    for cle in sorted(set(a) | set(b)):
        libelle = " / ".join(cle)
        if cle in a and cle in b:
            (concordent if a[cle] & b[cle] else ecarts).append(
                (libelle, sorted(a[cle]), sorted(b[cle])))
        elif cle in a:
            absents.append((libelle, sorted(a[cle])))
        else:
            nouveaux.append((libelle, sorted(b[cle])))

    print(f"\n=== Confrontation au relevé de référence ({REFERENCE_CSV.name}) ===")
    print(f"  {len(concordent)} concordance(s), {len(ecarts)} écart(s) de prix, "
          f"{len(absents)} offre(s) de la référence non retrouvée(s), "
          f"{len(nouveaux)} nouveauté(s).")
    for libelle, pa, pb in ecarts:
        print(f"  ~ ECART    {libelle} : reference {pa} DH -> releve {pb} DH")
    for libelle, pa in absents:
        print(f"  - ABSENT   {libelle} (reference {pa} DH)")
    for libelle, pb in nouveaux:
        print(f"  + NOUVEAU  {libelle} : {pb} DH")
    if ecarts or absents:
        print("\n  Rappel : la référence contient des valeurs de presse "
              "(fiabilite presse_*) — en cas d'écart, c'est le site officiel\n"
              "  qui fait foi ; la référence sert seulement d'alerte de contrôle.")


def derniers_releves():
    rows = read_master()
    if not rows:
        return []
    dernier = max(r_["date_releve"][:7] for r_ in rows)
    return [r_ for r_ in rows if r_["date_releve"][:7] == dernier]


def write_csv(path, rows, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def read_master():
    if not MASTER_CSV.exists():
        return []
    with open(MASTER_CSV, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh, delimiter=";"))

# --------------------------------------------------------------------------
# Diff entre les deux derniers relevés — le cœur du « baromètre »
# --------------------------------------------------------------------------


def _mesure(r_):
    """Volume comparable d'une offre : (25, "Go") / (100, "Mb") / None."""
    m = re.match(r"(\d+)(Go|Mb|Mo)", norm_debit(r_["debit_ou_data"]))
    return (int(m.group(1)), m.group(2)) if m else None


def diff_changes(rows, prev, curr):
    """Changements structurés entre deux mois du master : liste de dicts
    {type: nouveau|prix|retire|contenu|renomme, cle: (op, cat, offre), …}.

    Les mois backfillés depuis les archives ont une couverture PARTIELLE
    (toutes les pages ne sont pas capturées chaque mois) : un « nouveau »
    ou un « retiré » n'a de sens que si le périmètre (opérateur, catégorie)
    était observé dans les DEUX mois — sinon c'est un trou de couverture,
    pas un mouvement de grille.

    « contenu » = shrinkflation (ou l'inverse) : prix identique mais volume
    de data/débit modifié. Détecté sur les offres au même nom, ET par
    appariement nouveau×retiré au même (opérateur, catégorie, prix) —
    l'opérateur qui rebaptise « Forfait 30Go » en « Forfait 25Go » au même
    prix passait pour un retrait + une nouveauté. Un appariement à volume
    identique est un simple renommage (« renomme »)."""
    def index(month):
        return {(r_["operateur"], r_["categorie"], r_["offre"]): r_
                for r_ in rows if r_["date_releve"][:7] == month
                and r_["prix_dh_mois"]}

    a, b = index(prev), index(curr)
    scope_a = {(op, cat) for op, cat, _ in a}
    scope_b = {(op, cat) for op, cat, _ in b}
    changes, nouveaux, retires = [], [], []
    for key in sorted(b):
        if key not in a:
            if (key[0], key[1]) in scope_a:
                nouveaux.append(key)
        elif a[key]["prix_dh_mois"] != b[key]["prix_dh_mois"]:
            changes.append(dict(type="prix", cle=key,
                                avant=a[key]["prix_dh_mois"],
                                prix=b[key]["prix_dh_mois"]))
        else:
            ma, mb_ = _mesure(a[key]), _mesure(b[key])
            if ma and mb_ and ma != mb_:
                changes.append(dict(type="contenu", cle=key,
                                    prix=b[key]["prix_dh_mois"],
                                    avant_mesure=ma, mesure=mb_))
    for key in sorted(set(a) - set(b)):
        if (key[0], key[1]) in scope_b:
            retires.append(key)

    # appariement nouveau×retiré : même opérateur, catégorie et prix
    apparies = set()
    for kn in list(nouveaux):
        cands = [kr for kr in retires if kr not in apparies
                 and (kr[0], kr[1]) == (kn[0], kn[1])
                 and a[kr]["prix_dh_mois"] == b[kn]["prix_dh_mois"]]
        if not cands:
            continue
        kr = cands[0]
        ma, mb_ = _mesure(a[kr]), _mesure(b[kn])
        if ma and mb_ and ma != mb_:
            changes.append(dict(type="contenu", cle=kn, avant_offre=kr[2],
                                prix=b[kn]["prix_dh_mois"],
                                avant_mesure=ma, mesure=mb_))
        elif ma and mb_ and ma == mb_:
            changes.append(dict(type="renomme", cle=kn, avant_offre=kr[2],
                                prix=b[kn]["prix_dh_mois"]))
        else:
            continue        # volumes incomparables : rester nouveau + retiré
        nouveaux.remove(kn)
        apparies.add(kr)

    changes.extend(dict(type="nouveau", cle=k, prix=b[k]["prix_dh_mois"])
                   for k in nouveaux)
    changes.extend(dict(type="retire", cle=k, prix=a[k]["prix_dh_mois"])
                   for k in retires if k not in apparies)
    return changes


def diff():
    rows = read_master()
    months = sorted({r_["date_releve"][:7] for r_ in rows})
    if len(months) < 2:
        print("\n(diff) Un seul relevé en base — comparaison possible dès le mois prochain.")
        return
    prev, curr = months[-2], months[-1]
    changes = diff_changes(rows, prev, curr)

    def ligne(c):
        libelle = " / ".join(c["cle"])
        if c["type"] == "nouveau":
            return f"  + NOUVEAU  {libelle} : {c['prix']} DH"
        if c["type"] == "prix":
            return f"  ~ PRIX     {libelle} : {c['avant']} -> {c['prix']} DH"
        if c["type"] == "contenu":
            return (f"  ! CONTENU  {libelle} : {_fmt_mesure(c['avant_mesure'])} "
                    f"-> {_fmt_mesure(c['mesure'])} au même prix ({c['prix']} DH)")
        if c["type"] == "renomme":
            return f"  = RENOMME  {c['avant_offre']} -> {libelle} ({c['prix']} DH)"
        return f"  - RETIRE   {libelle} (était {c['prix']} DH)"

    print(f"\n=== Baromètre {prev} -> {curr} ===")
    print("\n".join(ligne(c) for c in changes) if changes
          else "  Aucun changement de grille.")

# --------------------------------------------------------------------------
# Publication : flux RSS des changements + résumé éditorial mensuel
# --------------------------------------------------------------------------

FEED_XML = DATA_DIR / "changements.xml"
RESUMES_JSON = DATA_DIR / "resumes.json"
MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]


def mois_label(month):
    y, m = month.split("-")
    return f"{MOIS_FR[int(m) - 1]} {y}"


def _fmt_mesure(m):
    return f"{m[0]} {'Go' if m[1] == 'Go' else 'Mb/s' if m[1] == 'Mb' else 'Mo'}"


def _libelle_change(c):
    """Phrase française d'un changement structuré."""
    op, _cat, offre = c["cle"]
    if c["type"] == "nouveau":
        return f"{op} lance « {offre} » à {c['prix']} DH/mois"
    if c["type"] == "prix":
        sens = "baisse" if int(c["prix"]) < int(c["avant"]) else "passe"
        return f"« {offre} » ({op}) {sens} de {c['avant']} à {c['prix']} DH/mois"
    if c["type"] == "contenu":
        av, ap = c["avant_mesure"], c["mesure"]
        sens = "réduit" if ap[0] < av[0] else "augmente"
        return (f"{op} {sens} le contenu de « {offre} » à prix constant "
                f"({c['prix']} DH/mois) : {_fmt_mesure(av)} -> {_fmt_mesure(ap)}")
    if c["type"] == "renomme":
        return (f"{op} renomme « {c['avant_offre']} » en « {offre} » "
                f"({c['prix']} DH/mois, contenu identique)")
    return f"{op} retire « {offre} » (était {c['prix']} DH/mois)"


def ecrire_feed():
    """Flux RSS des changements de grille, un item par mois comparé.
    Publié sur GitHub Pages : {SITE_URL}data/changements.xml"""
    import email.utils
    from html import escape

    rows = read_master()
    months = sorted({r_["date_releve"][:7] for r_ in rows})
    items = []
    for i in range(len(months) - 1, 0, -1):          # récent d'abord
        prev, curr = months[i - 1], months[i]
        changes = diff_changes(rows, prev, curr)
        date_releve = max(r_["date_releve"] for r_ in rows
                          if r_["date_releve"][:7] == curr)
        pub = email.utils.format_datetime(dt.datetime.fromisoformat(
            date_releve + "T08:00:00+00:00"))
        titre = (f"Baromètre {mois_label(curr)} : "
                 + (f"{len(changes)} changement(s) de grille" if changes
                    else "grilles stables"))
        corps = ("<ul>" + "".join(f"<li>{escape(_libelle_change(c))}</li>"
                                  for c in changes) + "</ul>"
                 if changes else
                 "<p>Aucun changement de grille chez IAM, Orange et inwi.</p>")
        items.append(
            f"<item><title>{escape(titre)}</title>"
            f"<link>{SITE_URL}?mois={curr}</link>"
            f"<guid isPermaLink=\"false\">barometre-{curr}</guid>"
            f"<pubDate>{pub}</pubDate>"
            f"<description>{escape(corps)}</description></item>")
        if len(items) >= 24:
            break

    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<rss version=\"2.0\"><channel>"
           "<title>Baromètre Télécoms Maroc — changements de grille</title>"
           f"<link>{SITE_URL}</link>"
           "<description>Nouveautés, retraits et changements de prix "
           "détectés chaque mois chez Maroc Telecom, Orange et inwi "
           "(sites officiels).</description>"
           "<language>fr</language>"
           + "".join(items) + "</channel></rss>\n")
    FEED_XML.write_text(xml, encoding="utf-8")
    print(f"Flux RSS écrit -> {FEED_XML.name} ({len(items)} item(s))")


def texte_resume(rows, month, months):
    """Paragraphe éditorial d'un mois : volumétrie, prix d'entrée fibre et
    mobile, changements vs mois précédent. Texte 100 % dérivé des données."""
    sel = [r_ for r_ in rows if r_["date_releve"][:7] == month]
    ordre = ["Maroc Telecom", "Orange", "inwi"]
    ops = [o for o in ordre if any(r_["operateur"] == o for r_ in sel)]
    liste_ops = (" et ".join([", ".join(ops[:-1]), ops[-1]])
                 if len(ops) > 1 else ops[0] if ops else "")
    if sel and all(r_["fiabilite"] in ("officiel_archive", "officiel_catalogue")
                   for r_ in sel):
        src = ("catalogues officiels archivés"
               if any(r_["fiabilite"] == "officiel_catalogue" for r_ in sel)
               else "archives web des sites officiels")
        phrases = [f"En {mois_label(month)}, relevé rétrospectif reconstruit "
                   f"depuis les {src} "
                   f"({liste_ops}) : {len(sel)} offres — couverture partielle."]
    else:
        phrases = [f"En {mois_label(month)}, le baromètre a relevé "
                   f"{len(sel)} offres sur les sites officiels de "
                   f"{liste_ops}."]

    def entree(cat, unite):
        avec_prix = [r_ for r_ in sel if r_["categorie"] == cat
                     and r_["prix_dh_mois"].isdigit()]
        if not avec_prix:
            return None
        mini = min(int(r_["prix_dh_mois"]) for r_ in avec_prix)
        ops = sorted({r_["operateur"] for r_ in avec_prix
                      if int(r_["prix_dh_mois"]) == mini})
        return f"{mini} DH/mois ({', '.join(ops)}{unite})"

    fibre = entree("Fibre", "")
    mobile = entree("Forfait mobile", "")
    if fibre:
        phrases.append(f"L'entrée fibre est à {fibre}.")
    if mobile:
        phrases.append(f"Le premier forfait mobile démarre à {mobile}.")

    idx = months.index(month)
    if idx > 0:
        prev = months[idx - 1]
        changes = diff_changes(rows, prev, month)
        if not changes:
            phrases.append(f"Aucun changement de grille par rapport à "
                           f"{mois_label(prev)}.")
        else:
            exemples = "; ".join(_libelle_change(c) for c in changes[:3])
            suite = f" — et {len(changes) - 3} autre(s)" if len(changes) > 3 else ""
            phrases.append(f"Par rapport à {mois_label(prev)} : "
                           f"{exemples}{suite}.")
    return " ".join(phrases)


def ecrire_resumes():
    """data/resumes.json : un paragraphe par mois, affiché par le dashboard
    (section « L'analyse du mois ») et baké dans la page pour le SEO."""
    import json
    rows = read_master()
    months = sorted({r_["date_releve"][:7] for r_ in rows})
    resumes = {m: texte_resume(rows, m, months) for m in months}
    RESUMES_JSON.write_text(
        json.dumps({"genere_le": today(), "resumes": resumes},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"Résumés écrits -> {RESUMES_JSON.name} ({len(resumes)} mois)")

# --------------------------------------------------------------------------
# Backfill : reconstruire l'historique depuis les captures web.archive.org
# --------------------------------------------------------------------------

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
# Parsers d'époque : quand le parser courant rend 0 offre sur une capture
# d'archive (« format ancien »), ces variantes sont essayées dans l'ordre.
# Clé = label de la page dans PAGES.
ERA_PARSERS = {
    "IAM forfaits mobile": [parse_iam_forfaits_liberte],
}

# Pages dont le HTML archivé est rendu serveur : parsables sans navigateur.
# (boutique Orange, yoxo, forfaits inwi : rendu client — les captures
# n'exécutent pas le JS applicatif de façon fiable, on ne backfill pas.)
# Les pages boutique Orange (Next.js) sont rendues côté serveur (SSR) :
# leurs captures d'archives contiennent cartes ET liens — nos parsers
# (cartes + slugs) marchent tels quels, à condition d'extraire les hrefs
# que html_to_text jette (la section LIENS, comme le fait le navigateur).
BACKFILL_LABELS = {
    "IAM fibre", "IAM forfaits mobile", "IAM Box El Manzil 5G", "IAM Box 4G+",
    "Orange fibre (grille pro, HTML)", "inwi fibre",
    "Orange forfaits (boutique)", "Orange Dar Box 5G", "Orange Dar Box 4G+",
    "Yoxo (digital)",
}


def _texte_archive(raw_html, avec_liens):
    """Texte d'une capture + section LIENS si la page l'exige (slugs)."""
    text = html_to_text(raw_html)
    if avec_liens:
        soup = BeautifulSoup(raw_html, "html.parser")
        hrefs = [a.get("href", "") for a in soup.find_all("a")]
        text += "\nLIENS\n" + "\n".join(dict.fromkeys(h for h in hrefs if h))
    return text


def _wayback_get(url, params=None, essais=6):
    """GET patient vers web.archive.org : l'API CDX rend des 503 en rafale
    dès la deuxième requête rapprochée (rate-limit ~1 req/30 s constaté
    09/2026). Backoff long et progressif — un backfill est un one-off."""
    for essai in range(1, essais + 1):
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS,
                             timeout=90)
            if r.status_code in (429, 503):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except Exception:
            if essai == essais:
                raise
            attente = 20 * essai
            print(f"(attente {attente}s)", end=" ", flush=True)
            time.sleep(attente)


def wayback_mois(url, m_from, m_to):
    """Premier snapshot HTTP 200 de chaque mois (API CDX, collapse mensuel).
    Rend {"YYYYMM": "timestamp complet"}."""
    params = {"url": url, "output": "json", "from": m_from.replace("-", ""),
              "to": m_to.replace("-", "") + "31", "filter": "statuscode:200",
              "collapse": "timestamp:6", "fl": "timestamp", "limit": "200"}
    data = _wayback_get(WAYBACK_CDX, params).json()
    return {ts[0][:6]: ts[0] for ts in data[1:]} if data else {}


def backfill(m_from, m_to=None, only=None, write=False, delai=1.5,
             refetch=False):
    """Relevés rétrospectifs : pour chaque mois absent du master, re-parser
    les captures web.archive.org des pages rendues serveur. Les lignes
    sortent en fiabilite=officiel_archive avec la capture exacte en source.

    --refetch : re-traite aussi les mois d'archives déjà en base (nouveaux
    parsers d'époque, nouvelles captures). Garde-fou : un mois contenant la
    moindre ligne NON-archive (relevé live) n'est JAMAIS retouché.
    Chaque capture est sauvegardée dans data/raw_archive/YYYY-MM/ — la
    boucle de mise au point des parsers d'époque travaille hors ligne."""
    m_to = m_to or today()[:7]
    master = read_master()
    par_mois_master = {}
    for r_ in master:
        par_mois_master.setdefault(r_["date_releve"][:7], []).append(r_)
    mois_proteges = {m for m, rs in par_mois_master.items()
                     if any(x["fiabilite"] != "officiel_archive" for x in rs)}
    mois_existants = set(par_mois_master)
    pages = [p for p in PAGES if p["label"] in BACKFILL_LABELS
             and (not only or p["op"] in only)]

    print(f"Backfill {m_from} -> {m_to} sur {len(pages)} page(s) archivée(s) :\n")
    par_mois = {}
    for page in pages:
        print(f"  CDX {page['label']} …", end=" ", flush=True)
        try:
            snaps = wayback_mois(page["url"], m_from, m_to)
            print(f"{len(snaps)} mois capturés")
        except Exception as exc:
            print(f"ECHEC ({exc.__class__.__name__}: {exc})")
            continue
        time.sleep(delai)
        # Les dumps déjà en cache comptent même si le CDX vient d'échouer :
        # une fois la capture téléchargée, la boucle est 100 % hors ligne.
        slug = slugify(page["label"])
        for cache in RAW_ARCHIVE_DIR.glob(f"*/{slug}@*.txt"):
            ym_c = cache.parent.name.replace("-", "")
            ts_c = cache.stem.split("@", 1)[1]
            if m_from.replace("-", "") <= ym_c[:6] <= m_to.replace("-", ""):
                snaps.setdefault(ym_c[:6], ts_c)
        for ym, ts in sorted(snaps.items()):
            month = f"{ym[:4]}-{ym[4:6]}"
            if month in mois_proteges:
                continue                      # mois avec données live : intouchable
            if month in mois_existants and not refetch:
                continue
            url_arch = f"https://web.archive.org/web/{ts}id_/{page['url']}"
            dump = RAW_ARCHIVE_DIR / month / f"{slug}@{ts}.txt"
            try:
                if dump.exists():             # déjà téléchargé : hors ligne
                    text = dump.read_text(encoding="utf-8")
                else:
                    text = _texte_archive(_wayback_get(url_arch).text,
                                          page.get("links", False))
                    dump.parent.mkdir(parents=True, exist_ok=True)
                    dump.write_text(text, encoding="utf-8")
                    time.sleep(delai)
                rs = page["parse"](text, page["url"])
                for era in ERA_PARSERS.get(page["label"], []):
                    if rs:
                        break
                    rs = era(text, page["url"])
            except Exception as exc:
                print(f"    {month}  {page['label']}: ECHEC "
                      f"({exc.__class__.__name__})")
                time.sleep(delai)
                continue
            date_iso = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
            for r_ in rs:
                r_["date_releve"] = date_iso
                if r_["fiabilite"] == "officiel_site":
                    r_["fiabilite"] = "officiel_archive"
                r_["remarques"] = ((r_["remarques"] + " - ") if r_["remarques"]
                                   else "") + "retrospectif web.archive.org"
                r_["source"] = url_arch
            etat = f"{len(rs)} offre(s)" if rs else "0 offre (format ancien ?)"
            print(f"    {month}  {page['label']}: {etat}")
            if rs:
                par_mois.setdefault(month, []).extend(rs)

    if not par_mois:
        # Résultat normal du cron de rattrapage mensuel : rien de neuf
        # dans les archives n'est un succès, pas une erreur.
        print("\nAucun mois reconstitué (tout est déjà en base, ou aucune "
              "capture parsable).")
        return 0

    for month in sorted(par_mois):
        rs = fusion_mois(par_mois_master.get(month, []), par_mois[month])
        print(f"\n=== {month} : {len(par_mois[month])} offre(s) "
              f"reconstituées, {len(rs)} au total après fusion ===")
        apercu(rs, 12)
        signaler_anomalies(rs)
        if write:
            ecrire_releve(rs, month)
    if write:
        ecrire_feed()
        ecrire_resumes()
    else:
        print("\nRien n'est écrit — ajouter --write pour enregistrer ces mois.")
    return 0

# --------------------------------------------------------------------------
# Catalogues : ingestion des grilles extraites des catalogues officiels
# archivés (PDF/pages datés). Extraction MANUELLE et auditée — un PDF de
# 19 pages ne se regexe pas fiablement ; chaque ligne de
# data/catalogues_extraits.csv référence sa capture d'archive en source.
# --------------------------------------------------------------------------

CATALOGUES_CSV = DATA_DIR / "catalogues_extraits.csv"


def catalogues(write=False):
    """Fusionne les extraits de catalogues officiels dans le master.
    Protection : un mois contenant des lignes live (ni archive ni catalogue)
    n'est jamais touché. À l'intérieur d'un mois, seuls les périmètres
    (opérateur, catégorie) présents dans les extraits sont remplacés."""
    if not CATALOGUES_CSV.exists():
        print(f"Pas d'extraits ({CATALOGUES_CSV.name}).")
        return 1
    with open(CATALOGUES_CSV, newline="", encoding="utf-8-sig") as fh:
        extraits = [r_ for r_ in csv.DictReader(fh, delimiter=";")
                    if r_.get("operateur")]
    master = read_master()
    par_mois_master = {}
    for r_ in master:
        par_mois_master.setdefault(r_["date_releve"][:7], []).append(r_)

    par_mois = {}
    for r_ in extraits:
        par_mois.setdefault(r_["date_releve"][:7], []).append(r_)
    ecrits = 0
    for month in sorted(par_mois):
        rs = par_mois[month]
        existants = par_mois_master.get(month, [])
        if any(x["fiabilite"] not in ("officiel_archive", "officiel_catalogue")
               for x in existants):
            print(f"  {month} : mois avec données live — extraits ignorés.")
            continue
        final = fusion_mois(existants, rs)
        print(f"  {month} : {len(rs)} ligne(s) de catalogue, "
              f"{len(final)} au total après fusion")
        signaler_anomalies(final)
        if write:
            ecrire_releve(final, month)
            ecrits += 1
    if write and ecrits:
        ecrire_feed()
        ecrire_resumes()
    elif not write:
        print("\nRien n'est écrit — ajouter --write pour enregistrer.")
    return 0

# --------------------------------------------------------------------------
# Discover : cartographier les URLs historiques des opérateurs dans la
# Wayback Machine — AVANT d'écrire des parsers d'époque, savoir où vit
# l'histoire (les URLs actuelles n'existent souvent que depuis fin 2025).
# --------------------------------------------------------------------------

DISCOVER_DOMAINS = ["iam.ma", "inwi.ma", "orange.ma", "yoxo.ma"]
DISCOVER_KEYWORDS = ("fibre", "forfait", "mobile", "box", "adsl", "tarif",
                     "catalogue", "offre", "internet", "dar-box", "jawal")
DISCOVER_EXCLU = ("actualite", "presse", "communique", "recrutement", "blog",
                  "faq", "aide", "contact", "mentions", "apropos", "a-propos",
                  ".jpg", ".png", ".css", ".js", ".svg", ".gif", ".woff",
                  "facebook", "twitter", "?", "assistance", "corporate")
INVENTORY_JSON = DATA_DIR / "wayback_inventory.json"


def _cdx_urls(domain, m_from, m_to, mimetype):
    """URLs uniques archivées (HTTP 200) d'un domaine et ses sous-domaines."""
    params = {"url": domain, "matchType": "domain", "output": "json",
              "fl": "original", "collapse": "urlkey",
              "filter": ["statuscode:200", f"mimetype:{mimetype}"],
              "from": m_from.replace("-", ""), "to": m_to.replace("-", "") + "31",
              "limit": "30000"}
    data = _wayback_get(WAYBACK_CDX, params).json()
    return [row[0] for row in data[1:]] if data else []


def _score_url(u):
    """Priorise les pages tarifaires probables : mots-clés forts, chemin court."""
    lo = u.lower()
    score = sum(3 for k in ("fibre", "forfait", "dar-box", "catalogue") if k in lo)
    score += sum(1 for k in DISCOVER_KEYWORDS if k in lo)
    score -= lo.count("/")                 # les pages profondes sont du détail
    return score


def discover(m_from="2022-01", m_to=None, par_domaine=12, delai=2.0):
    """Inventaire Wayback : URLs candidates par domaine + couverture mensuelle
    des meilleures. Écrit data/wayback_inventory.json et affiche le rapport.
    Lent (rate-limit CDX) — à lancer en tâche de fond."""
    import json
    m_to = m_to or today()[:7]
    inv = {"genere_le": today(), "de": m_from, "a": m_to, "domaines": {}}
    for dom in DISCOVER_DOMAINS:
        entry = {"candidats": {}, "pdf": {}}
        for mime, cible in (("text/html", "candidats"),
                            ("application/pdf", "pdf")):
            print(f"\n=== {dom} ({mime}) ===", flush=True)
            try:
                urls = _cdx_urls(dom, m_from, m_to, mime)
            except Exception as exc:
                print(f"  CDX ECHEC ({exc.__class__.__name__}: {exc})")
                continue
            time.sleep(delai)
            lo_ok = [u for u in urls
                     if any(k in u.lower() for k in DISCOVER_KEYWORDS)
                     and not any(x in u.lower() for x in DISCOVER_EXCLU)]
            print(f"  {len(urls)} URLs archivées, {len(lo_ok)} candidates "
                  "après filtrage")
            lo_ok.sort(key=_score_url, reverse=True)
            quota = par_domaine if mime == "text/html" else 6
            for u in lo_ok[:quota]:
                try:
                    mois = wayback_mois(u, m_from, m_to)
                except Exception as exc:
                    print(f"  ? {u} — couverture inconnue "
                          f"({exc.__class__.__name__})")
                    continue
                time.sleep(delai)
                if not mois:
                    continue
                cover = sorted(mois)
                entry[cible][u] = {"mois": len(cover),
                                   "de": cover[0], "a": cover[-1],
                                   "timestamps": mois}
                print(f"  {len(cover):>3} mois  {cover[0][:6]}->{cover[-1][:6]}  {u}")
        inv["domaines"][dom] = entry
    INVENTORY_JSON.write_text(json.dumps(inv, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\nInventaire écrit -> {INVENTORY_JSON.name}")
    return 0

# --------------------------------------------------------------------------
# Mode test : parsers validés sur les extraits réels capturés le 16/08/2026
# --------------------------------------------------------------------------

SAMPLES = {
    "iam_fibre": """
        Fibre optique 800 DH/mois 500 Mb/s Illimités vers les fixes nationaux
        50 H vers mobiles dont 10 H vers l’international Zone 1
        Frais Installation: DH Acheter
        Fibre optique 400 DH/mois 100 Mb/s Illimités vers les fixes nationaux
        10 H vers mobile dont 2 H vers l’international Zone 1
        Frais Installation: DH Acheter
        Fibre optique 500 DH/mois 200 Mb/s Illimités vers les fixes nationaux
        20 H vers mobile Frais Installation: DH Acheter
        Fibre optique 1000 DH/mois 1 Gb/s Illimité vers les fixes et mobiles
        nationaux 20 H vers l’international Zone 1 Acheter
    """,
    "iam_forfaits": """
        Liberté Plus 4G+/5G 165 DH/mois 30 Go 180 min Pass et options Acheter
        Liberté Plus 4G+/5G 199 DH/mois 50 Go 90 min 1 Go Roaming* Acheter
        Liberté Plus 4G+/5G 479 DH/mois 100 Go 1200 min 4 Go Roaming* Acheter
    """,
    # Format run réel 08/2026 : libellé multi-lignes, espace avant /mois,
    # grille rendue deux fois (desktop + mobile) — le dedup doit jouer.
    "orange_pro_fibre": """
        20
         Méga
        249
        Dh
        /mois
         20
        Mbps symétrique
        Découvrir cette offre
        1000
         Méga
        949
        Dh
        /mois
        1000 Mbps
        Découvrir cette offre
        20
         Méga
        249
        Dh
        /mois
        20 Mbps
    """,
    # Anciens formats compacts — la regex doit rester rétrocompatible.
    "orange_pro_fibre_compact": """
        20 Méga · 249Dh/mois · 20 Mbps · illimités vers les numéros mobile
        Découvrir cette offre · 1000 Méga · 949Dh/mois · 1000 Mbps symétrique
        · 20H d'appels vers le mobile Découvrir cette offre
    """,
    # Run réel 08/2026 : texte éditorial + cartes (la même grille 2×) —
    # le dedup doit ramener chaque palier à une seule ligne.
    "inwi_fibre": """
        Forfait 20 Méga 249dh : l’offre la plus accessible du marché.
        Forfait 50 Méga 299dh : Parfait pour les foyers multi-utilisateurs.
        Forfait 200 Méga 449dh : Conçu pour les foyers ultra-connectés.
        Forfait 500 Méga 749dh : Streaming 4K, télétravail intensif.
        Forfait 1 Giga 949dh :
        Fibre optique 20Méga
        249
        Dhs/mois
        Fibre optique 50Méga
        299
        Dhs/mois
        Fibre optique 1Gbps
        949
        Dhs/mois
    """,
    # Extraits des dumps réels 08/2026 pour les parsers dédiés.
    "orange_boutique": """
        Forfait Yo Max 5G 99 Dh
        Forfait Yo 49 Dh
        Forfait YO 3h + 3Go 49 Dh
        3H d'appels
        3Go d'internet
        appels illimités vers Orange**
        ‎49,00
        DH/mois
        En savoir plus
        Choisir ce forfait
        Forfait YO 11Go+1H 49 Dh
         11Go d'internet
         1H d'appels
        WhatsApp illimité*
        ‎49,00
        DH/mois
        Choisir ce forfait
        LIENS
        https://boutique.orange.ma/choisir/forfait-yo-max-99dh-25go-1h-d-appel
        https://boutique.orange.ma/choisir/forfait-yo-max-52go-10h-d-appels-199dh
        https://boutique.orange.ma/choisir/forfait-yo-max-80go-2go-roaming-299dh
        https://boutique.orange.ma/choisir/forfait-yo-max-illimite-national-120go-5go-roaming-399dh
        https://boutique.orange.ma/choisir/forfait-yo-max-illimite-national-160go-8go-roaming-499dh
        https://boutique.orange.ma/choisir/forfait-yo-max-tout-illimite-10go-roaming-649dh-5-services
        https://boutique.orange.ma/choisir/forfait-yo-11go-1h-49-dh
        https://boutique.orange.ma/accessoires
    """,
    "yoxo_liens": """
        LIENS
        https://www.yoxo.ma/forfait-yoxo-50dh
        https://www.yoxo.ma/forfait-yoxo-100dh
        https://www.yoxo.ma/forfait-yoxo-200dhs
        https://www.yoxo.ma/forfait-yoxo-250dhs.html
    """,
    "orange_darbox_5g": """
        Dar Box 5G 299Dh
        Internet illimité
        50 Méga
        3H d’appels vers le mobile national et fixe international (zone 1)*
        ‎299,00
        DH/mois
        Tester mon éligibilité
        Frais de mise en service 299 Dh
        Box Wifi 5G 799 Dh 349 Dh
        Dar Box 5G 349Dh
        Internet illimité
        100 Méga
        4H d’appels
        ‎349,00
        DH/mois
        Frais de mise en service 349 Dh
    """,
    "yoxo": """
        SKHAWA DIAL SA7? KAYNA HNA!!
         20GO
        1H d'appels*
        SMS illimité*
        50
        DHS
        /mois
        ACHETER
         60GO
        5H d'appels*
        SMS illimité*
        100
        DHS
        /mois
    """,
    "inwi_forfaits": """
        Les forfaits Max Réseaux Sociaux 4G/5G
        Forfait FLEXI Réseaux Sociaux
        49
        Dhs/mois
        Réseaux sociaux illimités
        2Go d'internet
        1h d'appels vers le national
        JE CHOISIS MON FORFAIT
        Nouveau
        Forfait 18Go + 5H + WhatsApp Illimité
        99
        Dhs/mois
        18Go d'internet
        5h d'appels vers le national
        Whatsapp illimité
        JE CHOISIS MON FORFAIT
        Les forfaits Max Appels 4G/5G
        Forfait Appels illimités + 60Go
        249
        Dhs/mois
        Appels illimités vers les numéros inwi
        60Go d'internet
        2Go Roaming internet
        JE CHOISIS MON FORFAIT
    """,
    "generic_darbox": """
        Dar Box 4G+ Le WiFi illimité sans installation 100 Go
        199 Dh/mois pendant 3 mois Engagement 12 mois Je choisis
        Dar Box 5G 300 Go 299 DH /mois Je choisis
    """,
    # Extraits des dumps réels 09/2026 (data/raw/2026-09/iam-box-*.txt).
    "iam_box_5g": """
        Wifi
        Box El Manzil
        El Manzil 5G
        El Manzil 5G
        400 DH/mois
        100 Mb/s*
        Frais de mise à disposition de la Box 5G:
        Frais de mise en service:
        Acheter
        El Manzil 5G
        Qu’est-ce que l’offre Box 5G El Manzil ?
    """,
    "iam_box_4g": """
        Box 4G+ internet
        Forfaits internet
        Box 4G+
        199 DH/mois
        60 min
        60 Go
        Acheter
        Box 4G+
        350 DH/mois
        120 min
        90 Go
        Acheter
        Vous pouvez souscrire à l'un des pass internet mobile suivants en appelant le 600.
        20 DH
        2 Go
    """,
    # Page résidentielle Orange : les cartes tarifaires sont des <img> SVG.
    # Ordre volontairement mélangé pour vérifier le tri numérique des paliers.
    "orange_svg_page": """
        <img src="https://cdn-exemple.orange.ma/FibreOrange/fibre-cards/100go.svg">
        <img src="https://cdn-exemple.orange.ma/FibreOrange/fibre-cards/1000go.svg">
        <img src="https://cdn-exemple.orange.ma/FibreOrange/fibre-cards/20go.svg">
    """,
    # ANCIEN format iam.ma/forfaits-mobile (capture réelle du 06/12/2025) —
    # gamme « Forfait Liberté », bundles différents au même prix.
    "iam_forfaits_liberte": """
        Filtrer Budget DH - DH
        Forfait Liberté 59 DH/mois 11 Go 1 Heure Pass et options Acheter
        Forfait Liberté 59 DH/mois 3 Go 3 Heures 300 SMS Pass et options Acheter
        Forfait Liberté 99 DH/mois 20 Go 1 Heure Pass et options Acheter
        Forfait Liberté 99 DH/mois 11 Heures 2 Go Pass et options Acheter
        Forfait Liberté 99 DH/mois 13 Go 4 Heures Pass et options Acheter
        Forfait Liberté 119 DH/mois 22 Go 2 Heures Pass et options Acheter
        Forfait Liberté 59 DH/mois 11 Go 1 Heure Pass et options Acheter
    """,
    # Palier dont le SVG ne contient AUCUN prix en texte (tracés vectoriels) :
    # depuis 09/2026, la ligne est ignorée au lieu de sortir vide.
    "orange_svg_page_sans_prix": """
        <img src="https://cdn-exemple.orange.ma/FibreOrange/fibre-cards/50go.svg">
    """,
}

# Contenu factice des SVG, indexé par palier — utilisé par le test hors ligne.
SVG_SAMPLES = {
    "20": '<svg><text>20 Méga</text><text>249</text><text>Dh/mois</text></svg>',
    "100": '<svg><text>100 Méga</text><text>349</text><text>Dh/mois</text></svg>',
    "1000": '<svg><text>1000 Méga</text><text>949</text><text>Dh/mois</text></svg>',
    "50": '<svg><path d="M0 0 L10 10"/></svg>',   # prix en tracés, pas en texte
}


def fake_fetch_svg(url):
    """Faux téléchargeur de SVG (test hors ligne, aucune requête réseau)."""
    palier = re.search(r"/(\d+)go\.svg", url).group(1)
    return SVG_SAMPLES[palier]


def test():
    ok = True

    r = parse_iam_fibre(SAMPLES["iam_fibre"], "test")
    print(f"[iam_fibre]        {len(r)} offres :",
          [(x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    ok &= {(x["debit_ou_data"], x["prix_dh_mois"]) for x in r} == {
        ("500 Mb/s", "800"), ("100 Mb/s", "400"),
        ("200 Mb/s", "500"), ("1 Gb/s", "1000")}

    r = parse_iam_forfaits(SAMPLES["iam_forfaits"], "test")
    print(f"[iam_forfaits]     {len(r)} offres :",
          [(x['debit_ou_data'], x['appels_inclus'], x['prix_dh_mois']) for x in r])
    ok &= {(x["prix_dh_mois"], x["debit_ou_data"]) for x in r} == {
        ("165", "30 Go"), ("199", "50 Go"), ("479", "100 Go")}
    ok &= any("4 Go roaming" in x["remarques"] for x in r)

    for nom in ("orange_pro_fibre", "orange_pro_fibre_compact"):
        r = parse_orange_pro_fibre(SAMPLES[nom], "test")
        print(f"[{nom}] {len(r)} offres :",
              [(x['debit_ou_data'], x['prix_dh_mois']) for x in r])
        # 2 offres exactement : la grille dupliquée doit être dédoublonnée.
        ok &= sorted((x["debit_ou_data"], x["prix_dh_mois"]) for x in r) == [
            ("1000 Mb/s", "949"), ("20 Mb/s", "249")]

    r = parse_inwi_fibre(SAMPLES["inwi_fibre"], "test")
    print(f"[inwi_fibre]       {len(r)} offres :",
          [(x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    # Grille triplée dans la page : chaque palier ne sort qu'une fois.
    ok &= sorted((x["debit_ou_data"], x["prix_dh_mois"]) for x in r) == [
        ("1 Gb/s", "949"), ("20 Mb/s", "249"), ("200 Mb/s", "449"),
        ("50 Mb/s", "299"), ("500 Mb/s", "749")]

    r = parse_orange_boutique_forfaits(SAMPLES["orange_boutique"], "test")
    print(f"[orange_boutique]  {len(r)} offres :",
          [(x['offre'], x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    # Les entrées de menu (« Forfait Yo Max 5G 99 Dh » sans prix mensuel)
    # ne sortent pas ; le prix vient du « 49,00 DH/mois », jamais du « 00 ».
    # Les 6 Yo Max arrivent par les slugs, quel que soit l'ordre des jetons.
    ok &= {(x["offre"], x["debit_ou_data"], x["appels_inclus"],
            x["prix_dh_mois"]) for x in r if "slug" in x["remarques"]} == {
        ("Forfait Yo Max 99 Dh", "25 Go", "1h", "99"),
        ("Forfait Yo Max 199 Dh", "52 Go", "10h", "199"),
        ("Forfait Yo Max 299 Dh", "80 Go", "", "299"),
        ("Forfait Yo Max 399 Dh", "120 Go", "Illimite national", "399"),
        ("Forfait Yo Max 499 Dh", "160 Go", "Illimite national", "499"),
        ("Forfait Yo Max 649 Dh", "Tout illimite", "Tout illimite", "649")}
    ok &= len(r) == 8            # 2 cartes Yo + 6 slugs Yo Max, zéro doublon
    ok &= any("+2 Go roaming" in x["remarques"] for x in r)

    r = parse_yoxo(SAMPLES["yoxo"] + SAMPLES["yoxo_liens"], "test")
    print(f"[yoxo+slugs]       {len(r)} offres :",
          [(x['offre'], x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    # Les cartes donnent 50 et 100 (avec data) ; les slugs n'ajoutent que
    # les paliers absents (200, 250), jamais un doublon des cartes.
    ok &= {(x["offre"], x["debit_ou_data"]) for x in r} == {
        ("Yoxo 50 DH", "20 Go"), ("Yoxo 100 DH", "60 Go"),
        ("Yoxo 200 DH", ""), ("Yoxo 250 DH", "")}

    r = parse_orange_darbox(SAMPLES["orange_darbox_5g"], "test", "5G")
    print(f"[orange_darbox]    {len(r)} offres :",
          [(x['debit_ou_data'], x['prix_dh_mois'], x['remarques']) for x in r])
    ok &= sorted((x["debit_ou_data"], x["prix_dh_mois"]) for x in r) == [
        ("100 Méga", "349"), ("50 Méga", "299")]
    ok &= all("Frais de mise en service" in x["remarques"] for x in r)

    r = parse_yoxo(SAMPLES["yoxo"], "test")
    print(f"[yoxo]             {len(r)} offres :",
          [(x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    ok &= sorted((x["debit_ou_data"], x["prix_dh_mois"]) for x in r) == [
        ("20 Go", "50"), ("60 Go", "100")]

    r = parse_inwi_forfaits(SAMPLES["inwi_forfaits"], "test")
    print(f"[inwi_forfaits]    {len(r)} offres :",
          [(x['offre'], x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    ok &= sorted((x["offre"], x["debit_ou_data"], x["prix_dh_mois"]) for x in r) == [
        ("Forfait 18Go + 5H + WhatsApp Illimité", "18Go", "99"),
        ("Forfait Appels illimités + 60Go", "60Go / 2Go", "249"),
        ("Forfait FLEXI Réseaux Sociaux", "2Go", "49")]
    # Le badge « Nouveau » et l'entête « Les forfaits Max… » ne polluent
    # jamais les titres.
    ok &= not any("Nouveau" in x["offre"] or "Les forfaits" in x["offre"]
                  for x in r)

    r = parse_generic(SAMPLES["generic_darbox"], "test", "Orange", "Box", "Dar Box")
    print(f"[generic_darbox]   {len(r)} offres :",
          [(x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    ok &= {x["prix_dh_mois"] for x in r} == {"199", "299"}
    # Chaque prix garde SA data : pas de contamination par l'offre voisine.
    ok &= {(x["prix_dh_mois"], x["debit_ou_data"]) for x in r} == {
        ("199", "100 Go"), ("299", "300 Go")}

    r = parse_iam_forfaits_liberte(SAMPLES["iam_forfaits_liberte"], "test")
    print(f"[iam_liberte]      {len(r)} offres :",
          [(x['offre'], x['appels_inclus'], x['prix_dh_mois']) for x in r])
    # 6 bundles distincts (la grille dupliquée est dédoublonnée) ; les deux
    # 59 DH et les trois 99 DH restent distincts grâce à la data/aux heures.
    ok &= len(r) == 6
    ok &= {(x["offre"], x["prix_dh_mois"]) for x in r} == {
        ("Forfait Liberte 59 (11 Go)", "59"),
        ("Forfait Liberte 59 (3 Go)", "59"),
        ("Forfait Liberte 99 (20 Go)", "99"),
        ("Forfait Liberte 99 (2 Go)", "99"),
        ("Forfait Liberte 99 (13 Go)", "99"),
        ("Forfait Liberte 119 (22 Go)", "119")}
    ok &= any(x["remarques"] == "300 SMS" for x in r)

    r = parse_iam_box(SAMPLES["iam_box_5g"], "test", "El Manzil 5G")
    print(f"[iam_box_5g]       {len(r)} offres :",
          [(x['offre'], x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    # Le titre répété et la FAQ ne génèrent pas de lignes fantômes.
    ok &= [(x["offre"], x["debit_ou_data"], x["prix_dh_mois"]) for x in r] == [
        ("El Manzil 5G 400 DH", "100 Mb/s", "400")]

    r = parse_iam_box(SAMPLES["iam_box_4g"], "test", "Box 4G+")
    print(f"[iam_box_4g]       {len(r)} offres :",
          [(x['debit_ou_data'], x['appels_inclus'], x['prix_dh_mois']) for x in r])
    # Les pass internet (« 20 DH 2 Go », sans /mois) restent dehors.
    ok &= sorted((x["debit_ou_data"], x["appels_inclus"], x["prix_dh_mois"])
                 for x in r) == [("60 Go", "60 min", "199"),
                                 ("90 Go", "120 min", "350")]
    ok &= all(x["fiabilite"] == "officiel_site" for x in r)

    r = parse_orange_svg_fibre(SAMPLES["orange_svg_page"], "test",
                               fetch=fake_fetch_svg)
    print(f"[orange_svg]       {len(r)} paliers :",
          [(x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    # Paliers triés numériquement, prix lu dans le SVG, hôte du CDN conservé.
    ok &= [(x["debit_ou_data"], x["prix_dh_mois"]) for x in r] == [
        ("20 Mb/s", "249"), ("100 Mb/s", "349"), ("1000 Mb/s", "949")]
    ok &= all("cdn-exemple.orange.ma" in x["source"] for x in r)
    ok &= all(x["fiabilite"] == "officiel_svg" for x in r)

    r = parse_orange_svg_fibre(SAMPLES["orange_svg_page_sans_prix"], "test",
                               fetch=fake_fetch_svg)
    print(f"[orange_svg_vide]  {len(r)} palier(s) (attendu : 0, ligne ignorée)")
    ok &= r == []

    # diff_changes + résumé éditorial : dérivés structurés du master.
    faux = [
        dict(zip(FIELDNAMES, v)) for v in [
            ("2026-08-02", "inwi", "Fibre", "Fibre 20", "20 Mb/s", "", "249",
             "", "", "officiel_site"),
            ("2026-08-02", "inwi", "Forfait mobile", "Forfait A", "10 Go", "",
             "49", "", "", "officiel_site"),
            ("2026-09-02", "inwi", "Fibre", "Fibre 20", "20 Mb/s", "", "199",
             "", "", "officiel_site"),
            ("2026-09-02", "inwi", "Forfait mobile", "Forfait B", "20 Go", "",
             "69", "", "", "officiel_site"),
        ]]
    ch = diff_changes(faux, "2026-08", "2026-09")
    print(f"[diff_changes]     {[(c['type'], c['cle'][2]) for c in ch]}")
    ok &= sorted(c["type"] for c in ch) == ["nouveau", "prix", "retire"]
    ok &= any(c["type"] == "prix" and c["avant"] == "249" and c["prix"] == "199"
              for c in ch)

    # Couverture partielle (mois d'archives) : IAM observé un seul des deux
    # mois -> ses offres ne sortent NI en retiré NI en nouveau, seul le
    # périmètre commun (inwi/Fibre) est comparé.
    partiel = faux + [dict(zip(FIELDNAMES, (
        "2026-08-02", "Maroc Telecom", "Fibre", "Fibre Optique 100M",
        "100 Mb/s", "", "400", "", "", "officiel_archive")))]
    ch = diff_changes(partiel, "2026-08", "2026-09")
    print(f"[diff_scope]       {[(c['type'], c['cle'][0]) for c in ch]}")
    # IAM (couvert seulement en 08) ne sort pas en retiré ; le périmètre
    # commun inwi garde ses 3 mouvements (prix, nouveau, retiré).
    ok &= not any(c["cle"][0] == "Maroc Telecom" for c in ch)
    ok &= sorted(c["type"] for c in ch) == ["nouveau", "prix", "retire"]

    # Shrinkflation : même nom + même prix mais data réduite -> « contenu » ;
    # nouveau×retiré au même prix avec data différente -> « contenu » apparié ;
    # à data identique -> simple « renomme ».
    def _l(d, op, cat, offre, data, prix):
        return dict(zip(FIELDNAMES, (d, op, cat, offre, data, "", prix,
                                     "", "", "officiel_site")))
    shrink = [
        _l("2026-08-02", "inwi", "Forfait mobile", "Forfait 99", "30 Go", "99"),
        _l("2026-09-02", "inwi", "Forfait mobile", "Forfait 99", "25 Go", "99"),
        _l("2026-08-02", "Orange", "Forfait mobile", "Yo 30Go", "30 Go", "149"),
        _l("2026-09-02", "Orange", "Forfait mobile", "Yo 20Go", "20 Go", "149"),
        _l("2026-08-02", "Maroc Telecom", "Forfait mobile", "Liberte X", "50 Go", "199"),
        _l("2026-09-02", "Maroc Telecom", "Forfait mobile", "Liberte Y", "50 Go", "199"),
    ]
    ch = diff_changes(shrink, "2026-08", "2026-09")
    print(f"[diff_contenu]     {[(c['type'], c['cle'][2]) for c in ch]}")
    ok &= sorted(c["type"] for c in ch) == ["contenu", "contenu", "renomme"]
    ok &= any(c["type"] == "contenu" and c["cle"][2] == "Forfait 99"
              and c["avant_mesure"] == (30, "Go") and c["mesure"] == (25, "Go")
              for c in ch)
    ok &= any(c["type"] == "contenu" and c.get("avant_offre") == "Yo 30Go"
              for c in ch)
    ok &= any(c["type"] == "renomme" and c["avant_offre"] == "Liberte X"
              and c["cle"][2] == "Liberte Y" for c in ch)
    lib = _libelle_change([c for c in ch if c["type"] == "contenu"
                           and c["cle"][2] == "Forfait 99"][0])
    print(f"[libelle_contenu]  {lib}")
    ok &= "réduit" in lib and "30 Go -> 25 Go" in lib

    # Garde-fou : relevé normal (1 changement) passe ; relevé « massacré »
    # (tous les prix changés / volume effondré) est bloqué.
    ref_master = [_l("2026-08-02", "inwi", "Fibre", f"Offre {i}", "20 Go",
                     str(100 + i)) for i in range(10)]
    normal = [_l("2026-09-02", "inwi", "Fibre", f"Offre {i}", "20 Go",
                 str(100 + i)) for i in range(10)]
    normal[0]["prix_dh_mois"] = "999"
    massacre = [_l("2026-09-02", "inwi", "Fibre", f"Offre {i}", "20 Go",
                   str(500 + i)) for i in range(10)]
    effondre = normal[:3]
    print(f"[garde_fou]        normal={garde_fou(normal, '2026-09', ref_master)} "
          f"massacre={len(garde_fou(massacre, '2026-09', ref_master))} alerte(s) "
          f"effondre={len(garde_fou(effondre, '2026-09', ref_master))} alerte(s)")
    ok &= garde_fou(normal, "2026-09", ref_master) == []
    ok &= len(garde_fou(massacre, "2026-09", ref_master)) >= 1
    ok &= len(garde_fou(effondre, "2026-09", ref_master)) >= 1
    resume = texte_resume(faux, "2026-09", ["2026-08", "2026-09"])
    print(f"[texte_resume]     {resume[:110]}…")
    ok &= "septembre 2026" in resume and "199 DH/mois" in resume
    ok &= "baisse de 249 à 199" in resume

    print("[norm_debit]       ", {k: norm_debit(k) for k in
                                  ("100 Mb/s", "1 Gb/s", "200 Méga", "25 Go")})
    ok &= (norm_debit("100 Mb/s") == norm_debit("100 Méga") == "100Mb")
    ok &= norm_debit("1 Gb/s") == "1000Mb" and norm_debit("25 Go") == "25Go"

    anomalies = signaler_anomalies([
        row("inwi", "Fibre", "Test", "20 Mb/s", "", "249"),
        row("inwi", "Fibre", "Prix aberrant", "20 Mb/s", "", "5"),
        row("inwi", "Fibre", "Fiabilite invalide", "20 Mb/s", "", "249",
            fiabilite="presse_2025-04"),
    ])
    ok &= len(anomalies) == 2

    print("\nRésultat :", "TOUS LES PARSERS PASSENT" if ok else "ECHEC — voir ci-dessus")
    return 0 if ok else 1

# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Baromètre télécoms Maroc")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ops = ["iam", "orange", "inwi"]

    p_run = sub.add_parser("run", help="effectuer le relevé du mois")
    p_run.add_argument("--only", nargs="+", choices=ops,
                       help="limiter à certains opérateurs")
    p_run.add_argument("--no-js", action="store_true",
                       help="sauter les pages nécessitant Playwright")
    p_run.add_argument("--force", action="store_true",
                       help="publier malgré les alertes du garde-fou")

    p_check = sub.add_parser("check", help="tester la joignabilité des sources")
    p_check.add_argument("--only", nargs="+", choices=ops)

    p_replay = sub.add_parser(
        "replay", help="re-parser les dumps data/raw sans réseau")
    p_replay.add_argument("--month", help="mois du dump (YYYY-MM), défaut : le plus récent")
    p_replay.add_argument("--only", nargs="+", choices=ops)
    p_replay.add_argument("--write", action="store_true",
                          help="enregistrer le relevé au lieu d'un simple aperçu")

    p_bf = sub.add_parser(
        "backfill", help="reconstruire l'historique via web.archive.org")
    p_bf.add_argument("--from", dest="m_from", required=True,
                      help="premier mois (YYYY-MM)")
    p_bf.add_argument("--to", dest="m_to", help="dernier mois, défaut : courant")
    p_bf.add_argument("--only", nargs="+", choices=ops)
    p_bf.add_argument("--write", action="store_true",
                      help="enregistrer les mois reconstitués")
    p_bf.add_argument("--refetch", action="store_true",
                      help="re-traiter aussi les mois d'archives déjà en base "
                           "(les mois avec données live restent intouchables)")

    sub.add_parser("test", help="valider les parsers sur les échantillons")
    sub.add_parser("diff", help="comparer les deux derniers relevés")
    sub.add_parser("compare", help="confronter le dernier relevé à la référence")
    sub.add_parser("feed", help="régénérer changements.xml + resumes.json")

    p_cat = sub.add_parser(
        "catalogues", help="fusionner les extraits de catalogues officiels")
    p_cat.add_argument("--write", action="store_true")

    p_disc = sub.add_parser(
        "discover", help="inventaire Wayback des URLs historiques (lent)")
    p_disc.add_argument("--from", dest="m_from", default="2022-01")
    p_disc.add_argument("--to", dest="m_to")
    args = ap.parse_args()

    if args.cmd == "run":
        sys.exit(run(only=args.only, no_js=args.no_js, force=args.force))
    if args.cmd == "check":
        sys.exit(check(only=args.only))
    if args.cmd == "replay":
        sys.exit(replay(month=args.month, only=args.only, write=args.write))
    if args.cmd == "backfill":
        sys.exit(backfill(args.m_from, args.m_to, only=args.only,
                          write=args.write, refetch=args.refetch))
    if args.cmd == "test":
        sys.exit(test())
    if args.cmd == "diff":
        diff()
    if args.cmd == "compare":
        comparer_reference()
    if args.cmd == "feed":
        ecrire_feed()
        ecrire_resumes()
    if args.cmd == "catalogues":
        sys.exit(catalogues(write=args.write))
    if args.cmd == "discover":
        sys.exit(discover(m_from=args.m_from, m_to=args.m_to))


if __name__ == "__main__":
    main()
