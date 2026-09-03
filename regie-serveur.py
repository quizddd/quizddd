# -*- coding: utf-8 -*-
"""
Regie Quiz - serveur de partie.

Sert trois choses :
  /            la console de l'animateur (regie-quiz.html)
  /jouer       la page des joueurs (joueur.html)
  /api/...     le relais entre les deux

L'animateur garde son quiz dans SON navigateur. Le serveur ne stocke qu'une
partie en cours : qui joue, la question affichee, son media, et les reponses.
Rien n'est ecrit sur le disque, tout disparait a l'arret.

Chaque partie a un code a cinq lettres. Les joueurs ouvrent /jouer, entrent le
code et un pseudo. Aucune inscription, aucun mot de passe.

Lancement local :
    python regie-serveur.py

En ligne, l'hebergeur fournit le port dans la variable PORT.
"""

import asyncio
import io
import json
import os
import random
import string
import sys
import time
import webbrowser

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
AVATARS_DIR = os.path.join(HERE, "avatars")
CONSOLE = os.path.join(HERE, "regie-quiz.html")
JOUEUR = os.path.join(HERE, "joueur.html")

PORT = int(os.environ.get("PORT", "8777"))
HOTE = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
EN_LIGNE = bool(os.environ.get("PORT"))          # vrai chez un hebergeur
VERSION = "2026-09-01 avatars"

# Code d'animateur, facultatif. Defini dans les variables d'environnement de
# l'hebergeur (jamais dans le depot), il verrouille l'ouverture de parties :
# les joueurs n'en ont pas besoin, seul celui qui anime doit le connaitre.
CLE_ANIMATEUR = (os.environ.get("CODE_ANIMATEUR") or "").strip()

MEDIA_MAX = 48 * 1024 * 1024     # au-dela, on refuse l'envoi du media
SALON_MAX = 12                   # parties simultanees
# L'offre gratuite de l'hebergeur tient dans 512 Mo. Un media de 48 Mo par
# partie suffirait a la faire tuer en pleine soiree : on plafonne le total.
MEMOIRE_MAX = 180 * 1024 * 1024
INACTIF = 6 * 3600               # une partie oubliee expire au bout de 6 h
LETTRES = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # sans I ni O, illisibles a l'oral

AV_N = 36                        # avatars dessines par le code, numerotes 0..35
AVATAR_MAX = 400 * 1024          # une image du pack
AVATAR_PACK_NB = 60              # images dans le pack d'un salon
AVATAR_PACK_MAX = 12 * 1024 * 1024


def say(*parts):
    print("  " + " ".join(str(p) for p in parts), flush=True)


# ------------------------------------------------------------------ avatars
def avatar_defaut(nom):
    """Le pseudo decide de la figure : celui qui revient apres une coupure
    retrouve la sienne, et deux ecrans affichent la meme. On pioche dans les
    images du depot ; les figures dessinees ne servent plus que si ce dossier
    est vide, pour qu'une installation neuve ne soit pas sans visages."""
    n = sum(ord(c) for c in (nom or ""))
    if PACK_FIXE:
        return "c:" + PACK_FIXE[n % len(PACK_FIXE)]["id"]
    return "g:%d" % (n % AV_N)


def avatar_valide(salon, valeur, nom):
    """Cette chaine arrive d'un navigateur : tout ce qui n'est pas un avatar
    connu retombe sur celui du pseudo, sinon n'importe quoi finirait a
    l'ecran de tout le monde."""
    v = str(valeur or "").strip()[:64]
    if v.startswith("g:"):
        try:
            n = int(v[2:])
        except (TypeError, ValueError):
            n = -1
        if 0 <= n < AV_N:
            return "g:%d" % n
    elif v.startswith("c:"):
        ident = v[2:]
        if any(a["id"] == ident for a in salon.tout_le_pack()):
            return v
    return avatar_defaut(nom)


IMAGES_OK = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def type_image(annonce, nom=""):
    """Un <img> reste vide si le type ment. Surtout, on refuse le SVG : il est
    resservi sur l'origine de l'application, ou le code animateur est range
    dans le navigateur, et un SVG peut porter du script."""
    t = (annonce or "").split(";")[0].strip().lower()
    if t in IMAGES_OK:
        return t
    ext = os.path.splitext(nom or "")[1].lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/png")


# Pack livre avec l'application : lu une fois au demarrage, partage par toutes
# les parties. Contrairement aux images deposees en cours de soiree, il survit
# aux redemarrages et ne depend pas de l'animateur qui anime.
PACK_FIXE = []


def charge_pack_fixe():
    """Lit le dossier avatars/. Un fichier ajoute au depot devient une figure
    proposee a tous, sans que personne n'ait rien a redeposer."""
    del PACK_FIXE[:]
    if not os.path.isdir(AVATARS_DIR):
        return
    exts = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}
    for nom in sorted(os.listdir(AVATARS_DIR)):
        ext = os.path.splitext(nom)[1].lower()
        if ext not in exts:
            continue
        chemin = os.path.join(AVATARS_DIR, nom)
        try:
            with open(chemin, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if not data:
            continue
        if len(data) > AVATAR_MAX:
            # Sans ce mot, l'animateur cherche longtemps pourquoi son image
            # n'apparait pas : elle est simplement trop lourde.
            say("Avatar ignore : %s fait %.0f Ko, la limite est de %d Ko"
                % (nom, len(data) / 1024.0, AVATAR_MAX // 1024))
            continue
        PACK_FIXE.append({"id": "f_" + os.path.splitext(nom)[0][:40],
                          "name": os.path.splitext(nom)[0][:40],
                          "type": exts[ext], "data": data, "fixe": True})
    if PACK_FIXE:
        poids = sum(len(a["data"]) for a in PACK_FIXE) / 1024.0
        say("Pack d'avatars du depot : %d images (%.0f Ko)" % (len(PACK_FIXE), poids))


# ------------------------------------------------------------------- salons
class Salon:
    """Tout ce qui concerne une partie. Volontairement en memoire seulement."""

    def __init__(self, code):
        self.code = code
        self.version = 0
        self.cree = time.time()
        self.vu = time.time()
        self.joueurs = {}        # pid -> {"name", "vu", "avatar"}
        self.avatars = []        # pack de l'animateur : [{"id","name","type","data"}]
        self.avatar_seq = 0
        self.question = None     # version expurgee, sans la reponse
        self.reponses = {}       # qid -> {pid: {"value", "at", "order"}}
        self.media = None        # {"id", "kind", "name", "type", "data"}
        self.blur = 0            # flou courant, en pixels, pilote par l'animateur
        self.media_url = ""      # lien externe (YouTube, mp3 distant...)
        self.media_url_kind = ""
        self.revele = None       # {"answer", "note", "media_id"}
        self.scores = []         # [{"name", "score"}]
        self.ouverte = False
        self.debut = 0           # horodatage d'affichage, pour caler les videos
        self.media_debut = 0     # 0 = le media attend que l'animateur le lance
        self.fin = 0             # horodatage de fin du chrono, 0 si sans chrono
        self.duree = 0           # duree totale, pour la barre de progression
        self.gel = None          # secondes restantes quand l'animateur met en pause
        self.buzz = []
        self.buzz_gagnant = None
        self.buzz_passes = []
        self.buzz_seq = 0        # incremente a chaque buzz : declenche le son
        self.q_cachee = False    # la question est masquee tant que l'animateur parle
        self.chat = []           # [{"id", "name", "role", "text", "at"}]
        self.chat_seq = 0
        # Rembobinage et relecture : les joueurs suivent la position de l'animateur.
        self.media_cmd = {"seq": 0, "pos": 0.0, "playing": True, "at": 0.0}
        # Media de la question suivante, envoye pendant que celle-ci se joue :
        # sinon l'animateur reste plusieurs secondes devant un ecran vide.
        self.pre_media = None
        self.media_offset = 0.0  # « demarrer a 30 s » : le point choisi en fiche
        self.classement = False  # l'animateur montre le classement a la salle
        self.brouillons = {}     # qid -> {pid: texte tape sans valider}

    def poids_media(self):
        """Octets de media retenus par cette partie, et eux seuls : ce sont les
        seuls que le menage sait relacher."""
        n = 0
        if self.media:
            n += len(self.media["data"])
        if self.pre_media:
            n += len(self.pre_media["data"])
        return n

    def tout_le_pack(self):
        """Les images du depot d'abord, puis celles deposees ce soir."""
        return PACK_FIXE + self.avatars

    def poids_avatars(self):
        return sum(len(a["data"]) for a in self.avatars)

    def poids(self):
        """Tout ce que cette partie retient en memoire."""
        return self.poids_media() + self.poids_avatars()

    def avatar_de(self, pid):
        j = self.joueurs.get(pid)
        if not j:
            return ""
        return j.get("avatar") or avatar_defaut(j.get("name") or "")

    def touch(self):
        self.version += 1
        self.vu = time.time()

    # -- vue de l'animateur -------------------------------------------------
    def vue_animateur(self):
        qid = self.question["id"] if self.question else None
        lignes = []
        for pid, r in (self.reponses.get(qid) or {}).items():
            j = self.joueurs.get(pid) or {}
            lignes.append({"userId": pid, "name": j.get("name", "?"),
                           "teamId": r.get("team_id"), "value": r["value"],
                           "at": r["at"], "order": r["order"]})
        lignes.sort(key=lambda r: r["order"])
        return {
            "v": self.version,
            "salon": {"code": self.code, "web": True},
            "players": [{"userId": p, "name": j["name"], "teamId": j.get("team_id"),
                         "avatar": self.avatar_de(p)}
                        for p, j in self.joueurs.items()],
            "question": ({"id": self.question["id"], "open": self.ouverte}
                         if self.question else None),
            "answers": lignes,
            "buzz": [{"userId": p, "name": self.joueurs.get(p, {}).get("name", "?")}
                     for p in self.buzz],
            "buzzOpen": False,
            "buzzMode": bool(self.question and self.question.get("buzzMode")),
            "buzzWinner": (None if self.buzz_gagnant is None else
                           {"userId": self.buzz_gagnant,
                            "name": self.joueurs.get(self.buzz_gagnant, {}).get("name", "?")}),
            "pack": [{"id": a["id"], "name": a["name"], "fixe": bool(a.get("fixe"))}
                     for a in self.tout_le_pack()],
            "questionHidden": self.q_cachee,
            "classement": self.classement,
            "drafts": [{"userId": p, "name": (self.joueurs.get(p) or {}).get("name", "?"),
                        "value": v}
                       for p, v in (self.brouillons.get(qid) or {}).items()
                       if p not in (self.reponses.get(qid) or {})],
            "chat": self.chat[-60:],
        }

    # -- vue d'un joueur ----------------------------------------------------
    def vue_joueur(self, pid):
        qid = self.question["id"] if self.question else None
        mienne = (self.reponses.get(qid) or {}).get(pid)
        peut_repondre = self.ouverte and not (
            self.question and self.question.get("buzzMode")
            and self.buzz_gagnant not in (None, pid))
        return {
            "v": self.version,
            "code": self.code,
            "me": (self.joueurs.get(pid) or {}).get("name"),
            "players": sorted(j["name"] for j in self.joueurs.values()),
            "avatars": {j.get("name", "?"): (j.get("avatar") or avatar_defaut(j.get("name") or ""))
                        for j in self.joueurs.values()},
            "pack": [{"id": a["id"], "name": a["name"], "fixe": bool(a.get("fixe"))}
                     for a in self.tout_le_pack()],
            "monAvatar": self.avatar_de(pid),
            "question": self.question_pour_joueur(),
            "questionHidden": self.q_cachee,
            "open": self.ouverte,
            "canAnswer": bool(peut_repondre),
            "media": ({"id": self.media["id"], "kind": self.media["kind"],
                       "name": self.media["name"]} if self.media else None),
            "blur": self.blur,
            "mediaUrl": self.media_url,
            "mediaUrlKind": self.media_url_kind,
            "startedAt": self.debut,
            "mediaStart": self.media_debut,
            "mediaOffset": self.media_offset,
            "showScores": self.classement,
            "deadline": self.fin,
            "duree": self.duree,
            "gel": self.gel,
            "now": time.time(),
            "mine": (mienne or {}).get("value"),
            "reveal": self.revele,
            "scores": self.scores,
            "answered": len(self.reponses.get(qid) or {}),
            "buzzWinner": (self.joueurs.get(self.buzz_gagnant, {}).get("name")
                           if self.buzz_gagnant else None),
            # Un enonce retenu derriere un media : on ne buzze pas avant de
            # savoir ce qui est demande. Une question purement orale, si.
            "canBuzz": bool(self.question and self.question.get("buzzMode")
                            and self.ouverte and self.buzz_gagnant is None
                            and pid not in self.buzz_passes
                            and not (self.q_cachee and (self.media or self.media_url))),
            "buzzSeq": self.buzz_seq,
            "mediaCmd": self.media_cmd,
            "chat": self.chat[-60:],
            "toutes": self.reponses_publiques(),
        }

    def reponses_publiques(self):
        """Ce que tout le monde a repondu. Uniquement apres la revelation :
        avant, une reponse visible par les autres fausserait la question."""
        if not self.revele or not self.question:
            return []
        qid = self.question["id"]
        seau = self.reponses.get(qid) or {}
        lignes = []
        for pid, r in seau.items():
            j = self.joueurs.get(pid) or {}
            lignes.append({"name": j.get("name", "?"),
                           "value": r.get("value", ""),
                           "order": r.get("order", 0), "brouillon": False})
        # Ce qui a ete tape sans etre valide compte quand meme : faute de temps,
        # une bonne reponse ne doit pas disparaitre parce qu'on n'a pas appuye.
        for pid, v in (self.brouillons.get(qid) or {}).items():
            if pid in seau or not (v or "").strip():
                continue
            j = self.joueurs.get(pid) or {}
            lignes.append({"name": j.get("name", "?"), "value": v,
                           "order": 9999, "brouillon": True})
        lignes.sort(key=lambda x: x["order"])
        return lignes

    def question_pour_joueur(self):
        """Question masquee : l'enonce ne doit pas atteindre le navigateur du
        joueur, sinon il suffit d'ouvrir les outils de developpement pour le
        lire. On ne garde que de quoi afficher le cadre."""
        if not self.question or not self.q_cachee:
            return self.question
        q = dict(self.question)
        q["text"] = ""
        q["choices"] = []
        return q


SALONS = {}


def nouveau_code():
    for _ in range(50):
        c = "".join(random.choice(LETTRES) for _ in range(5))
        if c not in SALONS:
            return c
    return "".join(random.choice(LETTRES) for _ in range(7))


def menage():
    """Oublie les parties abandonnees, pour ne pas garder de media en memoire."""
    limite = time.time() - INACTIF
    for c in [c for c, s in SALONS.items() if s.vu < limite]:
        SALONS.pop(c, None)
        say("Salon %s expire" % c)
    # Au-dela du plafond, on relache les medias des parties les plus anciennes
    # plutot que de risquer l'arret brutal du serveur.
    total = sum(s.poids() for s in SALONS.values())
    if total > MEMOIRE_MAX:
        anciens = sorted(SALONS.items(), key=lambda kv: kv[1].vu)
        # D'abord les medias, qui se rechargent tout seuls a la question
        # suivante. Les avatars ne partent qu'en dernier recours : les joueurs
        # les perdraient pour de bon au milieu de la partie.
        for c, s in anciens:
            if total <= MEMOIRE_MAX:
                break
            liberes = s.poids_media()
            if not liberes:
                continue
            s.media = None
            s.pre_media = None
            s.media_debut = 0
            total -= liberes
            say("Salon %s - medias liberes (memoire)" % c)
        for c, s in anciens:
            if total <= MEMOIRE_MAX:
                break
            liberes = s.poids_avatars()
            if not liberes:
                continue
            s.avatars = []
            for j in s.joueurs.values():
                if str(j.get("avatar", "")).startswith("c:"):
                    j["avatar"] = avatar_defaut(j.get("name") or "")
            s.touch()
            total -= liberes
            say("Salon %s - avatars liberes (memoire)" % c)

    while len(SALONS) > SALON_MAX:
        vieux = min(SALONS.values(), key=lambda s: s.vu)
        SALONS.pop(vieux.code, None)


def salon_de(request, creer=False):
    code = (request.query.get("room") or "").strip().upper()
    if not code:
        return None
    s = SALONS.get(code)
    if s is None and creer:
        menage()
        s = Salon(code)
        SALONS[code] = s
    return s


def anim_ok(request):
    """Vrai si la requete vient d'un animateur autorise."""
    if not CLE_ANIMATEUR:
        return True
    fournie = (request.headers.get("X-Regie-Cle") or request.query.get("cle") or "").strip()
    return fournie == CLE_ANIMATEUR


def refus():
    return web.json_response({"ok": False, "error": "cle"}, status=401,
                             headers={"Cache-Control": "no-store"})


def rep(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------- animateur
async def h_ping(request):
    return rep({"ok": True, "web": True, "version": VERSION, "cle": bool(CLE_ANIMATEUR),
                "online": EN_LIGNE, "rooms": len(SALONS)})


async def h_ouvrir(request):
    if not anim_ok(request):
        return refus()
    """La console reclame un salon. Elle peut proposer son ancien code pour le
    reprendre apres un rechargement de page."""
    menage()
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    voulu = (body.get("code") or "").strip().upper()
    if voulu and voulu in SALONS:
        s = SALONS[voulu]
    else:
        code = voulu if (voulu and len(voulu) == 5) else nouveau_code()
        s = SALONS.get(code) or Salon(code)
        SALONS[code] = s
        say("Salon ouvert : %s" % code)
    s.touch()
    return rep({"ok": True, "code": s.code})


async def lire_corps(request):
    """JSON simple, ou multipart quand un media accompagne la question."""
    if (request.content_type or "").startswith("multipart/"):
        data = await request.post()
        body = json.loads(data["payload"])
        champ = data.get("media")
        if champ is not None and hasattr(champ, "file"):
            champ.file.seek(0)
            octets = champ.file.read()
            return body, {"data": octets, "name": champ.filename,
                          "type": getattr(champ, "content_type", "") or "",
                          "size": len(octets)}
        return body, None
    return await request.json(), None


async def h_premedia(request):
    """Deposer a l'avance le media de la question suivante. L'animateur
    l'envoie pendant que la question en cours se joue, ce qui supprime
    l'attente au moment de l'afficher."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body, media = await lire_corps(request)
    qid = (body.get("qid") or "").strip()
    if not qid or not media:
        return rep({"ok": False, "error": "rien a deposer"}, status=400)
    if media["size"] > MEDIA_MAX:
        return rep({"ok": False, "error": "trop lourd"}, status=413)
    s.pre_media = {"qid": qid, "kind": body.get("mediaKind") or "",
                   "name": media["name"], "type": media["type"], "data": media["data"]}
    s.vu = time.time()
    menage()          # la memoire vient de grossir : c'est ici qu'il faut veiller
    say("Salon %s - media de la question suivante recu (%.1f Mo)" % (
        s.code, media["size"] / 1048576.0))
    return rep({"ok": True})


async def h_question(request):
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False, "error": "salon inconnu"}, status=404)
    body, media = await lire_corps(request)

    choix = []
    for i, c in enumerate(body.get("choices") or []):
        t = (c.get("text") or "").strip()
        if t:
            choix.append({"letter": string.ascii_uppercase[i], "text": t})

    nouvelle = (s.question is None) or (s.question.get("id") != body.get("id"))
    s.question = {
        "id": body["id"], "index": body.get("index", 1), "total": body.get("total", 1),
        "round": body.get("round") or "", "text": body.get("text") or "",
        "type": body.get("type") or "free", "multi": bool(body.get("multi")),
        "points": body.get("points"), "timer": body.get("timer"),
        "choices": choix, "buzzMode": bool(body.get("buzzMode")),
    }
    s.reponses.setdefault(s.question["id"], {})
    s.q_cachee = bool(body.get("questionHidden"))
    s.classement = False
    try:
        s.media_offset = max(0.0, float(body.get("mediaOffset") or 0))
    except (TypeError, ValueError):
        s.media_offset = 0.0
    s.brouillons.setdefault(s.question["id"], {})
    s.media_cmd = {"seq": 0, "pos": 0.0, "playing": True, "at": 0.0}
    s.revele = None
    s.ouverte = True
    s.debut = time.time()
    tim = body.get("timer") or 0
    s.duree = float(tim or 0)
    s.gel = None
    s.fin = (s.debut + float(tim)) if tim else 0
    if nouvelle:
        s.buzz = []
        s.buzz_gagnant = None
        s.buzz_passes = []

    try:
        s.blur = max(0.0, min(60.0, float(body.get("blur") or 0)))
    except (TypeError, ValueError):
        s.blur = 0
    s.media_url = (body.get("mediaUrl") or "").strip()[:500]
    s.media_url_kind = body.get("mediaKind") or ""
    # Le media a peut-etre ete depose pendant la question precedente.
    if media is None and s.pre_media and s.pre_media["qid"] == s.question["id"]:
        media = {"data": s.pre_media["data"], "name": s.pre_media["name"],
                 "type": s.pre_media["type"], "size": len(s.pre_media["data"])}
        if not s.media_url_kind:
            s.media_url_kind = s.pre_media["kind"]
        body = dict(body, mediaKind=body.get("mediaKind") or s.pre_media["kind"])
    if s.pre_media and s.pre_media["qid"] == s.question["id"]:
        s.pre_media = None
    if media and media["size"] <= MEDIA_MAX:
        s.media = {"id": "m_%d" % int(s.debut * 1000), "kind": body.get("mediaKind") or "",
                   "name": media["name"], "type": media["type"], "data": media["data"]}
        # « mediaHold » : tout le monde attend que l'animateur appuie sur lecture,
        # pour que l'extrait parte au meme instant chez chacun.
        s.media_debut = 0 if body.get("mediaHold") else s.debut
        say("Salon %s - question %s/%s avec media %s (%.1f Mo)" % (
            s.code, s.question["index"], s.question["total"],
            media["name"], media["size"] / 1048576.0))
    else:
        s.media = None
        # Un lien externe obeit a la meme regle qu'un fichier importe : il part
        # tout de suite, sauf si l'animateur a demande a le lancer lui-meme.
        s.media_debut = 0 if (body.get("mediaHold") or not s.media_url) else s.debut
        if media:
            say("Salon %s - media refuse : %.1f Mo" % (s.code, media["size"] / 1048576.0))
        else:
            say("Salon %s - question %s/%s" % (s.code, s.question["index"], s.question["total"]))
    s.touch()
    menage()          # un media vient d'etre retenu : on verifie le plafond
    return rep({"ok": True})


async def h_stop(request):
    if not anim_ok(request):
        return refus()
    """Fin de partie : les joueurs retournent en salle d'attente. On garde les
    joueurs connectes et les scores, on efface seulement la question en cours."""
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    s.question = None
    s.media = None
    s.media_url = ""
    s.media_url_kind = ""
    s.media_debut = 0
    s.revele = None
    s.ouverte = False
    s.debut = 0
    s.fin = 0
    s.buzz = []
    s.buzz_gagnant = None
    s.buzz_passes = []
    s.touch()
    say("Salon %s - partie arretee" % s.code)
    return rep({"ok": True})


async def h_chrono(request):
    """L'animateur met en pause ou rallonge : les joueurs doivent suivre,
    sinon leur decompte continue de filer alors que le temps est arrete."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    try:
        reste = max(0.0, float(body.get("left") or 0))
    except (TypeError, ValueError):
        reste = 0.0
    if body.get("running"):
        s.gel = None
        s.fin = time.time() + reste
    else:
        s.gel = reste
    if reste > s.duree:
        s.duree = reste          # une rallonge agrandit aussi la barre
    # Rendre du temps n'a de sens que si les joueurs peuvent encore repondre :
    # sinon ils voient un decompte tourner sous « les reponses sont closes ».
    if reste > 0 and s.revele is None:
        s.ouverte = True
    s.touch()
    return rep({"ok": True})


async def h_blur(request):
    """L'animateur fait varier le flou : les joueurs suivent en direct."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    try:
        s.blur = max(0.0, min(60.0, float(body.get("value") or 0)))
    except (TypeError, ValueError):
        s.blur = 0
    s.touch()
    return rep({"ok": True})


async def h_mediastart(request):
    if not anim_ok(request):
        return refus()
    """L'animateur lance le media : tous les joueurs demarrent a cet instant."""
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    s.media_debut = time.time()
    s.touch()
    say("Salon %s - media lance" % s.code)
    return rep({"ok": True})


async def h_close(request):
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    s.ouverte = False
    s.touch()
    return rep({"ok": True})


async def h_reveal(request):
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body, media = await lire_corps(request)
    s.ouverte = False
    s.revele = {"answer": body.get("answer") or "", "note": body.get("note") or ""}
    lien = (body.get("mediaUrl") or "").strip()[:500]
    if media and media["size"] <= MEDIA_MAX:
        s.media = {"id": "m_%d" % int(time.time() * 1000), "kind": body.get("mediaKind") or "",
                   "name": media["name"], "type": media["type"], "data": media["data"]}
        s.media_url = ""
        s.media_url_kind = ""
        s.debut = time.time()
        s.media_debut = time.time()
    elif lien:
        # Un media de revelation donne par lien doit repartir comme celui de la
        # question : sans ca il n'atteint jamais les joueurs, et sans un mot.
        s.media = None
        s.media_url = lien
        s.media_url_kind = body.get("mediaKind") or ""
        s.debut = time.time()
        s.media_debut = time.time()
    s.touch()
    return rep({"ok": True})


async def h_unreveal(request):
    """L'animateur remasque la reponse : les joueurs doivent la perdre aussi,
    sinon elle leur reste a l'ecran et ils ne peuvent plus rien saisir."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    s.revele = None
    s.ouverte = True
    s.touch()
    return rep({"ok": True})


async def h_scores(request):
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    s.scores = [{"name": r.get("name", "?"), "score": r.get("score", 0)}
                for r in (body.get("rows") or [])][:60]
    s.touch()
    return rep({"ok": True})


async def h_teams(request):
    if not anim_ok(request):
        return refus()
    """La console renvoie les scores : on les garde pour les afficher aux joueurs."""
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    equipes = body.get("teams") or []
    for e in body.get("assign") or []:
        pid = e.get("userId")
        if pid in s.joueurs:
            s.joueurs[pid]["team_id"] = e.get("teamId")
    s.scores = [{"name": t.get("name", "?"), "score": t.get("score", 0)} for t in equipes][:60]
    for pid, j in s.joueurs.items():
        for t in equipes:
            if t.get("id") == j.get("team_id"):
                j["score"] = t.get("score")
    s.touch()
    return rep({"ok": True})


async def h_kick(request):
    """Expulser quelqu'un. Utile quand un joueur plante et revient : sans ca
    son ancienne place garde son pseudo, et il revient en « Machin 2 »."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    pid = (body.get("playerId") or "").strip()
    j = s.joueurs.pop(pid, None)
    if j is None:
        return rep({"ok": False, "error": "joueur inconnu"}, status=404)
    for seau in s.reponses.values():
        seau.pop(pid, None)
    s.buzz = [p for p in s.buzz if p != pid]
    s.buzz_passes = [p for p in s.buzz_passes if p != pid]
    if s.buzz_gagnant == pid:
        s.buzz_gagnant = None
    s.touch()
    say("Salon %s - %s expulse (%d joueurs)" % (s.code, j.get("name", "?"), len(s.joueurs)))
    return rep({"ok": True})


async def h_buzzreset(request):
    """Tout le monde peut buzzer de nouveau, y compris ceux qui ont deja eu
    leur tour. Pratique quand personne ne trouve."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    s.buzz = []
    s.buzz_gagnant = None
    s.buzz_passes = []
    s.touch()
    return rep({"ok": True})


async def h_showquestion(request):
    """Afficher ou masquer l'enonce chez les joueurs, sans toucher au media."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    s.q_cachee = bool(body.get("hidden"))
    s.touch()
    return rep({"ok": True})


async def h_mediacmd(request):
    """L'animateur rembobine, avance ou relance : les joueurs se recalent."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    try:
        pos = max(0.0, float(body.get("pos") or 0))
    except (TypeError, ValueError):
        pos = 0.0
    joue = bool(body.get("playing"))
    maintenant = time.time()
    s.media_cmd = {"seq": s.media_cmd["seq"] + 1, "pos": pos,
                   "playing": joue, "at": maintenant}
    # On garde la position meme en pause : media_debut a zero veut dire « pas
    # encore lance », et l'extrait disparaitrait de l'ecran des joueurs.
    s.media_debut = maintenant - pos
    s.touch()
    return rep({"ok": True})


async def h_chat(request):
    """Un message dans le fil. Les joueurs s'identifient par leur playerId ;
    sans playerId, c'est l'animateur, et il faut alors son code."""
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    texte = (body.get("text") or "").strip()[:300]
    if not texte:
        return rep({"ok": False, "error": "message vide"}, status=400)
    pid = (body.get("playerId") or "").strip()
    if pid:
        j = s.joueurs.get(pid)
        if j is None:
            return rep({"ok": False, "error": "joueur inconnu"}, status=404)
        nom, role = j.get("name", "?"), "joueur"
    else:
        if not anim_ok(request):
            return refus()
        nom, role = (body.get("name") or "Animateur")[:24], "anim"
    s.chat_seq += 1
    s.chat.append({"id": s.chat_seq, "name": nom, "role": role,
                   "text": texte, "at": time.time()})
    del s.chat[:-200]
    s.touch()
    return rep({"ok": True})


async def h_draft(request):
    """Ce que le joueur est en train de taper. Garde de cote, jamais montre aux
    autres avant la revelation : sinon on lirait la reponse du voisin."""
    s = salon_de(request)
    if s is None or s.question is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    pid = (body.get("playerId") or "").strip()
    if pid not in s.joueurs:
        return rep({"ok": False}, status=404)
    seau = s.brouillons.setdefault(s.question["id"], {})
    seau[pid] = (body.get("value") or "")[:300]
    # Pas de touch() : un brouillon ne doit pas reveiller toute la salle a
    # chaque lettre tapee. L'animateur le verra au prochain tour de boucle.
    s.vu = time.time()
    return rep({"ok": True})


async def h_classement(request):
    """Montrer ou cacher le classement chez tout le monde."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    s.classement = bool(body.get("show"))
    s.touch()
    return rep({"ok": True})


async def h_pass(request):
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    if s.buzz_gagnant and s.buzz_gagnant not in s.buzz_passes:
        s.buzz_passes.append(s.buzz_gagnant)
    s.buzz_gagnant = None
    s.buzz = []
    s.touch()
    return rep({"ok": True})


async def h_buzz(request):
    if not anim_ok(request):
        return refus()
    """Compatibilite avec la console : ouvrir le buzzer libre."""
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    s.buzz = []
    s.buzz_gagnant = None
    s.buzz_passes = []
    s.touch()
    return rep({"ok": True})


async def h_state(request):
    if not anim_ok(request):
        return refus()
    """Interrogation longue de la console : on ne repond qu'en cas de changement."""
    s = salon_de(request)
    if s is None:
        return rep({"ok": False, "error": "salon inconnu"}, status=404)
    try:
        depuis = int(request.query.get("v", -1))
    except ValueError:
        depuis = -1
    limite = time.time() + 20
    while s.version == depuis and time.time() < limite:
        await asyncio.sleep(0.25)
    s.vu = time.time()
    return rep(s.vue_animateur())


# ------------------------------------------------------------------ joueurs
async def h_join(request):
    s = salon_de(request)
    if s is None:
        return rep({"ok": False, "error": "Ce code ne correspond à aucune partie."}, status=404)
    body = await request.json()
    nom = (body.get("name") or "").strip()[:24]
    if not nom:
        return rep({"ok": False, "error": "Il faut un pseudo."}, status=400)
    pid = (body.get("playerId") or "").strip()[:40]
    if not pid or pid not in s.joueurs:
        pid = "p_%s%d" % ("".join(random.choice(string.ascii_lowercase) for _ in range(6)),
                          int(time.time() * 1000) % 100000)
    deja = {j["name"].lower() for p, j in s.joueurs.items() if p != pid}
    if nom.lower() in deja:
        base, n = nom, 2
        while nom.lower() in deja:
            nom = "%s %d" % (base, n)
            n += 1
    ancien = s.joueurs.get(pid) or {}
    voulu = body.get("avatar")
    if voulu is None:
        voulu = ancien.get("avatar") or avatar_defaut(nom)
    s.joueurs[pid] = dict(ancien, name=nom, vu=time.time(),
                          avatar=avatar_valide(s, voulu, nom))
    s.touch()
    say("Salon %s - %s rejoint (%d joueurs)" % (s.code, nom, len(s.joueurs)))
    return rep({"ok": True, "playerId": pid, "name": nom, "code": s.code,
                "avatar": s.joueurs[pid]["avatar"]})


async def h_play(request):
    s = salon_de(request)
    if s is None:
        return rep({"ok": False, "error": "salon inconnu"}, status=404)
    pid = request.query.get("p") or ""
    if pid in s.joueurs:
        s.joueurs[pid]["vu"] = time.time()
    try:
        depuis = int(request.query.get("v", -1))
    except ValueError:
        depuis = -1
    limite = time.time() + 20
    while s.version == depuis and time.time() < limite:
        await asyncio.sleep(0.25)
    return rep(s.vue_joueur(pid))


async def h_answer(request):
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    pid = body.get("playerId") or ""
    if pid not in s.joueurs:
        return rep({"ok": False, "error": "Tu n'es plus dans la partie."}, status=403)
    if not s.ouverte or not s.question:
        return rep({"ok": False, "error": "Les réponses sont closes."}, status=409)
    if s.question.get("buzzMode") and s.buzz_gagnant != pid:
        return rep({"ok": False, "error": "Ce n'est pas ton tour."}, status=409)

    qid = s.question["id"]
    seau = s.reponses.setdefault(qid, {})
    avant = seau.get(pid)
    seau[pid] = {"value": str(body.get("value") or "")[:300],
                 "at": int(time.time() * 1000),
                 "order": avant["order"] if avant else len(seau) + 1,
                 "team_id": (s.joueurs.get(pid) or {}).get("team_id")}
    s.touch()
    return rep({"ok": True})


async def h_buzz_joueur(request):
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    pid = body.get("playerId") or ""
    if pid not in s.joueurs or not s.ouverte or not s.question:
        return rep({"ok": False}, status=409)
    if not s.question.get("buzzMode"):
        return rep({"ok": False}, status=409)
    if s.buzz_gagnant is not None:
        return rep({"ok": False, "error": "Quelqu'un a déjà la main."}, status=409)
    if pid in s.buzz_passes:
        return rep({"ok": False, "error": "Tu as déjà eu ta chance."}, status=409)
    s.buzz_gagnant = pid
    s.buzz = [pid]
    s.buzz_seq += 1
    s.touch()
    return rep({"ok": True})


async def h_setavatar(request):
    """Le joueur change de figure. Aucun code d'animateur ici : c'est son
    choix a lui. Il faut seulement qu'il soit bien dans la partie."""
    s = salon_de(request)
    if s is None:
        return rep({"ok": False, "error": "salon inconnu"}, status=404)
    body = await request.json()
    pid = (body.get("playerId") or "").strip()
    j = s.joueurs.get(pid)
    if j is None:
        return rep({"ok": False, "error": "Tu n'es plus dans la partie."}, status=403)
    j["avatar"] = avatar_valide(s, body.get("avatar"), j.get("name") or "")
    j["vu"] = time.time()
    s.touch()
    return rep({"ok": True, "avatar": j["avatar"]})


async def h_avatarpack(request):
    """L'animateur ajoute une image au pack du salon."""
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False, "error": "salon inconnu"}, status=404)
    try:
        body, media = await lire_corps(request)
    except Exception:
        return rep({"ok": False, "error": "envoi illisible"}, status=400)
    if not media or not media["size"]:
        return rep({"ok": False, "error": "aucune image"}, status=400)
    if media["size"] > AVATAR_MAX:
        return rep({"ok": False, "error": "image trop lourde"}, status=413)
    if len(s.avatars) >= AVATAR_PACK_NB:
        return rep({"ok": False, "error": "pack complet"}, status=409)
    if s.poids_avatars() + media["size"] > AVATAR_PACK_MAX:
        return rep({"ok": False, "error": "pack trop lourd"}, status=413)
    s.avatar_seq += 1
    ident = "a%d_%d" % (s.avatar_seq, int(time.time() * 1000) % 100000)
    nom = (str(body.get("name") or media["name"] or "image")).strip()[:40] or "image"
    s.avatars.append({"id": ident, "name": nom,
                      "type": type_image(media["type"], media["name"]),
                      "data": media["data"]})
    s.touch()
    menage()          # la memoire vient de grossir : on verifie le plafond
    say("Salon %s - avatar « %s » ajoute au pack (%d images)" % (
        s.code, nom, len(s.avatars)))
    return rep({"ok": True, "id": ident})


async def h_avatarpackdel(request):
    if not anim_ok(request):
        return refus()
    s = salon_de(request)
    if s is None:
        return rep({"ok": False, "error": "salon inconnu"}, status=404)
    body = await request.json()
    # Une image du depot n'est pas retirable depuis l'interface : elle
    # appartient a l'application, on l'enleve en modifiant le depot.
    if str(body.get("id") or "").startswith("f_"):
        return rep({"ok": False, "error": "image livree avec l'application"}, status=403)
    ident = (body.get("id") or "").strip()
    reste = [a for a in s.avatars if a["id"] != ident]
    if len(reste) == len(s.avatars):
        return rep({"ok": False, "error": "image inconnue"}, status=404)
    s.avatars = reste
    # Ceux qui l'avaient choisie garderaient sinon un cadre vide toute la soiree.
    perdu = "c:" + ident
    for j in s.joueurs.values():
        if j.get("avatar") == perdu:
            j["avatar"] = avatar_defaut(j.get("name") or "")
    s.touch()
    say("Salon %s - avatar retire du pack (%d images)" % (s.code, len(s.avatars)))
    return rep({"ok": True})


async def h_avatarimg(request):
    s = salon_de(request)
    if s is None:
        return web.Response(status=404, text="salon inconnu")
    ident = request.query.get("id") or ""
    for a in s.tout_le_pack():
        if a["id"] == ident:
            # Une image du pack ne change jamais : le navigateur peut la garder.
            return web.Response(body=a["data"], content_type=a["type"],
                                headers={"Cache-Control": "public, max-age=31536000, immutable",
                                         "X-Content-Type-Options": "nosniff"})
    return web.Response(status=404, text="avatar inconnu")


async def h_media(request):
    s = salon_de(request)
    if s is None or s.media is None:
        return web.Response(status=404, text="pas de media")
    if request.query.get("id") and request.query["id"] != s.media["id"]:
        return web.Response(status=404, text="media remplace")
    return web.Response(body=s.media["data"],
                        content_type=(s.media["type"] or "application/octet-stream"),
                        headers={"Cache-Control": "public, max-age=3600"})


# ------------------------------------------------------------------ pages
def page(chemin, defaut_msg):
    async def handler(request):
        if not os.path.exists(chemin):
            return web.Response(text=defaut_msg, status=500)
        return web.FileResponse(chemin, headers={"Cache-Control": "no-store"})
    return handler


async def h_racine(request):
    return await page(CONSOLE, "regie-quiz.html est introuvable.")(request)


async def h_jouer(request):
    return await page(JOUEUR, "joueur.html est introuvable.")(request)


def build_app():
    charge_pack_fixe()
    app = web.Application(client_max_size=64 * 1024 * 1024)
    r = app.router
    r.add_get("/", h_racine)
    r.add_get("/regie-quiz.html", h_racine)
    r.add_get("/jouer", h_jouer)
    r.add_get("/joueur.html", h_jouer)

    r.add_get("/api/ping", h_ping)
    r.add_post("/api/room", h_ouvrir)
    r.add_get("/api/state", h_state)
    r.add_post("/api/question", h_question)
    r.add_post("/api/premedia", h_premedia)
    r.add_post("/api/close", h_close)
    r.add_post("/api/mediastart", h_mediastart)
    r.add_post("/api/blur", h_blur)
    r.add_post("/api/chrono", h_chrono)
    r.add_post("/api/stop", h_stop)
    r.add_post("/api/reveal", h_reveal)
    r.add_post("/api/unreveal", h_unreveal)
    r.add_post("/api/scores", h_scores)
    r.add_post("/api/teams", h_teams)
    r.add_post("/api/pass", h_pass)
    r.add_post("/api/kick", h_kick)
    r.add_post("/api/buzzreset", h_buzzreset)
    r.add_post("/api/showquestion", h_showquestion)
    r.add_post("/api/mediacmd", h_mediacmd)
    r.add_post("/api/chat", h_chat)
    r.add_post("/api/draft", h_draft)
    r.add_post("/api/classement", h_classement)
    r.add_post("/api/buzz", h_buzz)
    r.add_post("/api/avatarpack", h_avatarpack)
    r.add_post("/api/avatarpackdel", h_avatarpackdel)

    r.add_post("/api/join", h_join)
    r.add_get("/api/play", h_play)
    r.add_post("/api/answer", h_answer)
    r.add_post("/api/playerbuzz", h_buzz_joueur)
    r.add_post("/api/setavatar", h_setavatar)
    r.add_get("/api/media", h_media)
    r.add_get("/api/avatarimg", h_avatarimg)
    return app


async def main():
    runner = web.AppRunner(build_app())
    await runner.setup()
    try:
        await web.TCPSite(runner, HOTE, PORT).start()
    except OSError as exc:
        say("Impossible d'ecouter sur %s:%d (%s)" % (HOTE, PORT, exc))
        say("Une autre fenetre Regie Quiz tourne peut-etre deja.")
        return

    print()
    say("Regie Quiz %s" % VERSION)
    if EN_LIGNE:
        say("En ligne, port %d. La console est a l'adresse du site," % PORT)
        say("et les joueurs vont sur cette meme adresse suivie de /jouer")
    else:
        say("Console animateur : http://127.0.0.1:%d" % PORT)
        say("Page joueur      : http://127.0.0.1:%d/jouer" % PORT)
        say("Laisse cette fenetre ouverte pendant la soiree. Ctrl+C pour arreter.")
        try:
            webbrowser.open("http://127.0.0.1:%d/" % PORT)
        except Exception:
            pass
    print()
    while True:
        await asyncio.sleep(3600)
        menage()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        say("Arret. A la prochaine.")
