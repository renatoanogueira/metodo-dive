"""Segunda etapa de la skill metodo-dive: valida insights.json, calcula los totales y exporta informe-finops.md.

La skill escribe insights.json (lo que concluye al analizar la corrida); este script lo valida con las mismas
reglas que usa el dashboard (common/insights.py), resuelve el dinero y los porcentajes desde sus fuentes y genera
el informe en Markdown a partir de esos datos. El .md es un derivado: lo que muestra el dashboard sale de
insights.json, nunca del .md, y ningún número se escribe a mano.

Uso: .venv/bin/python -I skill/scripts/insights.py <carpeta de la corrida> [--sin-md]
     (sale con 0 si es válido y escribe informe-finops.md; con 2 y la lista de problemas si no)
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from resumen import RAIZ, _leer  # noqa: E402,F401  (resumen agrega RAIZ al path)
from common import moneda_de_pago  # noqa: E402
from common.insights import MAX_ARCHIVO, renderizar, revisar, totales  # noqa: E402
from common.verificacion import cargar_json, leer_en_carpeta  # noqa: E402

SECCIONES = (("seguro_ya", "Ahorro seguro ya"),
             ("firma", "Necesita firma o decisión del dueño"),
             ("contradicho", "Contradicho por la verificación"),
             ("no_verificado", "No se pudo verificar"),
             ("informativo", "Gasto, causas de costo y recomendaciones"))
ETIQUETA_TIPO = {"hallazgo": "hallazgo", "causa_costo": "causa de costo", "recomendacion": "recomendación",
                 "pregunta": "pregunta", "laguna": "lo que no se pudo ver"}


def contexto(carpeta):
    """Lo que necesita el validador: findings por fingerprint, discovery, cuentas y las salidas crudas del runner."""
    carpeta = Path(carpeta)
    doc = _leer(carpeta / "findings.json")
    discovery = _leer(carpeta / "discovery.json")
    findings = {f["fingerprint"]: f for f in doc.get("findings") or [] if isinstance(f, dict) and f.get("fingerprint")}
    cuentas = set(discovery.get("accounts") or {}) | {f.get("account_id") for f in findings.values() if f.get("account_id")}

    def aws(ref):
        crudo = leer_en_carpeta(carpeta / "aws", ref, 1_000_000)
        try:
            return cargar_json(crudo) if crudo is not None else None
        except ValueError:
            return None

    run = doc.get("run") if isinstance(doc.get("run"), dict) else {}
    return {"findings": findings, "discovery": discovery, "cuentas": cuentas, "aws": aws}, run.get("run_id")


def validar(carpeta):
    """→ (insights válidos con valores, problemas, contexto, totales). Sin archivo o ilegible → problema."""
    carpeta = Path(carpeta)
    ctx, run_id = contexto(carpeta)
    crudo = leer_en_carpeta(carpeta, "insights.json", MAX_ARCHIVO)
    if crudo is None:
        return [], ["insights.json falta, es un symlink/hardlink, pesa más de 2 MB o no es un archivo regular"], ctx, {}
    try:
        datos = cargar_json(crudo)
    except ValueError as e:
        return [], [f"insights.json no es JSON válido ({e})"], ctx, {}
    validos, problemas = revisar(datos, run_id, ctx)
    return validos, problemas, ctx, totales(ctx["findings"], validos)


def exportar_md(run_id, insights, tot, ctx):
    """informe-finops.md derivado: mismo contenido que ve el dashboard, en USD (la moneda de pago, solo la tasa)."""
    nombres = {a: (d or {}).get("name") for a, d in (ctx["discovery"].get("accounts") or {}).items()}
    moneda = moneda_de_pago(ctx["discovery"])
    L = [f"# Informe FinOps · corrida {run_id}", "",
         "Exportado de `insights.json` por `skill/scripts/insights.py`. Los montos salen de las fuentes de cada cifra "
         "(findings, discovery.json, evidencia); los totales los calcula el script. Todo en USD"
         + (f"; se paga en {moneda['codigo']}, tasa {moneda['tasa']:g} por dólar"
            + (f" (factura de {moneda['periodo']})" if moneda.get("periodo") else "") if moneda else "") + ".", "",
         "| | $/mes |", "|---|---:|",
         f"| **Ahorro seguro ya** (reversible y verificado) | **${tot['seguro_ya']:,.2f}** |",
         f"| Máximo teórico (suma de los ahorros, sin doble conteo) | ${tot['maximo_teorico']:,.2f} |",
         f"| Gasto a revisar (no es ahorro) | ${tot['gasto_informativo']:,.2f} |", ""]
    ordenados = sorted(insights, key=lambda i: (i["prioridad"], i["id"]))
    for estado, titulo in SECCIONES:
        grupo = [i for i in ordenados if i["estado"] == estado and i["tipo"] != "laguna"]
        if not grupo:
            continue
        L += [f"## {titulo}", ""]
        for i in grupo:
            L += _bloque(i, nombres)
    lagunas = [i for i in ordenados if i["tipo"] == "laguna"]
    if lagunas:
        L += ["## Lo que no se pudo ver", ""]
        for i in lagunas:
            L += _bloque(i, nombres)
    return "\n".join(L).rstrip() + "\n"


def _bloque(i, nombres):
    a = i["alcance"]
    donde = " · ".join(x for x in (
        f"{nombres.get(a['cuenta']) or a['cuenta']}" if a.get("cuenta") else None, a.get("region"),
        f"{len(a['fingerprints'])} recurso(s)" if a.get("fingerprints") else None) if x) or "toda la organización"
    L = [f"### {renderizar(i['titulo'], i.get('cifras') or {}, i['valores'])} · {i['id']}",
         f"- {ETIQUETA_TIPO[i['tipo']]} · prioridad {i['prioridad']} · {donde}", "",
         renderizar(i["texto"], i.get("cifras") or {}, i["valores"]), ""]
    for e in i.get("evidencia") or []:
        L.append(f"- Evidencia [{e['id']}] ({e['origen']}): {e['descripcion']}" + (f" — `{e['comando']}`" if e.get("comando") else ""))
    if i.get("no_determinado"):
        L.append(f"- **No determinado:** {i['no_determinado']}")
    if i.get("pregunta_dueno"):
        L.append(f"- **Pregunta para el dueño:** {i['pregunta_dueno']}")
    if i.get("accion"):
        L.append(f"- Acción (la ejecuta una persona): `{i['accion']['comando']}`"
                 + (f" · {i['accion']['reversibilidad']}" if i["accion"].get("reversibilidad") else ""))
    return L + [""]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("carpeta")
    ap.add_argument("--sin-md", action="store_true", help="solo validar; no escribir informe-finops.md")
    args = ap.parse_args()
    carpeta = Path(args.carpeta)
    if not (carpeta / "findings.json").exists():
        raise SystemExit(f"No hay findings.json en {carpeta}: corre antes las fases 1-3 (bash dive.sh).")
    validos, problemas, ctx, tot = validar(carpeta)
    if problemas:
        print("insights.json NO es válido (no se muestra en el dashboard ni se exporta):")
        for p in problemas:
            print(f"  - {p}")
        return 2
    run_id = _leer(carpeta / "findings.json").get("run", {}).get("run_id")
    por_estado = {}
    for i in validos:
        por_estado[i["estado"]] = por_estado.get(i["estado"], 0) + 1
    print(f"insights.json válido: {len(validos)} insights ({', '.join(f'{k}={v}' for k, v in sorted(por_estado.items()))}); "
          f"ahorro seguro ya ${tot['seguro_ya']:,.2f}/mes de un máximo teórico de ${tot['maximo_teorico']:,.2f}/mes")
    if not args.sin_md:
        # El informe lleva el hash del insights.json del que sale: el dashboard avisa si ya no corresponde.
        sha = hashlib.sha256(leer_en_carpeta(carpeta, "insights.json", MAX_ARCHIVO) or b"").hexdigest()
        texto = exportar_md(run_id, validos, tot, ctx) + f"\n<!-- insights-sha256: {sha} -->\n"
        try:  # sin seguir un symlink que alguien haya puesto en lugar del informe
            fd = os.open(carpeta / "informe-finops.md", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
        except OSError as e:
            print(f"ERROR: no se pudo escribir informe-finops.md ({e.strerror}); ¿es un enlace simbólico?", file=sys.stderr)
            return 3
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(texto)
        print(f"informe-finops.md escrito en {carpeta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
