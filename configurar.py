#!/usr/bin/env python3
"""Configura Telegram: pide el token (oculto), averigua tu chat y guarda .env.

Antes: crea el bot con @BotFather (o revoca el token con /revoke) y pulsa "Iniciar" en tu bot.
Ejecuta:  python3 configurar.py

Conserva el resto de líneas de .env (por ejemplo tu ubicación), reutiliza tu chat si ya
estaba guardado y, si tienes la herramienta `gh`, actualiza también los secretos de GitHub.
"""
import getpass
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO = os.environ.get("CHOLLOS_REPO", "rubenRG33/chollos-bot")


def api(token, metodo, datos=None):
    url = "https://api.telegram.org/bot%s/%s" % (token, metodo)
    cuerpo = json.dumps(datos).encode() if datos is not None else None
    req = urllib.request.Request(url, data=cuerpo, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def leer_env():
    datos = {}
    ruta = BASE / ".env"
    if ruta.exists():
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            if "=" in linea and not linea.strip().startswith("#"):
                k, v = linea.split("=", 1)
                datos[k.strip()] = v.strip()
    return datos


def main():
    env = leer_env()
    token = os.environ.get("TELEGRAM_TOKEN") or getpass.getpass(
        "Pega el token de BotFather (no se verá al pegarlo) y pulsa Intro: ")
    token = token.strip()
    try:
        yo = api(token, "getMe")["result"]
    except (urllib.error.URLError, KeyError, ValueError):
        raise SystemExit("Ese token no funciona. Cópialo de nuevo desde @BotFather.")
    print("Bot encontrado: @%s" % yo["username"])

    chat_id = env.get("TELEGRAM_CHAT_ID")
    if chat_id:
        print("Reutilizo tu chat guardado.")
    else:
        mensajes = api(token, "getUpdates").get("result", [])
        chats = [m["message"]["chat"] for m in mensajes if "message" in m]
        if not chats:
            raise SystemExit("No veo ningún mensaje. Abre @%s en Telegram, pulsa Iniciar, "
                             "escríbele 'hola' y vuelve a ejecutar esto." % yo["username"])
        chat_id = str(chats[-1]["id"])
        print("Chat detectado: %s" % (chats[-1].get("first_name") or chats[-1].get("title") or chat_id))

    env["TELEGRAM_TOKEN"] = token
    env["TELEGRAM_CHAT_ID"] = chat_id
    ruta = BASE / ".env"
    ruta.write_text("".join("%s=%s\n" % (k, v) for k, v in env.items()), encoding="utf-8")
    os.chmod(ruta, 0o600)
    print("Guardado en .env (el resto de líneas se ha conservado).")

    if shutil.which("gh"):
        for clave in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
            r = subprocess.run(["gh", "secret", "set", clave, "--repo", REPO],
                               input=env[clave], text=True, capture_output=True)
            print("  GitHub %s: %s" % (clave, "actualizado" if r.returncode == 0 else "NO se pudo (%s)" % r.stderr.strip()[:80]))
    else:
        print("No encuentro `gh`: actualiza a mano el secreto TELEGRAM_TOKEN en GitHub.")

    api(token, "sendMessage", {"chat_id": chat_id,
                               "text": "✅ Bot de chollos conectado con el token nuevo."})
    print("Listo: mensaje de prueba enviado a tu Telegram.")


if __name__ == "__main__":
    main()
