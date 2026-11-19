#!/usr/bin/env python3
"""Configura Telegram: pide el token (oculto), averigua tu chat y guarda .env.

Antes: crea el bot con @BotFather y pulsa "Iniciar" en tu bot.
Ejecuta:  python3 configurar.py
"""
import getpass
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent


def api(token, metodo, datos=None):
    url = "https://api.telegram.org/bot%s/%s" % (token, metodo)
    cuerpo = json.dumps(datos).encode() if datos is not None else None
    req = urllib.request.Request(url, data=cuerpo, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def main():
    token = os.environ.get("TELEGRAM_TOKEN") or getpass.getpass(
        "Pega el token de BotFather (no se verá al pegarlo) y pulsa Intro: ")
    token = token.strip()
    try:
        yo = api(token, "getMe")["result"]
    except (urllib.error.URLError, KeyError, ValueError):
        raise SystemExit("Ese token no funciona. Cópialo de nuevo desde @BotFather.")
    print("Bot encontrado: @%s" % yo["username"])

    mensajes = api(token, "getUpdates").get("result", [])
    chats = [m["message"]["chat"] for m in mensajes if "message" in m]
    if not chats:
        raise SystemExit("No veo ningún mensaje. Abre @%s en Telegram, pulsa Iniciar, "
                         "escríbele 'hola' y vuelve a ejecutar esto." % yo["username"])
    chat = chats[-1]
    print("Chat detectado: %s" % (chat.get("first_name") or chat.get("title") or chat["id"]))

    ruta = BASE / ".env"
    ruta.write_text("TELEGRAM_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n" % (token, chat["id"]), encoding="utf-8")
    os.chmod(ruta, 0o600)
    api(token, "sendMessage", {"chat_id": chat["id"],
                               "text": "✅ Bot de chollos conectado. Aquí te llegarán los avisos."})
    print("Listo: guardado en .env y mensaje de prueba enviado a tu Telegram.")


if __name__ == "__main__":
    main()
