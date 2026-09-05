# Runbook — API de Anthropic (capa narrativa)

Es la única parte opcional del sistema: si esto no está configurado, el reporte
llega igual, solo sin el párrafo de resumen.

## 1. Cuenta y key

<https://console.anthropic.com> — **no** es lo mismo que una cuenta de
claude.ai. Necesita billing propio.

1. Crea la cuenta y carga saldo (el mínimo alcanza para años a este volumen).
2. **API Keys** → **Create Key**. Se muestra una sola vez.
3. Va a `.env`:

```
ANTHROPIC_API_KEY=sk-ant-api03-...
ANTHROPIC_MODEL=claude-haiku-4-5
```

## 2. Probar

```bash
python -m sport_report.narrative.probar
```

Sin argumento usa un JSON de ejemplo, así se prueba la API antes de tener datos
reales. Con un archivo (`python -m sport_report.narrative.probar reporte.json`)
usa ese reporte.

Imprime el texto, los tokens consumidos y —lo importante— si el modelo metió
alguna cifra que no estaba en el JSON.

## 3. Costo

Una llamada por semana. El JSON del motor de cálculo pesa ~4 KB (~1.500 tokens
de entrada) y la respuesta son ~150 tokens.

Con Haiku 4.5 ($1.00 por millón de tokens de entrada, $5.00 de salida): del
orden de **US$0,002 por semana**, unos 10 centavos de dólar al año. El mínimo de
carga de la consola es lo único que va a pesar.

## Qué hace y qué no hace el LLM

Recibe **únicamente** el JSON de la fase 5. El prompt del sistema le prohíbe
introducir cualquier cifra que no esté ahí: no calcula, no promedia, no estima,
no convierte unidades.

Además hay una verificación posterior en código: se extraen todos los números
del texto generado y se comparan contra los que existen en el JSON (tolerando el
redondeo a un decimal). Si aparece alguno sin respaldo, queda en `logs/` y en el
JSON del reporte bajo `numeros_no_verificados`. El texto **no se descarta** —un
falso positivo dejaría el reporte mudo— pero queda auditable.

## Si la API falla

El reporte se envía igual, sin narrativa, y el error queda en el log. Está
cubierto con tests para timeout, error de red, respuesta vacía, rechazo del
modelo y falta de key. Nunca hace caer la corrida semanal.

## Nota sobre Python

El SDK `anthropic` 1.x requiere **Python 3.10+**. El proyecto declara 3.11+, que
es lo que trae Raspberry Pi OS bookworm. Si tu Python local es más viejo, el
resto del sistema corre igual (el import del SDK es perezoso) pero no vas a
poder probar la narrativa en ese equipo.
