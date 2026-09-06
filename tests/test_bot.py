"""Tests del adaptador de Telegram.

`comandos.py` ya esta cubierto en otros archivos; lo que faltaba era la puerta:
`_autorizado` es el unico control de acceso del sistema —el token del bot no es
un secreto fuerte, cualquiera que lo tenga puede escribirle— y nada impedia que
una refactorizacion lo invirtiera en silencio.

Los handlers son corrutinas y el proyecto no depende de pytest-asyncio, asi que
se ejecutan con `asyncio.run` desde tests sincronos.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from sport_report import config
from sport_report.telegram import bot, comandos


# --------------------------------------------------------------------------
# Dobles
# --------------------------------------------------------------------------


class MensajeFalso:
    def __init__(self, texto: str = ""):
        self.text = texto
        self.respuestas: list[str] = []
        self.fotos: list[tuple] = []

    async def reply_text(self, texto: str) -> None:
        self.respuestas.append(texto)

    async def reply_photo(self, photo=None, caption: str = "") -> None:
        self.fotos.append((photo, caption))


class ChatFalso:
    def __init__(self, id_):
        self.id = id_


class UpdateFalso:
    def __init__(self, chat_id=42, texto: str = ""):
        self.effective_chat = None if chat_id is None else ChatFalso(chat_id)
        self.effective_message = MensajeFalso(texto)


@pytest.fixture
def chat_autorizado(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
    return "42"


# --------------------------------------------------------------------------
# _autorizado: el control de acceso
# --------------------------------------------------------------------------


def test_responde_al_chat_configurado(chat_autorizado):
    assert bot._autorizado(UpdateFalso(chat_id=42)) is True


def test_el_chat_id_se_compara_como_texto(chat_autorizado):
    """Telegram manda el id como int y la config lo trae como str."""
    assert bot._autorizado(UpdateFalso(chat_id=42)) is True
    assert bot._autorizado(UpdateFalso(chat_id="42")) is True


def test_ignora_cualquier_otro_chat(chat_autorizado):
    assert bot._autorizado(UpdateFalso(chat_id=99)) is False
    assert bot._autorizado(UpdateFalso(chat_id=-42)) is False
    assert bot._autorizado(UpdateFalso(chat_id="42 ")) is False
    assert bot._autorizado(UpdateFalso(chat_id="")) is False


def test_sin_chat_id_configurado_no_responde_a_nadie(monkeypatch, caplog):
    """Un .env a medio llenar no puede dejar el bot abierto al mundo.

    Se comprueba tambien el aviso en el log: la comparacion de mas abajo ya
    rechaza a todo el mundo cuando `esperado` es "", asi que lo unico que aporta
    la guarda explicita es decir POR QUE el bot no contesta. Sin esto, quitarla
    no rompia ningun test y el operador se quedaba sin la pista.
    """
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")

    with caplog.at_level("WARNING"):
        assert bot._autorizado(UpdateFalso(chat_id=42)) is False
        assert bot._autorizado(UpdateFalso(chat_id=0)) is False

    assert "TELEGRAM_CHAT_ID sin configurar" in caplog.text


def test_un_update_sin_chat_no_pasa(chat_autorizado):
    assert bot._autorizado(UpdateFalso(chat_id=None)) is False


def test_tolera_espacios_en_la_configuracion(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "  42  ")

    assert bot._autorizado(UpdateFalso(chat_id=42)) is True


# --------------------------------------------------------------------------
# _handler
# --------------------------------------------------------------------------


def test_un_chat_no_autorizado_no_recibe_respuesta(chat_autorizado):
    llamado = []

    @bot._handler
    def comando(update, ctx):
        llamado.append(True)
        return "secreto"

    update = UpdateFalso(chat_id=99)
    asyncio.run(comando(update, None))

    assert update.effective_message.respuestas == []
    assert llamado == [], "ni siquiera se ejecuto el comando"


def test_un_error_del_comando_no_deja_al_usuario_sin_respuesta(chat_autorizado):
    @bot._handler
    def comando(update, ctx):
        raise RuntimeError("algo se rompio")

    update = UpdateFalso()
    asyncio.run(comando(update, None))  # no propaga

    assert len(update.effective_message.respuestas) == 1
    assert "Error interno" in update.effective_message.respuestas[0]


def test_una_respuesta_larga_se_parte_en_varios_mensajes(chat_autorizado):
    @bot._handler
    def comando(update, ctx):
        return "\n".join("linea " + str(i) for i in range(2000))

    update = UpdateFalso()
    asyncio.run(comando(update, None))

    respuestas = update.effective_message.respuestas
    assert len(respuestas) > 1
    assert all(0 < len(r) <= 4096 for r in respuestas)


def test_el_comando_corre_fuera_del_event_loop(chat_autorizado):
    """/progreso sincroniza con Strava y tarda segundos: si corriera en el loop,
    el bot dejaria de atender nada mientras tanto."""
    hilos: dict[str, str] = {}

    @bot._handler
    def comando(update, ctx):
        hilos["comando"] = threading.current_thread().name
        return "listo"

    async def correr():
        hilos["loop"] = threading.current_thread().name
        await comando(UpdateFalso(), None)

    asyncio.run(correr())

    assert hilos["comando"] != hilos["loop"]


# --------------------------------------------------------------------------
# /volumen: el unico que responde con imagen
# --------------------------------------------------------------------------


def test_volumen_manda_la_foto_cuando_hay_grafico(chat_autorizado, monkeypatch, tmp_path):
    png = tmp_path / "volumen.png"
    png.write_bytes(b"\x89PNG fingido")
    monkeypatch.setattr(comandos, "cmd_volumen", lambda: (png, "40 km en total"))

    update = UpdateFalso()
    asyncio.run(bot.on_volumen(update, None))

    assert len(update.effective_message.fotos) == 1
    assert update.effective_message.fotos[0][1] == "40 km en total"
    assert update.effective_message.respuestas == []


def test_volumen_responde_texto_cuando_no_hay_grafico(chat_autorizado, monkeypatch):
    monkeypatch.setattr(comandos, "cmd_volumen", lambda: (None, "Todavia no hay kilometros"))

    update = UpdateFalso()
    asyncio.run(bot.on_volumen(update, None))

    assert update.effective_message.fotos == []
    assert update.effective_message.respuestas == ["Todavia no hay kilometros"]


def test_volumen_no_responde_a_un_chat_ajeno(chat_autorizado, monkeypatch):
    monkeypatch.setattr(comandos, "cmd_volumen", lambda: pytest.fail("no debio ejecutarse"))

    update = UpdateFalso(chat_id=99)
    asyncio.run(bot.on_volumen(update, None))

    assert update.effective_message.respuestas == []
    assert update.effective_message.fotos == []


def test_volumen_avisa_si_el_grafico_revienta(chat_autorizado, monkeypatch):
    def explota():
        raise RuntimeError("matplotlib no esta")

    monkeypatch.setattr(comandos, "cmd_volumen", explota)

    update = UpdateFalso()
    asyncio.run(bot.on_volumen(update, None))

    assert "Error interno" in update.effective_message.respuestas[0]


# --------------------------------------------------------------------------
# Cableado
# --------------------------------------------------------------------------


def test_todos_los_comandos_de_la_ayuda_estan_registrados(monkeypatch):
    """Agregar un comando a AYUDA y olvidar el handler es un fallo silencioso:
    el bot responde "comando no reconocido" a algo que el mismo anuncia."""
    import re

    registrados: list[str] = []

    class AppFalsa:
        def add_handler(self, handler):
            nombres = getattr(handler, "commands", None)
            if nombres:
                registrados.extend(nombres)

        def run_polling(self, **kw):
            pass

    class Builder:
        def token(self, _):
            return self

        def build(self):
            return AppFalsa()

    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:falso")
    monkeypatch.setattr(bot.Application, "builder", staticmethod(lambda: Builder()))
    bot.main()

    anunciados = set(re.findall(r"^/(\w+)", comandos.AYUDA, re.M))
    assert anunciados, "no se pudo leer la ayuda"
    assert anunciados <= set(registrados), (
        f"anunciados en la ayuda pero sin handler: {sorted(anunciados - set(registrados))}"
    )


def test_sin_token_el_bot_no_arranca(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")

    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        bot.main()
