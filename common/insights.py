"""Contrato de insights.json: lo que la skill metodo-dive concluye al analizar una corrida, en datos que el
dashboard muestra y un script valida (nunca un texto libre).

Los usa la skill (skill/scripts/insights.py: valida, calcula los totales y exporta informe-finops.md) y el
dashboard (5-dashboard/base.py: recalcula todo desde insights.json, no confía en ningún total escrito).

Reglas que hacen mecánico el "no inventar":
- El dinero y los porcentajes no se escriben en `titulo`/`texto`: van como {{c1}} y `cifras.c1.fuente` dice de
  dónde sale (un finding, discovery.json, una evidencia, un total). El script lee el valor de la fuente.
- Una evidencia de AWS (`origen: "aws"`) apunta a la salida cruda que guardó el runner (`salida_ref`, con hash);
  sus `valores` tienen que aparecer literalmente en esa salida.
- `seguro_ya` solo vale para ahorros que suman, con monto y de un tipo reversible.
- Un archivo con cualquier problema no se publica, ni a medias.
"""

import hashlib
import json
import math
import re

from common.verificacion import ESTADOS, RE_FINGERPRINT, REVERSIBILIDAD, monto, reversibilidad

TIPOS = ("hallazgo", "causa_costo", "recomendacion", "pregunta", "laguna")
ESTADOS_INSIGHT = tuple(ESTADOS) + ("informativo",)
NIVELES = ("organizacion", "cuenta", "region", "recurso")
FORMATOS = ("usd", "pct", "num")
REVERSIBILIDADES = tuple(sorted(set(REVERSIBILIDAD.values())))
MAX_ARCHIVO = 2_000_000  # bytes
MAX_INSIGHTS = 200
MAX_EVIDENCIAS = 20

CLAVES_INSIGHT = {"id", "tipo", "alcance", "estado", "prioridad", "titulo", "texto", "cifras", "evidencia",
                  "no_determinado", "pregunta_dueno", "accion"}
LARGOS = {"titulo": 160, "texto": 1500, "no_determinado": 800, "pregunta_dueno": 500}
RE_ID = re.compile(r"I\d{2,3}")
RE_CIFRA = re.compile(r"c\d{1,3}")
RE_EVIDENCIA = re.compile(r"E\d{1,3}")
RE_PLACEHOLDER = re.compile(r"\{\{(c\d{1,3})\}\}")
RE_REF_AWS = re.compile(r"A\d{3}\.json")  # lo que numera el runner
RE_CLAVE_VALOR = re.compile(r"[a-z][a-z0-9_]{0,40}")
RE_CUENTA = re.compile(r"[0-9]{12}")
RE_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]|global")
# Dinero y porcentajes sueltos: tienen que ir como placeholder (para convertirse con la moneda y no inventarse).
_NUMEROS = ("un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|trece|catorce|quince|veinte|treinta|"
            "cuarenta|cincuenta|sesenta|setenta|ochenta|noventa|cien|ciento|doscientos|trescientos|quinientos|mil|"
            "mill[oó]n|millones")
_MONEDAS = r"d[oó]lar(?:es)?|euros?|reais|reales|libras?|USD|EUR|GBP|BRL"
RE_DINERO = re.compile(
    r"[$€£]\s?\d"                                                     # $12, €5
    r"|\d[\d.,]*\s?[$€£]"                                             # 12€
    r"|\d[\d.,]*\s?(?:" + _MONEDAS + r")\b"                           # 12 USD, 12 dólares, 9,5 euros
    r"|\b(?:" + _NUMEROS + r")\b[\s\d.,y]*(?:" + _MONEDAS + r")\b"     # once mil dólares, cinco euros
    r"|\d\s?%|\d[\d.,]*\s?por ciento"                                # 94%, 94 por ciento
    r"|\b(?:" + _NUMEROS + r")\b\s+por ciento"
    r"|\b(?:USD|EUR|GBP|BRL)\s?\d"                                 # USD 12
    r"|\d[\d.,]*\s?(?:k|mil)\b", re.IGNORECASE)                    # 1.2k, 12 mil


def _numero(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _texto(v, largo):
    return isinstance(v, str) and 0 < len(v.strip()) and len(v) <= largo


def _r(v):
    """Un valor del archivo, acotado, para citarlo en un problema (puede venir de un insights.json de 2 MB)."""
    return repr(v)[:60]


# Una acción es un comando para una persona: solo `aws --profile '<perfil-con-escritura>' …` con valores entre
# comillas simples (sin pipes, redirecciones ni sustituciones). Una línea por comando.
_VALOR_ACCION = r"'(?:[A-Za-z0-9._:/@+=# \-]|<[a-zA-Z-]+>)*'"
RE_LINEA_ACCION = re.compile(r"aws --profile '<perfil-con-escritura>'(?: --region '[a-z0-9-]+')? [a-z0-9-]+ [a-z0-9-]+"
                             r"(?: --[a-z0-9-]+(?: " + _VALOR_ACCION + r")?)*")
RE_PELIGROSO = re.compile(r"[;|&$`<>\\\n\r]")
RE_MARKDOWN_EXTERNO = re.compile(r"!\[|\]\(")  # imágenes y enlaces: el .md los usaría para pedir cosas afuera


def _accion_ok(comando):
    lineas = [x for x in comando.split("\n") if x.strip()]
    return 0 < len(lineas) <= 6 and all(RE_LINEA_ACCION.fullmatch(x.strip()) for x in lineas)


def _hojas(o, nivel=0):
    if nivel > 12:
        return
    if isinstance(o, dict):
        for v in o.values():
            yield from _hojas(v, nivel + 1)
    elif isinstance(o, list):
        for v in o:
            yield from _hojas(v, nivel + 1)
    else:
        yield o


def _valor_en_salida(v, salida):
    """Un número citado tiene que ser una hoja de la salida (no un pedazo de otro número ni de una fecha)."""
    try:
        datos = json.loads(salida)
    except ValueError:  # salida truncada: sin JSON entero, se busca como token
        return re.search(r"(?<![\w.])" + re.escape(f"{v:g}") + r"(?![\w])", salida) is not None
    for h in _hojas(datos):
        if _numero(h) and h == v:
            return True
        if isinstance(h, str):
            try:
                if float(h) == v:
                    return True
            except ValueError:
                pass
    return False


# ---------- totales ----------

def _fps(insight):
    """Los fingerprints de un insight, solo si el archivo trae la forma esperada (lista de textos)."""
    a = insight.get("alcance") if isinstance(insight, dict) else None
    fps = a.get("fingerprints") if isinstance(a, dict) else None
    return [x for x in fps if isinstance(x, str)] if isinstance(fps, list) else []


def totales(findings, insights):
    """Totales de la corrida, calculados aquí: nadie los suma a mano.
    findings = {fingerprint: finding}; insights = lista (con `estado` y `alcance.fingerprints`)."""
    ahorro = {fp: f for fp, f in findings.items() if f.get("is_saving") and monto(f)}
    por_rev = {}
    for f in ahorro.values():
        r = reversibilidad(f)
        por_rev[r] = round(por_rev.get(r, 0) + monto(f), 2)
    seguros = {fp for i in insights if i.get("estado") == "seguro_ya" for fp in _fps(i) if fp in ahorro}
    return {
        "maximo_teorico": round(sum(monto(f) for f in ahorro.values()), 2),
        "seguro_ya": round(sum(monto(ahorro[fp]) for fp in seguros), 2),
        "por_reversibilidad": por_rev,
        "gasto_informativo": round(sum(monto(f) for f in findings.values()
                                       if not f.get("is_saving") and monto(f)
                                       and f.get("category") in ("costo", "desperdicio")), 2),
    }


# ---------- fuentes de las cifras ----------

def _camino(datos, ruta):
    actual = datos
    for parte in ruta.split("."):
        if isinstance(actual, dict) and parte in actual:
            actual = actual[parte]
        elif isinstance(actual, list) and parte.isdigit() and int(parte) < len(actual):
            actual = actual[int(parte)]
        else:
            return None
    return actual


def resolver_fuente(fuente, ctx, tot):
    """→ (valor, None) o (None, motivo). Fuentes:
    finding:<fp>.monthly_usd · suma:<fp>,<fp>.monthly_usd · discovery:<ruta.punteada> ·
    evidencia:<E1>.<clave> · total:maximo_teorico|seguro_ya|gasto_informativo"""
    if not isinstance(fuente, str) or len(fuente) > 400:
        return None, "fuente inválida"
    tipo, _, resto = fuente.partition(":")
    if tipo == "finding":
        fp, _, campo = resto.partition(".")
        f = ctx["findings"].get(fp)
        if f is None or campo != "monthly_usd":
            return None, f"{fuente}: finding inexistente o campo no permitido (solo monthly_usd)"
        v = monto(f)
        return (v, None) if v else (None, f"{fuente}: el finding no tiene monto")
    if tipo == "suma":
        lista, _, campo = resto.rpartition(".")
        fps = lista.split(",")
        if campo != "monthly_usd" or not fps or len(fps) > 100:
            return None, f"{fuente}: formato suma:<fp>,<fp>.monthly_usd"
        if len(set(fps)) != len(fps):
            return None, f"{fuente}: fingerprints repetidos (contarían dos veces)"
        valores = [monto(ctx["findings"].get(fp) or {}) for fp in fps]
        if not all(valores):
            return None, f"{fuente}: algún finding no existe o no tiene monto"
        return round(sum(valores), 4), None
    if tipo == "discovery":
        v = _camino(ctx.get("discovery") or {}, resto)
        return (v, None) if _numero(v) else (None, f"{fuente}: no es un número en discovery.json")
    if tipo == "evidencia":
        eid, _, clave = resto.partition(".")
        valores = ((ctx.get("evidencias") or {}).get(eid) or {}).get("valores")
        v = valores.get(clave) if isinstance(valores, dict) else None
        return (v, None) if _numero(v) else (None, f"{fuente}: la evidencia no tiene ese valor numérico")
    if tipo == "total":
        v = tot.get(resto) if resto in ("maximo_teorico", "seguro_ya", "gasto_informativo") else None
        return (v, None) if _numero(v) else (None, f"{fuente}: total desconocido")
    return None, f"{fuente}: tipo de fuente desconocido"


def formatear(valor, formato):
    if formato == "usd":
        return f"${valor:,.2f}"
    if formato == "pct":
        return f"{valor:.1f}%"
    return format(valor, ",.15g")


def renderizar(texto, cifras, valores):
    """texto con {{c1}} → texto con los valores de las fuentes, formateados (para el export .md, en USD)."""
    return RE_PLACEHOLDER.sub(lambda m: formatear(valores[m.group(1)], cifras[m.group(1)]["formato"])
                              if m.group(1) in valores else m.group(0), texto)


# ---------- validación ----------

def _evidencias(items, ctx, problemas):
    """Valida y junta las evidencias de todos los insights ({id: evidencia}); los ids son únicos en el archivo."""
    vistas = {}
    for n, it in enumerate(items):
        ev = it.get("evidencia") if isinstance(it, dict) else None
        if ev is None:
            continue
        if not isinstance(ev, list) or len(ev) > MAX_EVIDENCIAS:
            problemas.append(f"insights[{n}].evidencia debe ser una lista de hasta {MAX_EVIDENCIAS}")
            continue
        for m, e in enumerate(ev):
            donde = f"insights[{n}].evidencia[{m}]"
            if not isinstance(e, dict) or set(e) - {"id", "origen", "descripcion", "comando", "salida_ref", "valores"}:
                problemas.append(f"{donde}: no es un objeto con las claves permitidas")
                continue
            if not isinstance(e.get("id"), str) or not RE_EVIDENCIA.fullmatch(e["id"]) or e["id"] in vistas:
                problemas.append(f"{donde}: id inválido o repetido ({_r(e.get('id'))})")
                continue
            if e.get("origen") not in ("dive", "aws") or not _texto(e.get("descripcion"), 500):
                problemas.append(f"{donde}: origen (dive|aws) y descripcion (hasta 500) son obligatorios")
                continue
            if e.get("comando") is not None and (not _texto(e["comando"], 800) or RE_PELIGROSO.search(e["comando"])):
                problemas.append(f"{donde}: comando debe ser texto de hasta 800 caracteres, sin ; | & $ ` < > \\ ni saltos de línea")
                continue
            valores = e.get("valores")
            if valores is not None and (not isinstance(valores, dict) or len(valores) > 20 or not all(
                    isinstance(k, str) and RE_CLAVE_VALOR.fullmatch(k) and _numero(v) for k, v in valores.items())):
                problemas.append(f"{donde}: valores debe ser {{clave: número}} (hasta 20)")
                continue
            if e["origen"] == "aws":
                ref = e.get("salida_ref")
                crudo = ctx["aws"](ref) if isinstance(ref, str) and RE_REF_AWS.fullmatch(ref) and ctx.get("aws") else None
                if not isinstance(crudo, dict) or not isinstance(crudo.get("salida"), str):
                    problemas.append(f"{donde}: salida_ref {_r(ref)} no existe entre las salidas guardadas por el runner (A001.json…)")
                    continue
                if hashlib.sha256(crudo["salida"].encode()).hexdigest() != crudo.get("sha256"):
                    problemas.append(f"{donde}: la salida guardada no coincide con su hash (fue modificada)")
                    continue
                falta = [k for k, v in (valores or {}).items() if not _valor_en_salida(v, crudo["salida"])]
                if falta:
                    problemas.append(f"{donde}: los valores {falta} no son un valor de la salida guardada")
                    continue
            elif e.get("salida_ref") or valores:
                problemas.append(f"{donde}: salida_ref y valores solo valen con origen aws")
                continue
            vistas[e["id"]] = e
    return vistas


def _alcance(a, donde, ctx, problemas):
    if not isinstance(a, dict) or set(a) - {"nivel", "cuenta", "region", "fingerprints"} or a.get("nivel") not in NIVELES:
        problemas.append(f"{donde}.alcance: nivel debe ser uno de {NIVELES}, con cuenta/region/fingerprints")
        return False
    ok = True
    if a.get("cuenta") is not None and (not isinstance(a["cuenta"], str) or not RE_CUENTA.fullmatch(a["cuenta"])
                                        or a["cuenta"] not in ctx["cuentas"]):
        problemas.append(f"{donde}.alcance.cuenta {_r(a.get('cuenta'))} no es una cuenta de esta corrida")
        ok = False
    if a.get("region") is not None and (not isinstance(a["region"], str) or not RE_REGION.fullmatch(a["region"])):
        problemas.append(f"{donde}.alcance.region inválida")
        ok = False
    fps = a.get("fingerprints")
    if fps is not None and (not isinstance(fps, list) or len(fps) > 100 or not all(
            isinstance(x, str) and RE_FINGERPRINT.fullmatch(x) and x in ctx["findings"] for x in fps)):
        problemas.append(f"{donde}.alcance.fingerprints: cada uno debe ser un fingerprint de esta corrida")
        ok = False
    if a["nivel"] == "cuenta" and not a.get("cuenta") or a["nivel"] == "region" and not (a.get("cuenta") or a.get("region")) \
            or a["nivel"] == "recurso" and not fps:
        problemas.append(f"{donde}.alcance: el nivel {a['nivel']} necesita "
                         f"{'fingerprints' if a['nivel'] == 'recurso' else 'cuenta/region'}")
        ok = False
    return ok


def revisar(datos, run_id, ctx):
    """Valida insights.json ya parseado. ctx = {"findings": {fp: finding}, "discovery": dict, "cuentas": set,
    "aws": callable(ref) → dict|None (salida cruda del runner)}.
    → (insights con `valores` resueltos, problemas). Con cualquier problema el archivo no se publica."""
    if not isinstance(datos, dict):
        return [], ["insights.json falta, está roto o no es un objeto"]
    problemas = []
    if datos.get("run_id") != run_id:
        problemas.append(f"run_id {_r(datos.get('run_id'))} no es el de findings.json ({run_id!r})")
    items = datos.get("insights")
    if not isinstance(items, list):
        return [], problemas + ["insights debe ser una lista"]
    if len(items) > MAX_INSIGHTS:
        problemas.append(f"hay más de {MAX_INSIGHTS} insights")
        items = items[:MAX_INSIGHTS]
    ctx = {**ctx, "evidencias": _evidencias(items, ctx, problemas)}
    tot = totales(ctx["findings"], [i for i in items if isinstance(i, dict)])
    validos, ids = [], set()
    for n, it in enumerate(items):
        donde = f"insights[{n}]"
        if not isinstance(it, dict) or set(it) - CLAVES_INSIGHT:
            problemas.append(f"{donde}: no es un objeto o trae claves desconocidas "
                             f"({sorted(set(it) - CLAVES_INSIGHT) if isinstance(it, dict) else ''})")
            continue
        antes = len(problemas)
        if not isinstance(it.get("id"), str) or not RE_ID.fullmatch(it["id"]) or it["id"] in ids:
            problemas.append(f"{donde}: id inválido o repetido ({_r(it.get('id'))})")
        else:
            ids.add(it["id"])
        if it.get("tipo") not in TIPOS:
            problemas.append(f"{donde}: tipo debe ser uno de {TIPOS}")
        estado = it.get("estado", "informativo")
        if estado not in ESTADOS_INSIGHT:
            problemas.append(f"{donde}: estado debe ser uno de {ESTADOS_INSIGHT}")
        if not isinstance(it.get("prioridad"), int) or isinstance(it.get("prioridad"), bool) \
                or not 1 <= it["prioridad"] <= 99:
            problemas.append(f"{donde}: prioridad debe ser un entero de 1 a 99 (1 = lo primero)")
        for campo, largo in LARGOS.items():
            if (campo in ("titulo", "texto") or it.get(campo) is not None) and not _texto(it.get(campo), largo):
                problemas.append(f"{donde}: {campo} debe ser texto de hasta {largo} caracteres")
        alcance_ok = _alcance(it.get("alcance"), donde, ctx, problemas)
        # cifras: cada una con fuente que resuelve; los placeholders del texto existen
        cifras, valores = it.get("cifras") or {}, {}
        if not isinstance(cifras, dict) or len(cifras) > 30:
            problemas.append(f"{donde}: cifras debe ser un objeto de hasta 30 entradas")
            cifras = {}
        for c, d in cifras.items():
            if not isinstance(c, str) or not RE_CIFRA.fullmatch(c) or not isinstance(d, dict) \
                    or set(d) - {"fuente", "formato"} or d.get("formato") not in FORMATOS:
                problemas.append(f"{donde}.cifras[{c!r}]: debe ser {{fuente, formato: {'|'.join(FORMATOS)}}}")
                continue
            v, error = resolver_fuente(d.get("fuente"), ctx, tot)
            tipo_fuente = str(d.get("fuente")).partition(":")[0]
            if not error and (tipo_fuente in ("finding", "suma", "total") and d["formato"] != "usd"
                              or tipo_fuente == "evidencia" and d["formato"] == "usd"):
                error = f"formato {d['formato']} no vale con una fuente {tipo_fuente} (finding, suma y total son usd; evidencia nunca)"
            if error:
                problemas.append(f"{donde}.cifras.{c}: {error}")
            else:
                valores[c] = v
        for campo in ("titulo", "texto"):
            t = it.get(campo)
            if not isinstance(t, str):
                continue
            for c in RE_PLACEHOLDER.findall(t):
                if c not in cifras:
                    problemas.append(f"{donde}.{campo}: {{{{{c}}}}} no está definido en cifras")
            if RE_MARKDOWN_EXTERNO.search(t):
                problemas.append(f"{donde}.{campo}: sin imágenes ni enlaces de Markdown")
            suelto = RE_DINERO.search(RE_PLACEHOLDER.sub("", t))
            if suelto:
                problemas.append(f"{donde}.{campo}: dinero o porcentaje escrito a mano ({suelto.group(0)!r}); "
                                 "usa un {{cN}} con su fuente")
        if estado == "seguro_ya":
            fps = (it.get("alcance") or {}).get("fingerprints") if alcance_ok else None
            if it.get("tipo") not in ("hallazgo", "recomendacion") or not fps:
                problemas.append(f"{donde}: seguro_ya solo en un hallazgo/recomendación con fingerprints")
            else:
                for fp in fps:
                    f = ctx["findings"][fp]
                    if not (f.get("is_saving") and monto(f) and reversibilidad(f) == "reversible"):
                        problemas.append(f"{donde}: {f.get('id') or fp} no puede ser seguro_ya (suma="
                                         f"{bool(f.get('is_saving'))}, reversibilidad={reversibilidad(f)}); usa firma")
        accion = it.get("accion")
        if accion is not None and (not isinstance(accion, dict) or set(accion) - {"comando", "reversibilidad"}
                                   or not _texto(accion.get("comando"), 800) or not _accion_ok(accion["comando"])
                                   or accion.get("reversibilidad") not in (None, *REVERSIBILIDADES)):
            problemas.append(f"{donde}.accion: debe ser {{comando, reversibilidad?}}; el comando, solo `aws --profile "
                             "'<perfil-con-escritura>' …` con valores entre comillas simples, una línea por comando (hasta 6)")
        if len(problemas) == antes:
            validos.append({**it, "estado": estado, "valores": valores})
    # Un recurso no puede ser "seguro ya" para un insight y firma/contradicho/no verificado para otro.
    por_fp = {}
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            for fp in _fps(it):
                por_fp.setdefault(fp, {})[it["id"]] = it.get("estado", "informativo")
    for fp, estados in sorted(por_fp.items()):
        if "seguro_ya" in estados.values() and set(estados.values()) & {"firma", "contradicho", "no_verificado"}:
            problemas.append(f"el fingerprint {fp} está en insights con estados que se contradicen: {dict(sorted(estados.items()))}")
    return validos, problemas
