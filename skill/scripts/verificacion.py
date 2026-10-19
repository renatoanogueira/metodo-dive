"""Valida verificacion.json, lo que la skill averiguó en la pasada escéptica, antes de que lo lea el dashboard.

Comprueba la forma (ver CONTRATO.md) y además dos reglas: cada fingerprint existe en findings.json de la misma
corrida, y `seguro_ya` solo se usa en un ahorro que suma (is_saving, con monto) y es reversible. Las reglas están
en common/verificacion.py y el dashboard aplica las mismas: un archivo que no pasa no se muestra.

Uso: .venv/bin/python -I skill/scripts/verificacion.py <carpeta de la corrida>
     (sale con 0 y un resumen si está bien; con 2 y la lista de problemas si no)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from resumen import RAIZ, _leer, _usd  # noqa: E402,F401  (resumen agrega RAIZ al path)
from common.verificacion import ESTADOS, MAX_ARCHIVO, cargar_json, leer_archivo_seguro, revisar  # noqa: E402,F401


def validar(carpeta):
    """→ (items válidos, problemas). Un archivo con cualquier problema no debería publicarse (y el dashboard
    tampoco lo acepta: usa las mismas reglas, common/verificacion.py)."""
    carpeta = Path(carpeta)
    crudo = leer_archivo_seguro(carpeta / "verificacion.json", MAX_ARCHIVO)
    if crudo is None:
        return [], ["verificacion.json falta, es un symlink, pesa más de 2 MB o no es un archivo regular"]
    try:
        datos = cargar_json(crudo)
    except ValueError:
        return [], ["verificacion.json no es JSON válido"]
    findings_doc = _leer(carpeta / "findings.json")
    run = findings_doc.get("run") if isinstance(findings_doc.get("run"), dict) else {}
    por_fp = {f.get("fingerprint"): f for f in findings_doc.get("findings") or [] if isinstance(f, dict)}
    return revisar(datos, run.get("run_id"), por_fp)


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__.split("Uso:")[1].strip())
    carpeta = Path(sys.argv[1])
    validos, problemas = validar(carpeta)
    if problemas:
        print("verificacion.json NO es válido:")
        for p in problemas:
            print(f"  - {p}")
        return 2
    findings = {f["fingerprint"]: f for f in _leer(carpeta / "findings.json").get("findings") or []
                if isinstance(f, dict) and f.get("fingerprint")}
    por_estado = {}
    for it in validos:
        por_estado[it["estado"]] = por_estado.get(it["estado"], 0) + 1
    seguro = sum(_usd(findings[it["fingerprint"]]) or 0 for it in validos if it["estado"] == "seguro_ya")
    print(f"verificacion.json válido: {len(validos)} ítems "
          f"({', '.join(f'{k}={v}' for k, v in sorted(por_estado.items()))}); ahorro seguro ya ${seguro:,.2f}/mes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
