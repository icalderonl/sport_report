"""Rasteriza assets/logo.svg a PNG sin depender de cairo (no esta en el equipo).

    python assets/render_logo.py

Redibuja la misma geometria con Pillow a 4x y reduce con LANCZOS, que da
bordes mas limpios que rasterizar directo al tamano final.
"""
from pathlib import Path

from PIL import Image, ImageDraw

LADO = 512
S = 4  # supermuestreo
SALIDAS = (512, 256, 124)  # 124 es el minimo que pide Strava

FONDO_TOP, FONDO_BOT = (0x16, 0x23, 0x3A), (0x0A, 0x11, 0x20)
LINEA = (0x38, 0xBD, 0xF8)
CORREDOR = (0xFB, 0xBF, 0x24)
RADIO = 115


def miembro(d: ImageDraw.ImageDraw, puntos, ancho, color):
    """Traza segmentos gruesos con juntas y remates redondos."""
    r = ancho / 2
    for (x0, y0), (x1, y1) in zip(puntos, puntos[1:]):
        d.line([x0 * S, y0 * S, x1 * S, y1 * S], fill=color, width=int(ancho * S))
    for x, y in puntos:  # circulos en cada vertice = linecap/linejoin round
        d.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S], fill=color)


def dibujar() -> Image.Image:
    n = LADO * S

    # fondo con degradado vertical
    fondo = Image.new("RGB", (1, n))
    px = fondo.load()
    for y in range(n):
        t = y / (n - 1)
        px[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(FONDO_TOP, FONDO_BOT))
    img = fondo.resize((n, n), Image.NEAREST).convert("RGBA")

    d = ImageDraw.Draw(img)

    # linea de tendencia y su nodo final
    miembro(d, [(96, 432), (198, 406), (300, 418), (416, 360)], 16, LINEA)
    d.ellipse([(416 - 20) * S, (360 - 20) * S, (416 + 20) * S, (360 + 20) * S], fill=LINEA)

    # corredor
    d.ellipse([(310 - 37) * S, (148 - 37) * S, (310 + 37) * S, (148 + 37) * S], fill=CORREDOR)
    miembro(d, [(278, 194), (236, 282)], 36, CORREDOR)                 # torso
    miembro(d, [(278, 194), (340, 230), (358, 182)], 26, CORREDOR)     # brazo adelante
    miembro(d, [(278, 194), (214, 222), (198, 274)], 26, CORREDOR)     # brazo atras
    miembro(d, [(236, 282), (330, 296), (294, 358)], 32, CORREDOR)     # pierna adelante
    miembro(d, [(236, 282), (190, 358), (132, 384)], 32, CORREDOR)     # pierna atras

    # esquinas redondeadas
    mascara = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mascara).rounded_rectangle([0, 0, n - 1, n - 1], radius=RADIO * S, fill=255)
    img.putalpha(mascara)
    return img


def main() -> None:
    maestro = dibujar()
    destino = Path(__file__).parent
    for lado in SALIDAS:
        p = destino / f"logo-{lado}.png"
        maestro.resize((lado, lado), Image.LANCZOS).save(p)
        print(f"  {p.name}  ({p.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
