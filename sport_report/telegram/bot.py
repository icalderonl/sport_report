"""Bot de Telegram (proceso persistente, systemd).

Adaptador delgado: toda la logica vive en `comandos.py`, que se testea sin red.
Usa long polling porque la Pi esta detras de NAT domestico y un webhook
exigiria puerto publico y TLS.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .. import config
from ..logging_setup import setup
from ..plan.store import PlanStore
from . import comandos
from .formato import trozos

log = logging.getLogger(__name__)
_store = PlanStore()


def _autorizado(update: Update) -> bool:
    """El token del bot no es un secreto fuerte: cualquiera que lo tenga puede
    escribirle. Solo se responde al chat configurado."""
    chat = update.effective_chat
    if chat is None:
        return False
    esperado = str(config.TELEGRAM_CHAT_ID).strip()
    if not esperado:
        log.warning("TELEGRAM_CHAT_ID sin configurar; el bot no respondera a nadie")
        return False
    if str(chat.id) != esperado:
        log.warning("mensaje ignorado de chat no autorizado: %s", chat.id)
        return False
    return True


async def _responder(update: Update, texto: str) -> None:
    for parte in trozos(texto):
        await update.effective_message.reply_text(parte)


def _handler(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _autorizado(update):
            return
        try:
            texto = fn(update, ctx)
        except Exception:  # nunca dejar al usuario sin respuesta
            log.exception("error manejando el comando")
            texto = "Error interno procesando el comando. Revisa logs/bot.log."
        await _responder(update, texto)

    return wrapper


@_handler
def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return comandos.AYUDA


@_handler
def on_setplan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return comandos.cmd_setplan(_store, update.effective_message.text or "")


@_handler
def on_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return comandos.cmd_plan(_store)


@_handler
def on_fuerza(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return comandos.cmd_fuerza(_store, " ".join(ctx.args or []))


@_handler
def on_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return comandos.cmd_estado(_store)


@_handler
def on_progreso(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    # Consulta Strava, asi que tarda unos segundos mas que el resto.
    return comandos.cmd_progreso(_store)


@_handler
def on_volumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return comandos.cmd_volumen()


@_handler
def on_desconocido(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return "Comando no reconocido.\n\n" + comandos.AYUDA


def main() -> None:
    setup("bot")
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help", "ayuda"], on_start))
    app.add_handler(CommandHandler("setplan", on_setplan))
    app.add_handler(CommandHandler("plan", on_plan))
    app.add_handler(CommandHandler("fuerza", on_fuerza))
    app.add_handler(CommandHandler("estado", on_estado))
    app.add_handler(CommandHandler(["progreso", "avance"], on_progreso))
    app.add_handler(CommandHandler("volumen", on_volumen))
    app.add_handler(MessageHandler(filters.COMMAND, on_desconocido))

    log.info("bot iniciado (long polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
