"""Reglas compartidas de la verificación de la skill metodo-dive (verificacion.json): la reversibilidad de cada
tipo de problema y la validación de cada ítem.

Las usan el validador de la skill (skill/scripts/verificacion.py), su resumen (skill/scripts/resumen.py) y el
dashboard (5-dashboard/base.py). Así el panel nunca muestra como "seguro ya" algo que el validador rechazaría,
aunque el archivo no se haya validado.
"""

import json
import math
import os
import re
import stat

# Qué implica actuar sobre cada problema (por issue_id canónico o rule_id). Valores:
#   reversible   se puede deshacer (snapshot antes de borrar, volver a cambiar un tipo): candidato a "ahorro seguro ya"
#   reinicio     con corte (parar la instancia, cambiar la familia o el tamaño): puede perder la IPv4 o el instance store
#   irreversible no se deshace (soltar una IP, borrar un snapshot, un LB o un secreto): necesita la firma del dueño
#   compromiso   compra de Savings Plans o reservas: plazo de 1-3 años, firma del dueño
#   informativo  gasto actual o riesgo: no es una acción de ahorro
REVERSIBILIDAD = {
    # canónicos (ver 3-validar/hallazgos.ISSUE_CANONICO)
    "ebs.unattached": "reversible",
    "eip.unassociated": "irreversible",
    "ebs_snapshot.old": "irreversible",
    "ec2.stopped": "irreversible",
    "root.no_mfa": "informativo",
    "aws-doctor.unused_load_balancers": "irreversible",
    "aws-doctor.cloudwatch_log_groups_without_retention_policy": "informativo",
    # reglas
    "regla.gp2": "reversible",
    "regla.rds_gp2": "reversible",
    "regla.s3_kms_sin_bucket_key": "reversible",
    "regla.nat_sin_endpoint": "reversible",
    "regla.lambda_x86": "reinicio",
    "regla.ec2_generacion_antigua": "reinicio",
    "regla.ipv4_publica": "informativo",
    "regla.sg_abierto": "informativo",
    "regla.rds_publico": "informativo",
    "regla.rds_sin_cifrar": "informativo",
    "regla.opensearch_abierto": "informativo",
    "regla.s3_sin_bloqueo_publico": "informativo",
    "regla.sin_tags": "informativo",
    "regla.root_access_keys": "informativo",
    "regla.sin_budget": "informativo",
    # aws-doctor
    # Suele ser el volumen raíz: borrarlo deja la instancia sin arrancar (la instancia es irreversible también).
    "aws-doctor.stopped_instance_volumes": "irreversible",
    "aws-doctor.reserved_instances": "compromiso",
    "aws-doctor.unused_amis": "irreversible",
    "aws-doctor.unused_key_pairs": "informativo",
    "aws-doctor.s3_buckets_without_lifecycle": "irreversible",
    "aws-doctor.s3_buckets_with_incomplete_multipart_uploads": "irreversible",
    "aws-doctor.stopped_rds_instances": "irreversible",
    "aws-doctor.old_rds_snapshots": "irreversible",
    "aws-doctor.idle_rds_instances": "reinicio",
    "aws-doctor.idle_load_balancers": "irreversible",
    "aws-doctor.idle_nat_gateways": "irreversible",
    "aws-doctor.idle_ec2_instances": "reinicio",
    "aws-doctor.idle_sagemaker_endpoints": "irreversible",
    "aws-doctor.over_provisioned_lambdas": "reversible",
    "aws-doctor.ecr_untagged_images": "irreversible",
    "aws-doctor.ecr_no_lifecycle_policies": "irreversible",
    "aws-doctor.ecr_empty_repositories": "informativo",
    # Recuperable solo 7-30 días (y sin --force-delete-without-recovery), y un consumidor esporádico se rompe.
    "aws-doctor.unused_secrets": "irreversible",
    "aws-doctor.unused_iam_users": "informativo",
    # cruce billing ↔ inventario
    "cruce.dejo_de_cobrarse": "informativo",
    "cruce.no_inventariado": "informativo",
    "cruce.region_sorpresa": "informativo",
    "cruce.sin_colector": "informativo",
    # compromisos y guardarraíles
    "finops.savings_plans_sin_usar": "informativo",
    "finops.savings_plan_vence": "compromiso",
    "finops.cobertura_baja": "compromiso",
    "finops.reservas_sin_usar": "informativo",
    "finops.sin_anomalias": "informativo",
    "finops.anomalias_sin_suscripcion": "informativo",
    "finops.coh_inactivo": "informativo",
}
# Cost Optimization Hub: por acción (rule_id cost-optimization-hub.<acción>.<tipo>[.sin_ahorro]).
REVERSIBILIDAD_COH = {"delete": "irreversible", "stop": "reinicio", "rightsize": "reinicio",
                      "migratetograviton": "reinicio", "upgrade": "reinicio", "scalein": "reinicio",
                      "purchasesavingsplans": "compromiso", "purchasereservedinstances": "compromiso"}
DESCONOCIDA = "irreversible"  # lo que no está en la tabla nunca cuenta como seguro


def reversibilidad(f):
    for clave in (f.get("issue_id"), f.get("rule_id")):
        if clave in REVERSIBILIDAD:
            return REVERSIBILIDAD[clave]
    rid = f.get("rule_id") or ""
    if rid.startswith("cost-optimization-hub."):
        return REVERSIBILIDAD_COH.get(rid.split(".")[1], DESCONOCIDA)
    return DESCONOCIDA


def monto(f):
    """Monto mensual positivo y finito, o None."""
    v = f.get("monthly_usd")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 else None


ESTADOS = {
    "seguro_ya": "pasó la pasada escéptica; reversible: se puede hacer ya",
    "firma": "verificado, pero necesita la decisión del dueño (irreversible, compromiso, corte o se guarda a propósito)",
    "contradicho": "la verificación contradice el finding (ya no existe, tiene uso, otro precio)",
    "no_verificado": "no se pudo verificar (sin permiso, sin datos)",
}
RE_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
LARGOS = {"motivo": 500, "pregunta_dueno": 500, "evidencia": 2000, "created_by": 200, "owner": 200, "id": 16}
MAX_ARCHIVO = 2_000_000  # bytes de verificacion.json


def revisar(datos, run_id, findings_por_fp):
    """Valida verificacion.json ya parseado contra los findings de su corrida ({fingerprint: finding}).
    → (ítems válidos, problemas). Un archivo con cualquier problema no se publica: ni el validador ni el panel
    lo aceptan a medias."""
    if not isinstance(datos, dict):
        return [], ["verificacion.json falta, está roto o no es un objeto"]
    problemas = []
    if datos.get("run_id") != run_id:
        problemas.append(f"run_id {datos.get('run_id')!r} no es el de findings.json ({run_id!r})")
    items = datos.get("items")
    if not isinstance(items, list):
        return [], problemas + ["items debe ser una lista"]
    vistos, validos = set(), []
    for n, it in enumerate(items):
        donde = f"items[{n}]"
        if not isinstance(it, dict):
            problemas.append(f"{donde}: no es un objeto")
            continue
        fp = it.get("fingerprint")
        if not isinstance(fp, str) or not RE_FINGERPRINT.fullmatch(fp):
            problemas.append(f"{donde}: fingerprint inválido {fp!r}")
            continue
        if fp in vistos:
            problemas.append(f"{donde}: fingerprint {fp} repetido")
            continue
        vistos.add(fp)
        f = findings_por_fp.get(fp)
        if f is None:
            problemas.append(f"{donde}: fingerprint {fp} no está en findings.json de esta corrida")
            continue
        estado = it.get("estado")
        if estado not in ESTADOS:
            problemas.append(f"{donde}: estado {estado!r} no es uno de {sorted(ESTADOS)}")
            continue
        if estado == "seguro_ya" and not (f.get("is_saving") and monto(f) and reversibilidad(f) == "reversible"):
            problemas.append(f"{donde}: {f.get('id')} no puede ser seguro_ya (is_saving={bool(f.get('is_saving'))}, "
                             f"reversibilidad={reversibilidad(f)}); usa firma")
            continue
        malo = next((c for c, largo in LARGOS.items() if it.get(c) is not None
                     and (not isinstance(it.get(c), str) or len(it[c]) > largo)), None)
        if malo:
            problemas.append(f"{donde}: {malo} debe ser texto de hasta {LARGOS[malo]} caracteres")
            continue
        if not (it.get("motivo") or "").strip():
            problemas.append(f"{donde}: falta motivo")
            continue
        validos.append(it)
    return validos, problemas


_FLAGS_ARCHIVO = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_FLAGS_CARPETA = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _abrir_en_carpeta(carpeta, nombre):
    """(fd, stat) de `carpeta/nombre` si es un archivo regular sin links, o (None, None). `nombre` es un nombre
    simple (sin "/" ni ".."): solo se abre lo que está directamente en la carpeta."""
    if not nombre or os.sep in nombre or (os.altsep and os.altsep in nombre) or nombre in (".", ".."):
        return None, None
    try:
        dfd = os.open(carpeta, _FLAGS_CARPETA)
    except OSError:
        return None, None
    try:
        fd = os.open(nombre, _FLAGS_ARCHIVO, dir_fd=dfd)
    except (OSError, ValueError):
        return None, None
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None, None
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        os.close(fd)
        return None, None
    return fd, st


def existe_en_carpeta(carpeta, nombre, maximo):
    """¿Hay en la carpeta un archivo regular, sin links, de hasta `maximo` bytes? Sin leerlo."""
    fd, st = _abrir_en_carpeta(carpeta, nombre)
    if fd is None:
        return False
    os.close(fd)
    return 0 < st.st_size <= maximo


def leer_en_carpeta(carpeta, nombre, maximo):
    """Bytes de `carpeta/nombre`, o None. Ni la carpeta ni el archivo pueden ser symlinks (se abre la carpeta y
    después el archivo relativo a ella: cambiar la carpeta por un link después de la ingesta no redirige la
    lectura). Rechaza hardlinks, FIFOs y todo lo que no sea un archivo regular (O_NONBLOCK: un FIFO no bloquea),
    lo que pase de `maximo` y un archivo que cambió de tamaño mientras se leía (estaba a medio escribir): todo
    sobre el mismo descriptor, sin carrera entre comprobar y leer."""
    fd, st = _abrir_en_carpeta(carpeta, nombre)
    if fd is None:
        return None
    try:
        if st.st_size > maximo:
            return None
        partes, leido = [], 0
        while True:  # hasta EOF, en trozos acotados
            trozo = os.read(fd, min(1 << 20, maximo + 1 - leido))
            if not trozo:
                break
            partes.append(trozo)
            leido += len(trozo)
            if leido > maximo:
                return None
        return b"".join(partes) if leido == st.st_size else None
    except OSError:
        return None
    finally:
        os.close(fd)


def leer_archivo_seguro(ruta, maximo):
    """leer_en_carpeta para una ruta: la carpeta que la contiene tampoco puede ser un symlink."""
    ruta = os.fspath(ruta)
    return leer_en_carpeta(os.path.dirname(ruta) or ".", os.path.basename(ruta), maximo)


def _rechazar_constante(nombre):
    raise ValueError(f"valor no válido en el JSON: {nombre}")


def _flotante(texto):
    v = float(texto)
    if not math.isfinite(v):  # 1e999 → inf
        raise ValueError(f"número no finito en el JSON: {texto[:20]}")
    return v


def _entero(texto):
    if len(texto) > 30:  # un entero de miles de dígitos no es un dato de DIVE y desborda más adelante
        raise ValueError("entero demasiado grande en el JSON")
    return int(texto)


def cargar_json(crudo):
    """JSON de bytes o texto, rechazando NaN/Infinity, números no finitos (1e999), enteros gigantes y
    anidamientos patológicos (RecursionError → ValueError): un archivo escrito por un agente no puede tumbar a
    quien lo lee ni meter un Infinity en una suma."""
    try:
        return json.loads(crudo, parse_constant=_rechazar_constante, parse_float=_flotante, parse_int=_entero)
    except RecursionError as e:
        raise ValueError("JSON demasiado anidado") from e
