"""Lectura del archivo FIT de una actividad: streams y vueltas.

La API Personal de Garmin no expone muestras segundo a segundo ni vueltas
individuales: solo vienen en el archivo original del reloj. De ahi salen las
dos cosas que alimentan la carga TRIMP, la deriva cardiaca y el emparejamiento
de vueltas contra `estructura=`, con la misma forma que ya producen
intervals.icu y Strava.

El SDK oficial (`garmin-fit-sdk`) se importa adentro de `leer`: si faltara en
la Pi, el error es de esta fuente (`GarminError`) y la semana degrada igual que
con cualquier otra falla, en vez de romper el import del paquete.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Any

from .errors import GarminError


@dataclass
class ActividadFit:
    #: {clave: lista} con las claves que usa `strava.metricas`: time (s desde
    #: el inicio), distance (m), heartrate, cadence (spm), watts,
    #: velocity_smooth (m/s). Una muestra sin el dato lleva None.
    streams: dict[str, list[Any]] = field(default_factory=dict)
    #: Vueltas con el vocabulario de Strava (`distance`, `moving_time`,
    #: `elapsed_time`, `lap_index`) para reutilizar su normalizacion.
    vueltas: list[dict[str, Any]] = field(default_factory=list)


def _contenido_fit(crudo: bytes) -> bytes:
    """Garmin entrega el original dentro de un ZIP; se acepta tambien suelto."""
    try:
        with zipfile.ZipFile(io.BytesIO(crudo)) as z:
            nombre = next((n for n in z.namelist() if n.lower().endswith(".fit")), None)
            if nombre is None:
                raise GarminError(f"el ZIP de la actividad no trae un .fit: {z.namelist()}")
            return z.read(nombre)
    except zipfile.BadZipFile:
        return crudo


def _cadencia_spm(m: dict[str, Any]) -> float | None:
    """El FIT guarda la cadencia de carrera en rpm de una pierna."""
    rpm = m.get("cadence")
    if rpm is None:
        return None
    return round((float(rpm) + float(m.get("fractional_cadence") or 0.0)) * 2, 1)


def leer(crudo: bytes) -> ActividadFit:
    datos = _contenido_fit(crudo)
    if datos[8:12] != b".FIT":
        raise GarminError("el archivo de la actividad no es un FIT")
    try:
        from garmin_fit_sdk import Decoder, Stream
    except ImportError as exc:
        raise GarminError(
            "falta garmin-fit-sdk: corre `pip install -e .` en el venv de la Pi"
        ) from exc

    mensajes, errores = Decoder(Stream.from_byte_array(bytearray(datos))).read()
    registros = [m for m in mensajes.get("record_mesgs", []) if m.get("timestamp")]
    if errores and not registros:
        raise GarminError(f"FIT ilegible: {errores[0]}")

    salida = ActividadFit()
    if registros:
        t0 = registros[0]["timestamp"]
        s: dict[str, list[Any]] = {
            "time": [], "distance": [], "heartrate": [],
            "cadence": [], "watts": [], "velocity_smooth": [],
        }
        for m in registros:
            s["time"].append((m["timestamp"] - t0).total_seconds())
            s["distance"].append(m.get("distance"))
            s["heartrate"].append(m.get("heart_rate"))
            s["cadence"].append(_cadencia_spm(m))
            s["watts"].append(m.get("power"))
            s["velocity_smooth"].append(m.get("enhanced_speed", m.get("speed")))
        # Una serie que el reloj no midio en ninguna muestra no es un stream:
        # devolverla llena de None haria creer que hay HR y daria carga cero.
        salida.streams = {k: v for k, v in s.items() if any(x is not None for x in v)}

    for i, v in enumerate(mensajes.get("lap_mesgs", [])):
        indice = v.get("message_index")
        salida.vueltas.append(
            {
                "distance": v.get("total_distance"),
                "moving_time": v.get("total_timer_time"),
                "elapsed_time": v.get("total_elapsed_time"),
                "lap_index": (int(indice) + 1) if isinstance(indice, int) else i + 1,
            }
        )
    return salida
