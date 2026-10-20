"""Resumen compacto de una corrida de DIVE para la skill metodo-dive: totales sin doble conteo, candidatos a
ahorro con su reversibilidad, gasto informativo, riesgos, datos FinOps y lo que la corrida no pudo ver.

Es determinístico (sin AWS ni modelo): la skill lo lee en vez de cargar findings.json entero, y los totales
salen iguales que en findings.md. La reversibilidad es de cada tipo de problema (REVERSIBILIDAD), no de la
confianza del precio: un finding con precio de alta confianza puede ser una compra irreversible.

Uso: .venv/bin/python -I skill/scripts/resumen.py <carpeta de la corrida> [--top 15]
"""

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))

# Reversibilidad de cada tipo de problema: tabla compartida con el validador y el dashboard.
from common.verificacion import (DESCONOCIDA, REVERSIBILIDAD, REVERSIBILIDAD_COH, cargar_json,  # noqa: E402,F401
                                 leer_archivo_seguro, monto, reversibilidad)

TAGS_DUENO = ("owner", "dueño", "dueno", "responsable", "team", "squad", "contact")
TAGS_ENTORNO = ("environment", "env", "entorno", "ambiente", "stage")


def _leer(ruta):
    """El JSON como dict, o {} si falta, es un symlink, está roto, no es un objeto o trae NaN/Infinity."""
    crudo = leer_archivo_seguro(ruta, 500_000_000)
    try:
        d = cargar_json(crudo) if crudo is not None else None
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _usd(f):
    return monto(f)


def _indice_recursos(inventory):
    idx = {}
    for r in inventory.get("resources") or []:
        if not isinstance(r, dict):
            continue
        for clave in (r.get("id"), r.get("arn")):
            if clave:
                idx[(r.get("account_id"), clave)] = r
    return idx


def _contexto(f, idx):
    """Dueño, entorno y si lo gestiona IaC, de las tags del inventario. Sin dato: None (la skill dice
    "desconocido"; nunca se infiere del nombre)."""
    r = idx.get((f.get("account_id"), f.get("resource_id")))
    if not r:
        return {"dueno": None, "entorno": None, "iac": "desconocido", "nombre": None}
    tags = r.get("tags") if isinstance(r.get("tags"), dict) else {}
    tags = {str(k): str(v) for k, v in tags.items()}
    bajo = {k.lower(): v for k, v in tags.items()}
    # Sin tags de IaC no se sabe: Terraform y Pulumi no etiquetan por defecto. "desconocido" ≠ "sin IaC".
    iac = ("cloudformation" if any(k.startswith("aws:cloudformation:") for k in tags)
           else "terraform" if any("terraform" in k.lower() or "terraform" in v.lower() for k, v in tags.items())
           else "desconocido")
    return {"dueno": next((bajo[k] for k in TAGS_DUENO if k in bajo), None),
            "entorno": next((bajo[k] for k in TAGS_ENTORNO if k in bajo), None),
            "iac": iac, "nombre": r.get("name")}


MAX_FP_GRUPO = 100  # el contrato admite hasta 100 fingerprints por insight


def _grupos(findings):
    """Todos los findings agrupados por regla: lo que la skill necesita para citar fingerprints (y sumar montos)
    sin leer findings.json. `fp_con_monto` es el subconjunto que sirve en una fuente `suma:` (monto positivo).
    Si el grupo es `mixto` (unos findings suman como ahorro y otros no, ej. AMIs de AWS Backup), para hablar de ahorro usa
    `fp_ahorro`: `suma_usd` y `fp_con_monto` incluyen a los que no suman."""
    por_regla = {}
    for f in findings:
        por_regla.setdefault(f.get("rule_id") or "?", []).append(f)
    grupos = []
    for rid, lista in por_regla.items():
        con_monto = [f for f in lista if _usd(f)]
        ahorro = [f for f in con_monto if f.get("is_saving")]
        grupos.append({
            "rule_id": rid, "n": len(lista), "categoria": lista[0].get("category"),
            "suma_usd": round(sum(_usd(f) for f in con_monto), 2),
            "suma_como_ahorro": any(f.get("is_saving") for f in lista),
            "mixto": any(f.get("is_saving") for f in lista) and not all(f.get("is_saving") for f in lista),
            "fp_ahorro": [f.get("fingerprint") for f in ahorro if f.get("fingerprint")][:MAX_FP_GRUPO],
            "reversibilidad": reversibilidad(lista[0]),
            "cuentas": sorted({f.get("account_id") for f in lista if f.get("account_id")}),
            "fingerprints": [f.get("fingerprint") for f in lista if f.get("fingerprint")][:MAX_FP_GRUPO],
            "fp_con_monto": [f.get("fingerprint") for f in con_monto if f.get("fingerprint")][:MAX_FP_GRUPO],
            "truncado": len(lista) > MAX_FP_GRUPO,
            "ejemplos": [f.get("title") for f in lista[:3]]})
    return sorted(grupos, key=lambda g: (-g["suma_usd"], -g["n"], g["rule_id"]))


def resumir(carpeta, top=15):
    carpeta = Path(carpeta)
    findings_doc = _leer(carpeta / "findings.json")
    discovery = _leer(carpeta / "discovery.json")
    inventory = _leer(carpeta / "inventory.json")
    lista = findings_doc.get("findings")
    findings = [f for f in lista if isinstance(f, dict)] if isinstance(lista, list) else []
    idx = _indice_recursos(inventory)

    def item(f):
        return {"id": f.get("id"), "fingerprint": f.get("fingerprint"), "rule_id": f.get("rule_id"),
                "issue_id": f.get("issue_id"), "category": f.get("category"), "severity": f.get("severity"),
                "account_id": f.get("account_id"), "region": f.get("region"), "resource_id": f.get("resource_id"),
                "title": f.get("title"), "detail": f.get("detail"), "monthly_usd": f.get("monthly_usd"),
                "price_source": f.get("price_source"), "confidence": f.get("confidence"),
                "source": f.get("source"), "reversibilidad": reversibilidad(f), **_contexto(f, idx)}

    ahorro = sorted((f for f in findings if f.get("is_saving") and _usd(f)), key=lambda f: -_usd(f))
    # is_saving False con monto, en tres grupos distintos:
    #   solapados   el mismo recurso ya cuenta en otro ahorro (gp2→gp3 de un volumen sin adjuntar)
    #   gasto       gasto actual, no un ahorro (IPv4, logs, compromiso sin usar): reversibilidad "informativo"
    #   diferidos   un ahorro que no llega a la factura todavía (cómputo con Savings Plans sobrando)
    # El mismo criterio que validar.deduplicar: el recurso lo "ocupa" un ahorro con monto o un desperdicio aunque
    # no tenga monto (ej. una instancia ociosa sin precio bloquea su cambio de familia).
    contados = {(f.get("account_id"), f.get("region"), f.get("resource_id")) for f in findings
                if f.get("is_saving") and f.get("resource_id") and (_usd(f) or f.get("category") == "desperdicio")}
    no_suman = [f for f in findings if not f.get("is_saving") and _usd(f)
                and f.get("category") in ("costo", "desperdicio")]
    solapados = [f for f in no_suman if f.get("resource_id")
                 and (f.get("account_id"), f.get("region"), f.get("resource_id")) in contados]
    resto = [f for f in no_suman if f not in solapados]
    gasto = sorted((f for f in resto if reversibilidad(f) == "informativo"), key=lambda f: -_usd(f))
    diferidos = sorted((f for f in resto if reversibilidad(f) != "informativo"), key=lambda f: -_usd(f))
    por_rev = {}
    for f in ahorro:
        r = reversibilidad(f)
        por_rev[r] = round(por_rev.get(r, 0) + _usd(f), 2)
    riesgos = [f for f in findings if f.get("category") == "riesgo"]
    def dic(x):
        return x if isinstance(x, dict) else {}
    run = dic(findings_doc.get("run"))
    fin = dic(discovery.get("finops"))
    sp = dic(fin.get("savings_plans"))
    hub = dic(fin.get("hub_optimizacion"))
    moneda = dic(fin.get("moneda_pago"))
    return {
        "corrida": str(carpeta),
        "run_id": run.get("run_id"),
        "generado": findings_doc.get("generated_at"),
        "flags": dic(run.get("flags")).get("validar"),
        # Igual que el total de findings.md: suma de is_saving (sin doble conteo por recurso).
        "maximo_teorico_usd": round(sum(_usd(f) for f in ahorro), 2),
        "ahorro_por_reversibilidad_usd": por_rev,
        "gasto_informativo_usd": round(sum(_usd(f) for f in gasto), 2),
        "candidatos": [item(f) for f in ahorro[:top]],
        "n_candidatos_total": len(ahorro),
        "gasto_informativo": [item(f) for f in gasto[:top]],
        "solapados": [item(f) for f in solapados[:top]],
        "grupos": _grupos(findings),
        "diferidos": [item(f) for f in diferidos[:top]],
        "riesgos": {"por_severidad": {s: sum(1 for f in riesgos if f.get("severity") == s) for s in ("alta", "media", "baja")},
                    "altos": [item(f) for f in riesgos if f.get("severity") == "alta"][:top]},
        "finops": {
            "savings_plans": {"activos": len(sp.get("activos") or []), "utilizacion": sp.get("utilizacion"),
                              "cobertura": sp.get("cobertura"),
                              "primer_vencimiento": min((a.get("fin") for a in sp.get("activos") or [] if a.get("fin")),
                                                        default=None)},
            "hub_optimizacion": {"estado": hub.get("estado"), "modo_estimacion": hub.get("modo_estimacion"),
                                 "recomendaciones": len(hub.get("recomendaciones") or [])},
            "anomalias": fin.get("anomalias"),
            # Solo tasa: el informe no debe mostrar montos convertidos (el anonimizador redondea USD, no EUR).
            "moneda_pago": {k: moneda.get(k) for k in ("codigo", "tasa", "periodo", "fuente")} if moneda else None,
            "leido": bool(fin),
        },
        "lagunas": {
            "errores_validar": [e for e in findings_doc.get("errors") or [] if isinstance(e, dict)][:30],
            "errores_inventario": [e for e in inventory.get("errors") or [] if isinstance(e, dict)][:30],
            "omitidas": inventory.get("skipped") or [],
            "avisos_finops": fin.get("avisos") or [],
            "sin_finops": not fin,
            "sin_findings": not findings_doc,
            # Sin estos archivos, dueño/entorno/IaC salen "desconocido" en todo: que el informe lo diga.
            "sin_inventario": not inventory,
            "sin_discovery": not discovery,
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("carpeta", nargs="?", help="Carpeta de la corrida (por defecto, la de ULTIMA en output_dir)")
    ap.add_argument("--config", help="dive.config.yaml (para encontrar ULTIMA)")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    carpeta = args.carpeta
    if not carpeta:
        sys.path.insert(0, str(RAIZ))
        from common import cargar_config, corrida_actual
        carpeta = corrida_actual(cargar_config(args.config))
    if not (Path(carpeta) / "findings.json").exists():
        raise SystemExit(f"No hay findings.json en {carpeta}: corre antes las fases 1-3 (bash dive.sh).")
    if not _leer(Path(carpeta) / "findings.json"):
        # Un archivo ilegible no es "nada que ahorrar": que no salga un resumen en $0.
        raise SystemExit(f"findings.json de {carpeta} no se puede leer (JSON roto, NaN o no es un objeto): "
                         "vuelve a correr la fase 3 (3-validar/validar.py).")
    print(json.dumps(resumir(carpeta, args.top), indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
