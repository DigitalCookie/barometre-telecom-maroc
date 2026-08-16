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
  python barometre.py test                # valider les parsers sur échantillons
  python barometre.py diff                # comparer les 2 derniers relevés
  python barometre.py compare             # confronter le relevé au releve de reference

Sorties :
  data/barometre.csv            base cumulée (une ligne par offre et par mois)
  data/releve_YYYY-MM.csv       snapshot du mois
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
FIABILITES = {"officiel_site", "officiel_svg", "officiel_catalogue",
              "officiel_js_generique", "a_completer"}

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

    def text(self, url: str, settle_ms: int = 5000, clicks=()) -> str:
        """Texte visible d'une page après exécution du JavaScript.

        `clicks` : libellés d'onglets à cliquer après le chargement (ex. les
        gammes de la boutique Orange) — le texte de chaque état est concaténé,
        les parsers dédoublonnent. Best effort : un clic raté est ignoré."""
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
        rows.append(row("Orange", "Fibre", f"Fibre {mega} Mega (residentiel)",
                        f"{mega} Mb/s", "", prix, note, svg_url,
                        "officiel_svg" if prix else "a_completer"))
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
    return dedup(rows)


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
    (run réel 08/2026)."""
    t = norm(text)
    rows = []
    pat = re.compile(r"(\d{1,3})\s*GO\s+(\d{1,2})\s*H\s*d'appels.{0,80}?"
                     r"(\d{2,3})\s*DHS?\s*/\s*mois", re.I)
    for go, heures, prix in pat.findall(t):
        rows.append(row("Orange", "Forfait mobile", f"Yoxo {prix} DH",
                        f"{go} Go", f"{heures}h",
                        prix, "100% digital sans engagement", url))
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
         parse=lambda txt, u: parse_generic(txt, u, "Maroc Telecom", "Box",
                                            "El Manzil 5G")),
    dict(op="iam", label="IAM Box 4G+", method="js",
         url="https://www.iam.ma/box-4g",
         parse=lambda txt, u: parse_generic(txt, u, "Maroc Telecom", "Box",
                                            "Box 4G+")),
    # ------------------------------------------------------------- Orange
    dict(op="orange", label="Orange fibre (grille pro, HTML)", method="http",
         url="https://pro.orange.ma/Fixe-et-Internet/Business-Box-Fibre",
         parse=lambda txt, u: parse_orange_pro_fibre(txt, u)),
    dict(op="orange", label="Orange fibre residentiel (SVG)", method="html_raw",
         url="https://www.orange.ma/WiFi-a-la-Maison/Fibre-d-Orange/Offres-Fibre-d-Orange",
         parse=lambda html, u: parse_orange_svg_fibre(html, u)),
    # Les cartes Yo Max sont derrière un onglet (seuls les Yo s'affichent au
    # chargement — constat du run du 16/08/2026) : on clique les gammes.
    dict(op="orange", label="Orange forfaits (boutique)", method="js",
         url="https://boutique.orange.ma/offres-mobile",
         clicks=("Forfaits Yo Max", "Tous les forfaits"),
         parse=lambda txt, u: parse_orange_boutique_forfaits(txt, u)),
    dict(op="orange", label="Orange Dar Box 5G", method="js",
         url="https://boutique.orange.ma/offres-dar-box/dar-box-5g",
         parse=lambda txt, u: parse_orange_darbox(txt, u, "5G")),
    dict(op="orange", label="Orange Dar Box 4G+", method="js",
         url="https://boutique.orange.ma/dar-box",
         parse=lambda txt, u: parse_orange_darbox(txt, u, "4G+")),
    dict(op="orange", label="Yoxo (digital)", method="js",
         url="https://www.yoxo.ma/",
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


def run(only=None, no_js=False):
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
                    content = nav.text(page["url"], clicks=page.get("clicks", ()))
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

    ecrire_releve(all_rows, month)
    if failures:
        print("\nPoints d'attention :")
        for label, msg in failures:
            print(f"  - {label}: {msg}")
    signaler_anomalies(all_rows)
    diff()
    comparer_reference(all_rows)
    return 0


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


def diff():
    rows = read_master()
    months = sorted({r_["date_releve"][:7] for r_ in rows})
    if len(months) < 2:
        print("\n(diff) Un seul relevé en base — comparaison possible dès le mois prochain.")
        return
    prev, curr = months[-2], months[-1]

    def index(month):
        return {(r_["operateur"], r_["categorie"], r_["offre"]): r_
                for r_ in rows if r_["date_releve"][:7] == month
                and r_["prix_dh_mois"]}

    a, b = index(prev), index(curr)
    changes = []
    for key in sorted(b):
        if key not in a:
            changes.append(f"  + NOUVEAU  {' / '.join(key)} : {b[key]['prix_dh_mois']} DH")
        elif a[key]["prix_dh_mois"] != b[key]["prix_dh_mois"]:
            changes.append(f"  ~ PRIX     {' / '.join(key)} : "
                           f"{a[key]['prix_dh_mois']} -> {b[key]['prix_dh_mois']} DH")
    for key in sorted(set(a) - set(b)):
        changes.append(f"  - RETIRE   {' / '.join(key)} (était {a[key]['prix_dh_mois']} DH)")

    print(f"\n=== Baromètre {prev} -> {curr} ===")
    print("\n".join(changes) if changes else "  Aucun changement de grille.")

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
    # Page résidentielle Orange : les cartes tarifaires sont des <img> SVG.
    # Ordre volontairement mélangé pour vérifier le tri numérique des paliers.
    "orange_svg_page": """
        <img src="https://cdn-exemple.orange.ma/FibreOrange/fibre-cards/100go.svg">
        <img src="https://cdn-exemple.orange.ma/FibreOrange/fibre-cards/1000go.svg">
        <img src="https://cdn-exemple.orange.ma/FibreOrange/fibre-cards/20go.svg">
    """,
}

# Contenu factice des SVG, indexé par palier — utilisé par le test hors ligne.
SVG_SAMPLES = {
    "20": '<svg><text>20 Méga</text><text>249</text><text>Dh/mois</text></svg>',
    "100": '<svg><text>100 Méga</text><text>349</text><text>Dh/mois</text></svg>',
    "1000": '<svg><text>1000 Méga</text><text>949</text><text>Dh/mois</text></svg>',
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
    ok &= sorted((x["debit_ou_data"], x["prix_dh_mois"]) for x in r) == [
        ("11Go", "49"), ("3Go", "49")]
    ok &= not any("Yo Max" in x["offre"] for x in r)  # le menu ne sort jamais

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

    r = parse_orange_svg_fibre(SAMPLES["orange_svg_page"], "test",
                               fetch=fake_fetch_svg)
    print(f"[orange_svg]       {len(r)} paliers :",
          [(x['debit_ou_data'], x['prix_dh_mois']) for x in r])
    # Paliers triés numériquement, prix lu dans le SVG, hôte du CDN conservé.
    ok &= [(x["debit_ou_data"], x["prix_dh_mois"]) for x in r] == [
        ("20 Mb/s", "249"), ("100 Mb/s", "349"), ("1000 Mb/s", "949")]
    ok &= all("cdn-exemple.orange.ma" in x["source"] for x in r)
    ok &= all(x["fiabilite"] == "officiel_svg" for x in r)

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

    p_check = sub.add_parser("check", help="tester la joignabilité des sources")
    p_check.add_argument("--only", nargs="+", choices=ops)

    p_replay = sub.add_parser(
        "replay", help="re-parser les dumps data/raw sans réseau")
    p_replay.add_argument("--month", help="mois du dump (YYYY-MM), défaut : le plus récent")
    p_replay.add_argument("--only", nargs="+", choices=ops)
    p_replay.add_argument("--write", action="store_true",
                          help="enregistrer le relevé au lieu d'un simple aperçu")

    sub.add_parser("test", help="valider les parsers sur les échantillons")
    sub.add_parser("diff", help="comparer les deux derniers relevés")
    sub.add_parser("compare", help="confronter le dernier relevé à la référence")
    args = ap.parse_args()

    if args.cmd == "run":
        sys.exit(run(only=args.only, no_js=args.no_js))
    if args.cmd == "check":
        sys.exit(check(only=args.only))
    if args.cmd == "replay":
        sys.exit(replay(month=args.month, only=args.only, write=args.write))
    if args.cmd == "test":
        sys.exit(test())
    if args.cmd == "diff":
        diff()
    if args.cmd == "compare":
        comparer_reference()


if __name__ == "__main__":
    main()
