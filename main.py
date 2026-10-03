#!/usr/bin/env python3
"""Astrolia : horoscope quotidien 100 % automatisé.

Commandes :
  python main.py generer     -> texte (IA) + 14 visuels dans posts/AAAA-MM-JJ/
  python main.py publier     -> publie le carrousel sur Instagram
  python main.py rafraichir  -> renouvelle le jeton Instagram (60 jours)

Variables d'environnement : voir README.md
"""
import datetime as dt
import json
import math
import os
import pathlib
import random
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).parent
POSTS = ROOT / "posts"
FONTS = ROOT / "fonts"
W, H = 1080, 1350  # format 4:5 Instagram

SIGNES = [
    ("Bélier", "♈", "feu"), ("Taureau", "♉", "terre"), ("Gémeaux", "♊", "air"),
    ("Cancer", "♋", "eau"), ("Lion", "♌", "feu"), ("Vierge", "♍", "terre"),
    ("Balance", "♎", "air"), ("Scorpion", "♏", "eau"), ("Sagittaire", "♐", "feu"),
    ("Capricorne", "♑", "terre"), ("Verseau", "♒", "air"), ("Poissons", "♓", "eau"),
]
COULEURS = {"feu": "#E8795A", "terre": "#8FA98B", "air": "#8FB8DE", "eau": "#7A8FD6"}
OR, CREME = "#E8C77D", "#FAF6EF"
FOND_HAUT, FOND_BAS = (27, 16, 51), (58, 35, 105)
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
INTERDITS = ["garanti", "à coup sûr", "guérison", "guérir", "diagnostic",
             "médicament", "investis", "bitcoin", "cancer du", "dépression"]
MENTION = "Astrologie proposée à titre de divertissement et d'inspiration."

PROMPT = """Tu es la voix d'Astrolia, une marque d'astrologie francophone moderne, chaleureuse et un brin espiègle. Public : 18-35 ans sur Instagram.

DATE DU JOUR : {{DATE}}
CONTEXTE ASTRAL (données réelles, ne rien inventer d'autre) : {{TRANSIT}}

MISSION : écrire l'horoscope du jour pour les 12 signes, dans cet ordre exact : Bélier, Taureau, Gémeaux, Cancer, Lion, Vierge, Balance, Scorpion, Sagittaire, Capricorne, Verseau, Poissons.

RÈGLES DE STYLE
- Tutoiement, phrases courtes, rythme vif
- AUCUN emoji, aucun symbole spécial
- Ton bienveillant, jamais anxiogène ni fataliste
- Aucune promesse médicale, financière ou de résultat garanti
- Chaque signe a un angle différent : pas de formule répétée entre les signes
- Appuie l'énergie du jour sur le contexte astral fourni
- Évite les clichés (« les astres te sourient »)
- N'utilise pas ces accroches déjà publiées récemment : {{HISTORIQUE}}

STRUCTURE PAR SIGNE
- accroche : 8 mots max
- texte : 25 à 35 mots
- amour : 1 phrase, 12 mots max
- travail : 1 phrase, 12 mots max
- mantra : 5 mots max
- note : entier de 1 à 5 (énergie du jour)

ÉLÉMENTS GLOBAUX
- hook_couverture : titre d'ouverture viral, 12 mots max, qui cite 3 signes réels (ex : « 3 signes vont recevoir une réponse aujourd'hui : Lion, Vierge, Poissons »)
- signe_star : le signe mis en avant aujourd'hui
- cta_commentaire : une question qui pousse à commenter
- legende_post : légende Instagram de 2 phrases, suivie de 5 hashtags

Réponds UNIQUEMENT avec un JSON valide, sans texte autour, sans balises Markdown :
{"hook_couverture": "", "signe_star": "", "cta_commentaire": "", "legende_post": "",
 "signes": [{"signe": "", "accroche": "", "texte": "", "amour": "", "travail": "", "mantra": "", "note": 0}]}"""


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------
def aujourd_hui():
    return dt.datetime.now(ZoneInfo("Europe/Paris")).date()


def date_fr(d):
    return f"{d.day} {MOIS[d.month - 1]} {d.year}"


def dossier_du_jour():
    return POSTS / aujourd_hui().isoformat()


def historique_accroches(n=3):
    """Accroches des n derniers posts, pour éviter les répétitions."""
    accroches = []
    for p in sorted(POSTS.glob("*/post.json"))[-n:]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            accroches += [s["accroche"] for s in data["signes"]]
        except Exception:
            pass
    return " | ".join(accroches) if accroches else "aucune"


# --------------------------------------------------------------------------
# Contexte astral réel (calculé localement, gratuit, sans API)
# --------------------------------------------------------------------------
def contexte_astral(d):
    try:
        import ephem
    except ImportError:
        return "Pas de données astrales précises : reste sur des thèmes généraux de saison."

    jour = ephem.Date(d.strftime("%Y/%m/%d") + " 12:00")

    def longitude(corps, date):
        c = corps()
        c.compute(date, epoch=date)
        return math.degrees(ephem.Ecliptic(c).lon) % 360

    def signe(lon):
        return SIGNES[int(lon // 30) % 12][0]

    lune = ephem.Moon()
    lune.compute(jour)
    infos = [
        f"Lune en {signe(longitude(ephem.Moon, jour))} (illumination {lune.phase:.0f} %)",
        f"Soleil en {signe(longitude(ephem.Sun, jour))}",
    ]
    ecart = (longitude(ephem.Mercury, jour + 1) - longitude(ephem.Mercury, jour) + 180) % 360 - 180
    if ecart < 0:
        infos.append("Mercure est rétrograde")
    return ". ".join(infos) + "."


# --------------------------------------------------------------------------
# Génération du texte (Gemini gratuit par défaut, Claude en option)
# --------------------------------------------------------------------------
MODELES_GEMINI = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"]


class AucunModele(Exception):
    """Aucun modèle Gemini utilisable : inutile de réessayer."""
_modele_ok = None


def modeles_decouverts():
    """Demande à Google la liste des modèles « flash » disponibles pour ta clé."""
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                     params={"pageSize": 200}, timeout=60)
    if not r.ok:
        print(f"Liste des modèles refusée ({r.status_code}) : {r.text[:400]}")
        return []
    noms = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])]
    flash = [n for n in noms if "flash" in n and not any(x in n for x in ("image", "tts", "live", "audio", "preview-"))]
    return sorted(flash, reverse=True)


def message_google(r):
    try:
        return r.json()["error"]["message"][:160]
    except Exception:
        return r.text[:160].replace("\n", " ")


def appeler_gemini(prompt):
    """Essaie plusieurs modèles : si l'un est introuvable (404), interdit (403) ou saturé (429), passe au suivant."""
    global _modele_ok
    force = os.getenv("LLM_MODEL")
    if _modele_ok:
        candidats = [_modele_ok]
    else:
        candidats = ([force] if force else []) + [m for m in MODELES_GEMINI if m != force]
    deja_decouvert = False
    bilan = []
    i = 0
    while i < len(candidats):
        modele = candidats[i]
        i += 1
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{modele}:generateContent",
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}},
            timeout=120)
        if r.status_code in (403, 404, 429):
            bilan.append(f"{modele} -> {r.status_code} : {message_google(r)}")
            if i >= len(candidats) and not deja_decouvert:
                deja_decouvert = True
                candidats += [m for m in modeles_decouverts() if m not in candidats]
            continue
        if not r.ok:
            raise RuntimeError(f"Gemini {r.status_code} : {r.text[:400]}")
        _modele_ok = modele
        print(f"Modèle utilisé : {modele}")
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise AucunModele("BILAN DES MODELES GEMINI :\n" + "\n".join(bilan))


MODELES_MISTRAL = ["mistral-small-latest", "open-mistral-nemo", "ministral-8b-latest",
                   "mistral-medium-latest", "mistral-large-latest"]


def appeler_mistral(prompt):
    """Appelle Mistral ; si un modèle est limité (429), essaie le suivant, patiente et réessaie."""
    force = os.getenv("LLM_MODEL")
    modeles = ([force] if force else []) + [m for m in MODELES_MISTRAL if m != force]
    bilan = []
    for tentative in range(1, 11):
        modele = modeles[(tentative - 1) % len(modeles)]
        r = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}", "Content-Type": "application/json"},
            json={"model": modele,
                  "messages": [{"role": "user", "content": prompt}],
                  "response_format": {"type": "json_object"}, "temperature": 0.9},
            timeout=120)
        if r.ok:
            print(f"Modèle utilisé : {modele}")
            return r.json()["choices"][0]["message"]["content"]
        infos = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower() or k.lower() == "retry-after"}
        bilan.append(f"{modele} -> {r.status_code} : {message_google(r)} {infos if infos else ''}".strip())
        print(bilan[-1])
        if r.status_code in (401, 403):
            break  # clé refusée : inutile d'insister
        if r.status_code in (429, 500, 502, 503, 504) and tentative < 10:
            try:
                attente = int(float(r.headers.get("Retry-After", "")))
            except ValueError:
                attente = 0
            time.sleep(min(max(attente, 5), 30))
            continue
        if r.status_code not in (400, 404):
            break
    raise AucunModele("BILAN MISTRAL :\n" + "\n".join(bilan[-6:]))


def appeler_ia(prompt):
    fournisseur = os.getenv("LLM_PROVIDER") or ("mistral" if os.getenv("MISTRAL_API_KEY") else "gemini")
    if fournisseur == "mistral":
        texte = appeler_mistral(prompt)
        return json.loads(texte.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    if fournisseur == "claude":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.getenv("LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": 4000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120)
        r.raise_for_status()
        texte = r.json()["content"][0]["text"]
    else:
        texte = appeler_gemini(prompt)
    texte = texte.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(texte)


def valider(data):
    attendu = [s[0] for s in SIGNES]
    assert [s["signe"] for s in data["signes"]] == attendu, "signes manquants ou dans le mauvais ordre"
    for s in data["signes"]:
        for cle in ("accroche", "texte", "amour", "travail", "mantra"):
            assert str(s[cle]).strip(), f"{s['signe']} : champ vide ({cle})"
        assert len(s["texte"].split()) <= 45, f"{s['signe']} : texte trop long"
        assert 1 <= int(s["note"]) <= 5, f"{s['signe']} : note invalide"
    for cle in ("hook_couverture", "signe_star", "cta_commentaire", "legende_post"):
        assert str(data[cle]).strip(), f"champ vide : {cle}"
    blob = json.dumps(data, ensure_ascii=False).lower()
    for mot in INTERDITS:
        assert mot not in blob, f"mot interdit détecté : {mot}"


def generer_texte(d):
    prompt = (PROMPT.replace("{{DATE}}", date_fr(d))
              .replace("{{TRANSIT}}", contexte_astral(d))
              .replace("{{HISTORIQUE}}", historique_accroches()))
    derniere_erreur = None
    for essai in range(1, 5):
        try:
            data = appeler_ia(prompt)
            valider(data)
            return data
        except AucunModele:
            raise
        except Exception as e:  # on relance la génération
            derniere_erreur = e
            print(f"Essai {essai} refusé : {e}")
            time.sleep(5)
    raise RuntimeError(f"Génération impossible après 4 essais : {derniere_erreur}")


# --------------------------------------------------------------------------
# Visuels (Pillow, gratuit)
# --------------------------------------------------------------------------
def police(nom, taille, secours="DejaVuSans.ttf"):
    chemin = FONTS / nom
    if chemin.exists():
        return ImageFont.truetype(str(chemin), taille)
    try:
        return ImageFont.truetype(secours, taille)
    except OSError:
        return ImageFont.load_default(taille)


def titre(taille):
    return police("Playfair.ttf", taille, "DejaVuSerif-Bold.ttf")


def corps(taille):
    return police("Inter.ttf", taille, "DejaVuSans.ttf")


def glyphes(taille):
    return ImageFont.truetype("DejaVuSans.ttf", taille)  # contient les symboles du zodiaque


def fond(graine):
    img = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        px.line([(0, y), (W, y)], fill=tuple(int(FOND_HAUT[i] + (FOND_BAS[i] - FOND_HAUT[i]) * t) for i in range(3)))
    rnd = random.Random(graine)
    for _ in range(80):
        x, y, r = rnd.randrange(W), rnd.randrange(H), rnd.choice([1, 1, 2, 3])
        px.ellipse([x - r, y - r, x + r, y + r], fill=OR if rnd.random() < 0.25 else (200, 190, 220))
    return img


def decouper(draw, texte, fnt, largeur):
    lignes, cur = [], ""
    for mot in texte.split():
        essai = (cur + " " + mot).strip()
        if draw.textlength(essai, font=fnt) <= largeur:
            cur = essai
        else:
            lignes.append(cur)
            cur = mot
    if cur:
        lignes.append(cur)
    return lignes


def ecrire(draw, texte, fnt, y, couleur, centre=True, x=80, largeur=W - 160, interligne=1.3):
    for ligne in decouper(draw, texte, fnt, largeur):
        xx = x + (largeur - draw.textlength(ligne, font=fnt)) / 2 if centre else x
        draw.text((xx, y), ligne, font=fnt, fill=couleur)
        y += int(fnt.size * interligne)
    return y


def slide_couverture(data, d):
    img = fond(f"{d}-0")
    dr = ImageDraw.Draw(img)
    ecrire(dr, "A S T R O L I A", corps(34), 150, OR)
    ecrire(dr, date_fr(d), corps(34), 205, CREME)
    y = ecrire(dr, data["hook_couverture"], titre(84), 430, CREME, interligne=1.25)
    dr.line([(W / 2 - 60, y + 40), (W / 2 + 60, y + 40)], fill=OR, width=3)
    ecrire(dr, "Fais défiler pour lire ton signe  →", corps(34), H - 180, OR)
    return img


def slide_signe(s, d, i):
    nom, symbole, element = SIGNES[i]
    c = COULEURS[element]
    img = fond(f"{d}-{i + 1}")
    dr = ImageDraw.Draw(img)
    dr.text((W / 2, 150), symbole, font=glyphes(140), fill=c, anchor="mm")
    dr.text((W / 2, 290), nom, font=titre(88), fill=CREME, anchor="mm")
    y = ecrire(dr, s["accroche"], titre(52), 360, OR, interligne=1.2)
    y = ecrire(dr, s["texte"], corps(38), y + 25, CREME, interligne=1.38)
    y += 25
    for etiquette, cle in (("AMOUR", "amour"), ("TRAVAIL", "travail")):
        dr.text((90, y), etiquette, font=corps(26), fill=c)
        y = ecrire(dr, s[cle], corps(34), y + 38, CREME, centre=False, x=90, largeur=W - 180, interligne=1.3) + 14
    dr.line([(90, H - 215), (W - 90, H - 215)], fill=c, width=2)
    dr.text((W / 2, H - 160), f"« {s['mantra']} »", font=titre(42), fill=OR, anchor="mm")
    etoiles = "★" * int(s["note"]) + "☆" * (5 - int(s["note"]))
    dr.text((W / 2, H - 85), f"Énergie du jour  {etoiles}", font=glyphes(32), fill=CREME, anchor="mm")
    return img


def slide_cta(data, d):
    img = fond(f"{d}-13")
    dr = ImageDraw.Draw(img)
    y = ecrire(dr, "Ton mini-thème natal est offert", titre(78), 330, CREME, interligne=1.25)
    y = ecrire(dr, "Commente ASTRO et je te l'envoie en message privé", corps(42), y + 50, OR)
    ecrire(dr, data["cta_commentaire"], corps(36), y + 70, CREME)
    ecrire(dr, "Enregistre ce post et envoie-le à un signe qui en a besoin", corps(30), H - 170, OR)
    return img


def generer():
    d = aujourd_hui()
    dossier = dossier_du_jour()
    if (dossier / "post.json").exists():
        print("Post du jour déjà généré, rien à faire.")
        return
    dossier.mkdir(parents=True, exist_ok=True)
    data = generer_texte(d)
    images = [slide_couverture(data, d)]
    images += [slide_signe(s, d, i) for i, s in enumerate(data["signes"])]
    images.append(slide_cta(data, d))
    for n, img in enumerate(images, 1):
        img.save(dossier / f"slide_{n:02d}.jpg", "JPEG", quality=92)
    legende = f"{data['legende_post']}\n\nCommente ASTRO pour recevoir ton mini-thème natal gratuit.\n\n{MENTION}"
    data["legende_finale"] = legende
    (dossier / "post.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(images)} visuels créés dans {dossier}")


# --------------------------------------------------------------------------
# Publication Instagram (API officielle, connexion Instagram, sans Page Facebook)
# --------------------------------------------------------------------------
def api_instagram(methode, chemin, **params):
    version = os.getenv("GRAPH_VERSION", "v24.0")
    url = f"https://graph.instagram.com/{version}/{chemin}"
    params["access_token"] = os.environ["IG_TOKEN"]
    r = requests.request(methode, url, **({"data": params} if methode == "POST" else {"params": params}), timeout=60)
    if not r.ok:
        raise RuntimeError(f"Instagram {r.status_code} : {r.text}")
    return r.json()


def publier():
    dossier = dossier_du_jour()
    if not (dossier / "post.json").exists():
        raise SystemExit("Aucun post généré aujourd'hui : lance d'abord « generer ».")
    if (dossier / "published.txt").exists():
        print("Déjà publié aujourd'hui.")
        return
    if os.getenv("DRY_RUN") == "1":
        print("Mode test (DRY_RUN=1) : rien n'est publié.")
        return
    data = json.loads((dossier / "post.json").read_text(encoding="utf-8"))
    depot = os.environ["GITHUB_REPOSITORY"]
    branche = os.getenv("GITHUB_REF_NAME", "main")
    base = f"https://raw.githubusercontent.com/{depot}/{branche}/posts/{dossier.name}"
    ig = os.environ["IG_USER_ID"]

    enfants = []
    for n in range(1, 15):
        res = api_instagram("POST", f"{ig}/media", image_url=f"{base}/slide_{n:02d}.jpg", is_carousel_item="true")
        enfants.append(res["id"])
    carrousel = api_instagram("POST", f"{ig}/media", media_type="CAROUSEL",
                              children=",".join(enfants), caption=data["legende_finale"])["id"]
    for _ in range(40):
        statut = api_instagram("GET", carrousel, fields="status_code").get("status_code")
        if statut == "FINISHED":
            break
        if statut in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Traitement Instagram en échec : {statut}")
        time.sleep(5)
    api_instagram("POST", f"{ig}/media_publish", creation_id=carrousel)
    (dossier / "published.txt").write_text(dt.datetime.now().isoformat(), encoding="utf-8")
    print("Carrousel publié.")


def rafraichir():
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": os.environ["IG_TOKEN"]}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Renouvellement impossible : {r.text}")
    nouveau = r.json()["access_token"]
    if os.getenv("GH_PAT"):
        subprocess.run(["gh", "secret", "set", "IG_TOKEN", "--body", nouveau, "--repo", os.environ["GITHUB_REPOSITORY"]],
                       check=True, env={**os.environ, "GH_TOKEN": os.environ["GH_PAT"]})
        print("Jeton Instagram renouvelé et enregistré dans les secrets GitHub.")
    else:
        raise SystemExit("Jeton renouvelé mais NON enregistré : ajoute le secret GH_PAT (voir README).")


if __name__ == "__main__":
    commande = sys.argv[1] if len(sys.argv) > 1 else ""
    {"generer": generer, "publier": publier, "rafraichir": rafraichir}.get(
        commande, lambda: sys.exit(__doc__))()
