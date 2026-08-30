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
CONSOLE = os.path.join(HERE, "regie-quiz.html")
JOUEUR = os.path.join(HERE, "joueur.html")

PORT = int(os.environ.get("PORT", "8777"))
HOTE = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
EN_LIGNE = bool(os.environ.get("PORT"))          # vrai chez un hebergeur
VERSION = "2026-08-30 web-1"

MEDIA_MAX = 48 * 1024 * 1024     # au-dela, on refuse l'envoi du media
SALON_MAX = 40                   # parties simultanees
INACTIF = 6 * 3600               # une partie oubliee expire au bout de 6 h
LETTRES = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # sans I ni O, illisibles a l'oral


def say(*parts):
    print("  " + " ".join(str(p) for p in parts), flush=True)


# ------------------------------------------------------------------- salons
class Salon:
    """Tout ce qui concerne une partie. Volontairement en memoire seulement."""

    def __init__(self, code):
        self.code = code
        self.version = 0
        self.cree = time.time()
        self.vu = time.time()
        self.joueurs = {}        # pid -> {"name", "vu"}
        self.question = None     # version expurgee, sans la reponse
        self.reponses = {}       # qid -> {pid: {"value", "at", "order"}}
        self.media = None        # {"id", "kind", "name", "type", "data"}
        self.revele = None       # {"answer", "note", "media_id"}
        self.scores = []         # [{"name", "score"}]
        self.ouverte = False
        self.debut = 0           # horodatage d'affichage, pour caler les videos
        self.media_debut = 0     # 0 = le media attend que l'animateur le lance
        self.fin = 0             # horodatage de fin du chrono, 0 si sans chrono
        self.buzz = []
        self.buzz_gagnant = None
        self.buzz_passes = []

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
            "discord": {"ready": True, "guild": "Salon " + self.code,
                        "channel": self.code, "bound": True, "demo": False,
                        "web": True, "code": self.code},
            "players": [{"userId": p, "name": j["name"], "teamId": j.get("team_id")}
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
            "question": self.question,
            "open": self.ouverte,
            "canAnswer": bool(peut_repondre),
            "media": ({"id": self.media["id"], "kind": self.media["kind"],
                       "name": self.media["name"]} if self.media else None),
            "startedAt": self.debut,
            "mediaStart": self.media_debut,
            "deadline": self.fin,
            "now": time.time(),
            "mine": (mienne or {}).get("value"),
            "reveal": self.revele,
            "scores": self.scores,
            "answered": len(self.reponses.get(qid) or {}),
            "buzzWinner": (self.joueurs.get(self.buzz_gagnant, {}).get("name")
                           if self.buzz_gagnant else None),
            "canBuzz": bool(self.question and self.question.get("buzzMode")
                            and self.ouverte and self.buzz_gagnant is None
                            and pid not in self.buzz_passes),
        }


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


def rep(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------- animateur
async def h_ping(request):
    return rep({"ok": True, "web": True, "version": VERSION,
                "online": EN_LIGNE, "rooms": len(SALONS)})


async def h_ouvrir(request):
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


async def h_question(request):
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
    s.revele = None
    s.ouverte = True
    s.debut = time.time()
    tim = body.get("timer") or 0
    s.fin = (s.debut + float(tim)) if tim else 0
    if nouvelle:
        s.buzz = []
        s.buzz_gagnant = None
        s.buzz_passes = []

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
        s.media_debut = 0
        if media:
            say("Salon %s - media refuse : %.1f Mo" % (s.code, media["size"] / 1048576.0))
        else:
            say("Salon %s - question %s/%s" % (s.code, s.question["index"], s.question["total"]))
    s.touch()
    return rep({"ok": True})


async def h_stop(request):
    """Fin de partie : les joueurs retournent en salle d'attente. On garde les
    joueurs connectes et les scores, on efface seulement la question en cours."""
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    s.question = None
    s.media = None
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


async def h_mediastart(request):
    """L'animateur lance le media : tous les joueurs demarrent a cet instant."""
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    s.media_debut = time.time()
    s.touch()
    say("Salon %s - media lance" % s.code)
    return rep({"ok": True})


async def h_close(request):
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    s.ouverte = False
    s.touch()
    return rep({"ok": True})


async def h_reveal(request):
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body, media = await lire_corps(request)
    s.ouverte = False
    s.revele = {"answer": body.get("answer") or "", "note": body.get("note") or ""}
    if media and media["size"] <= MEDIA_MAX:
        s.media = {"id": "m_%d" % int(time.time() * 1000), "kind": body.get("mediaKind") or "",
                   "name": media["name"], "type": media["type"], "data": media["data"]}
        s.debut = time.time()
    s.touch()
    return rep({"ok": True})


async def h_scores(request):
    s = salon_de(request)
    if s is None:
        return rep({"ok": False}, status=404)
    body = await request.json()
    s.scores = [{"name": r.get("name", "?"), "score": r.get("score", 0)}
                for r in (body.get("rows") or [])][:60]
    s.touch()
    return rep({"ok": True})


async def h_teams(request):
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


async def h_pass(request):
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
    s.joueurs[pid] = dict(s.joueurs.get(pid) or {}, name=nom, vu=time.time())
    s.touch()
    say("Salon %s - %s rejoint (%d joueurs)" % (s.code, nom, len(s.joueurs)))
    return rep({"ok": True, "playerId": pid, "name": nom, "code": s.code})


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
    s.touch()
    return rep({"ok": True})


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
    r.add_post("/api/close", h_close)
    r.add_post("/api/mediastart", h_mediastart)
    r.add_post("/api/stop", h_stop)
    r.add_post("/api/reveal", h_reveal)
    r.add_post("/api/scores", h_scores)
    r.add_post("/api/teams", h_teams)
    r.add_post("/api/pass", h_pass)
    r.add_post("/api/buzz", h_buzz)

    r.add_post("/api/join", h_join)
    r.add_get("/api/play", h_play)
    r.add_post("/api/answer", h_answer)
    r.add_post("/api/playerbuzz", h_buzz_joueur)
    r.add_get("/api/media", h_media)
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
