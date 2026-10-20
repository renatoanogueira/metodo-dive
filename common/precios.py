"""Precios on-demand por región desde la AWS Price List API (servicio `pricing`).

Las reglas no llaman a la API: validar.py pide una vez los precios que necesita el inventario, los guarda
en <corrida>/precios.json y se los pasa a las reglas (ctx["precios"]). Así las reglas siguen siendo
funciones puras, los tests no tocan AWS y cada corrida deja constancia de con qué precio calculó.

La API solo se sirve desde algunas regiones (us-east-1): el endpoint es us-east-1 y la región de los
precios va en el filtro `regionCode`. El prefijo del usagetype cambia por región ("EU-", "USE1-", o
ninguno en us-east-1), así que no se filtra por usagetype en la API: se elige el producto por su sufijo.

Uso: from common.precios import obtener_precios, precio
"""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from botocore.config import Config
from botocore.exceptions import (BotoCoreError, ClientError, ConnectionError as ErrorDeConexion,
                                 CredentialRetrievalError, NoCredentialsError, PartialCredentialsError,
                                 ReadTimeoutError, SSOError, TokenRetrievalError)

from common import error_corto

# clave → (service code, filtros exactos, sufijo del usagetype que identifica el producto, unidad esperada)
CONSULTAS = {
    "ebs_gb_mes:gp2": ("AmazonEC2", {"productFamily": "Storage", "volumeApiName": "gp2"}, "EBS:VolumeUsage.gp2", "GB-Mo"),
    "ebs_gb_mes:gp3": ("AmazonEC2", {"productFamily": "Storage", "volumeApiName": "gp3"}, "EBS:VolumeUsage.gp3", "GB-Mo"),
    "ebs_gb_mes:io1": ("AmazonEC2", {"productFamily": "Storage", "volumeApiName": "io1"}, "EBS:VolumeUsage.piops", "GB-Mo"),
    "ebs_gb_mes:io2": ("AmazonEC2", {"productFamily": "Storage", "volumeApiName": "io2"}, "EBS:VolumeUsage.io2", "GB-Mo"),
    "ebs_gb_mes:st1": ("AmazonEC2", {"productFamily": "Storage", "volumeApiName": "st1"}, "EBS:VolumeUsage.st1", "GB-Mo"),
    "ebs_gb_mes:sc1": ("AmazonEC2", {"productFamily": "Storage", "volumeApiName": "sc1"}, "EBS:VolumeUsage.sc1", "GB-Mo"),
    "ebs_gb_mes:standard": ("AmazonEC2", {"productFamily": "Storage", "volumeApiName": "standard"}, "EBS:VolumeUsage", "GB-Mo"),
    "snapshot_gb_mes": ("AmazonEC2", {"productFamily": "Storage Snapshot"}, "EBS:SnapshotUsage", "GB-Mo"),
    "ipv4_hora": ("AmazonVPC", {}, "PublicIPv4:InUseAddress", "Hrs"),
    "ipv4_ociosa_hora": ("AmazonVPC", {}, "PublicIPv4:IdleAddress", "Hrs"),
    "logs_gb_mes": ("AmazonCloudWatch", {}, "TimedStorage-ByteHrs", "GB-Mo"),
    "rds_gb_mes:gp2": ("AmazonRDS", {"productFamily": "Database Storage", "deploymentOption": "Single-AZ"}, "RDS:GP2-Storage", "GB-Mo"),
    "rds_gb_mes:gp3": ("AmazonRDS", {"productFamily": "Database Storage", "deploymentOption": "Single-AZ"}, "RDS:GP3-Storage", "GB-Mo"),
    "alb_hora": ("AWSELB", {"productFamily": "Load Balancer-Application"}, "LoadBalancerUsage", "Hrs"),
    "nlb_hora": ("AWSELB", {"productFamily": "Load Balancer-Network"}, "LoadBalancerUsage", "Hrs"),
}
HORAS_MES = 730  # la convención de AWS para pasar $/hora a $/mes
# Pocos reintentos y timeouts cortos: todas las claves van al mismo endpoint, así que si no responde no tiene
# sentido esperar minutos por cada una (los reintentos adaptativos de common.aws son para los colectores).
_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=20)
# La misma unidad aparece escrita de varias formas según el producto (io2 usa "GB-month").
UNIDADES = {"GB-Mo": {"GB-Mo", "GB-month"}, "Hrs": {"Hrs", "hours"}}


def _sufijo_ok(usagetype, sufijo):
    return usagetype == sufijo or usagetype.endswith("-" + sufijo)


def elegir(lista_precios, sufijo, unidad):
    """De las respuestas de get_products (JSON en texto), el precio on-demand del producto cuyo usagetype
    termina en `sufijo`. Devuelve {usd, unidad, descripcion, usagetype} o None. Función pura (testeable)."""
    candidatos, unidades = [], UNIDADES.get(unidad, {unidad})
    for texto in lista_precios:
        d = json.loads(texto) if isinstance(texto, str) else texto
        ut = (d.get("product", {}).get("attributes") or {}).get("usagetype") or ""
        if not _sufijo_ok(ut, sufijo):
            continue
        for termino in (d.get("terms", {}).get("OnDemand") or {}).values():
            for dim in (termino.get("priceDimensions") or {}).values():
                usd = float(dim.get("pricePerUnit", {}).get("USD") or 0)
                # El primer tramo (beginRange 0) es el precio de referencia; los tramos siguientes son descuentos.
                if usd > 0 and dim.get("unit") in unidades and str(dim.get("beginRange", "0")) == "0":
                    candidatos.append({"usd": usd, "unidad": unidad, "descripcion": dim.get("description", "")[:120],
                                       "usagetype": ut})
    if not candidatos:
        return None
    # El sufijo también lo tienen variantes del producto (EU-TS-LoadBalancerUsage, EU-Outposts-...): el
    # producto base es el de usagetype más corto (solo el prefijo de región, o ninguno en us-east-1).
    corto = min(len(c["usagetype"]) for c in candidatos)
    base = [c for c in candidatos if len(c["usagetype"]) == corto]
    # Varias filas del mismo producto (ej. RDS por motor) suelen tener el mismo precio: el menor es el
    # estimado conservador del ahorro.
    return min(base, key=lambda c: c["usd"])


def _consultar(pricing, clave, region):
    codigo, filtros, sufijo, unidad = CONSULTAS[clave]
    filtros = [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in {**filtros, "regionCode": region}.items()]
    paginas = pricing.get_paginator("get_products").paginate(ServiceCode=codigo, Filters=filtros)
    lista = [x for p in paginas for x in p.get("PriceList", [])]
    elegido = elegir(lista, sufijo, unidad)
    if elegido:
        elegido.update(fuente="pricing_api", consulta={"service": codigo, "filtros": {f["Field"]: f["Value"] for f in filtros},
                                                         "usagetype": sufijo})
    return elegido


def client(session, servicio, region):
    return session.client(servicio, region_name=region, config=_CONFIG)


# Errores que no dependen de la clave (credenciales, sesión SSO vencida, sin red, sin permiso): todas las
# consultas van al mismo endpoint con las mismas credenciales, así que se corta en el primero.
_ERRORES_GLOBALES = (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError, SSOError,
                     TokenRetrievalError, ErrorDeConexion, ReadTimeoutError)
_CODIGOS_GLOBALES = ("AccessDenied", "UnrecognizedClient", "ExpiredToken", "InvalidClientTokenId",
                     "UnauthorizedOperation", "InvalidSignature", "SignatureDoesNotMatch")


def _es_global(e):
    if isinstance(e, _ERRORES_GLOBALES):
        return True
    if isinstance(e, ClientError):
        codigo = (e.response.get("Error") or {}).get("Code") or ""
        return codigo.startswith(_CODIGOS_GLOBALES)
    return isinstance(e, BotoCoreError) and "credential" in type(e).__name__.lower()


_MAX_USD = 100  # ningún precio unitario consultado (GB-mes u hora) se acerca: más que esto es un dato roto
_CAMPOS = ("usd", "unidad", "descripcion", "usagetype", "fuente", "consulta")


def _valido(p):
    usd = p.get("usd") if isinstance(p, dict) else None
    return isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd) and 0 < usd < _MAX_USD


def _leer_cache(cache, hoy):
    """Precios guardados hoy; cualquier cosa rara (otro día, JSON roto o con otra forma) se ignora."""
    try:
        datos = json.loads(Path(cache).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(datos, dict) or datos.get("fecha") != hoy or not isinstance(datos.get("precios"), dict):
        return {}
    # Solo los campos conocidos (y la descripción como texto corto): el archivo es local pero editable.
    return {r: {k: {**{c: p[c] for c in _CAMPOS if c in p}, "descripcion": str(p.get("descripcion", ""))[:120]}
                for k, p in c.items() if _valido(p)}
            for r, c in datos["precios"].items() if isinstance(c, dict)}


def _escribir_cache(cache, hoy, precios):
    try:
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(cache).with_suffix(".tmp")
        tmp.write_text(json.dumps({"fecha": hoy, "precios": precios}, indent=2, ensure_ascii=False))
        os.replace(tmp, cache)
    except OSError:
        pass  # sin cache la próxima corrida vuelve a preguntar; no es motivo para fallar


def obtener_precios(session, pedidos, cache=None):
    """pedidos = {region: {claves}} → ({region: {clave: precio}}, errores). `cache` (opcional) es un archivo
    JSON con precios del mismo día: los precios cambian poco y la API es lenta. Nunca lanza por la API:
    un error queda en `errores` y la regla usa su precio de respaldo."""
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    guardado = _leer_cache(cache, hoy) if cache else {}
    precios, errores, pricing = {}, [], None
    for region, claves in sorted(pedidos.items()):
        for clave in sorted(claves):
            if clave not in CONSULTAS:
                continue
            p = (guardado.get(region) or {}).get(clave)
            if p is not None:
                p = {**p, "cache": hoy}  # el precio es de la API, leído de la cache del día (queda a la vista)
            else:
                try:
                    pricing = pricing or client(session, "pricing", "us-east-1")
                    p = _consultar(pricing, clave, region)
                except Exception as e:  # noqa: BLE001 — sin precio, la regla usa su estimado estático
                    errores.append({"region": region, "collector": f"precios/{clave}", "error": error_corto(e)})
                    if _es_global(e):  # no seguir repitiendo el mismo error en cada clave
                        return _con_cache(cache, hoy, guardado, precios), errores
                    continue
            if _valido(p):
                precios.setdefault(region, {})[clave] = p
    return _con_cache(cache, hoy, guardado, precios), errores


def _con_cache(cache, hoy, guardado, precios):
    if cache and precios:
        todos = {r: {**(guardado.get(r) or {}),
                     **{k: {c: p[c] for c in _CAMPOS if c in p} for k, p in (precios.get(r) or {}).items()}}
                 for r in set(guardado) | set(precios)}
        _escribir_cache(cache, hoy, todos)
    return precios


def precio(precios, region, clave, estatico=None):
    """(usd por unidad, fuente) para una región: el de la API si está, si no el estático (fuente
    static_estimate), si no (None, None)."""
    p = ((precios or {}).get(region) or {}).get(clave)
    if _valido(p):
        return p["usd"], "pricing_api"
    return (estatico, "static_estimate") if estatico is not None else (None, None)
