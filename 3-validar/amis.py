"""AMIs "sin uso" de aws-doctor, refinadas con lo que él no mira.

aws-doctor marca una AMI como sin uso cuando ninguna instancia que existe ahora la tiene y su creación pasó el
umbral de antigüedad. No mira `LastLaunchedTime`, así que una AMI lanzada esta mañana (y cuya instancia ya
terminó) sale como desperdicio. Aquí cada una se clasifica así:

- **AWS Backup** (nombre `AwsBackup_…`): la retención del vault la retira, no `deregister-image`. No suma.
- **Lanzada hace menos de DIAS_SIN_USO días**: uso puntual (runners, restores, launch templates). No suma.
- **Sin lanzamientos registrados o sin lanzar desde hace más de DIAS_SIN_USO días**: candidata. Una versión vieja de una AMI
  que ya tiene sucesora cae aquí tras ese plazo.
- **Sin lectura** (AccessDenied, red): se queda como la dejó aws-doctor, con confianza baja.

Una consulta de solo lectura por AMI (`ec2:DescribeImageAttribute`, sin costo). Nunca aborta la fase.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from hallazgos import parsear_fecha

REGLA = "aws-doctor.unused_amis"
DIAS_SIN_USO = 30
PREFIJO_BACKUP = "AwsBackup_"
_RE_NOMBRE = re.compile(r"(?:^|, )name=([^,]*)")
_SUFIJO = "(verificar ASG/launch templates)"


def _nombre(detail):
    m = _RE_NOMBRE.search(detail or "")
    return m.group(1).strip() if m else ""


def clasificar(nombre, ultimo, leido, ahora, dias=DIAS_SIN_USO):
    """→ (clase, días desde el último lanzamiento o None). Clases: backup, reciente, antigua, nunca, sin_lectura."""
    if nombre.startswith(PREFIJO_BACKUP):
        return "backup", None
    if not leido:
        return "sin_lectura", None
    if ultimo is None:
        return "nunca", None
    transcurridos = max(0, (ahora - ultimo).days)
    return ("reciente" if transcurridos < dias else "antigua"), transcurridos


def _aplicar(f, clase, ultimo, dias):
    """Ajusta el finding in situ. El rule_id (y con él el fingerprint) no cambia."""
    marca = ultimo.strftime("%Y-%m-%d") if ultimo else None
    if clase == "backup":
        f["title"] = "AMI de AWS Backup (la retención del vault la retira)"
        f["detail"] = (f.get("detail") or "") + ", gestionada=aws_backup"
        f["is_saving"] = False
    elif clase == "reciente":
        f["title"] = f"AMI lanzada hace {dias} días (uso puntual)"
        f["detail"] = (f.get("detail") or "") + f", last_launched={marca}"
        f["is_saving"] = False
    elif clase == "antigua":
        f["title"] = f"AMI sin lanzar hace {dias} días {_SUFIJO}"
        f["detail"] = (f.get("detail") or "") + f", last_launched={marca}"
    elif clase == "nunca":
        # EC2 no registra lanzamientos anteriores a ese atributo: "sin registros" no prueba que nunca se usó.
        f["title"] = f"AMI sin lanzamientos registrados {_SUFIJO}"
        f["detail"] = (f.get("detail") or "") + ", last_launched=sin registros"
    else:
        f["detail"] = (f.get("detail") or "") + ", last_launched=no verificado"
        f["confidence"] = "baja"


def lector_aws(cfg, cuentas):
    """Lector real: (cuenta, región, ami) → datetime UTC o None si nunca se lanzó. Lanza si falla la consulta."""
    from common.aws import client, sesion_cuenta, sesion_management
    mgmt = sesion_management(cfg) if cfg["access_mode"] != "sso" else None
    sesiones, clientes = {}, {}
    for cuenta, region in cuentas:  # los clientes se crean aquí, en el hilo principal
        if cuenta not in sesiones:
            sesiones[cuenta] = sesion_cuenta(cfg, cuenta, mgmt)
        clientes[(cuenta, region)] = client(sesiones[cuenta], "ec2", region)

    def leer(cuenta, region, ami):
        r = clientes[(cuenta, region)].describe_image_attribute(ImageId=ami, Attribute="lastLaunchedTime")
        valor = (r.get("LastLaunchedTime") or {}).get("Value")
        if not valor:
            return None
        fecha = parsear_fecha(valor)
        if fecha is None:
            raise ValueError("LastLaunchedTime con un formato que no se entiende")  # → sin lectura, no "sin registros"
        return fecha
    return leer


def refinar(cfg, findings, errores, lector=None, ahora=None):
    """Clasifica los findings de AMIs sin uso (in situ). `errores` recibe los fallos de lectura, sin repetir."""
    amis = [f for f in findings if f.get("rule_id") == REGLA and f.get("resource_id")]
    if not amis:
        return findings
    ahora = ahora or datetime.now(timezone.utc)
    por_leer = [f for f in amis if not _nombre(f["detail"]).startswith(PREFIJO_BACKUP)]
    resultados = {}
    if por_leer:
        try:
            lector = lector or lector_aws(cfg, {(f["account_id"], f["region"]) for f in por_leer})
        except (Exception, SystemExit) as e:  # noqa: BLE001 — sin sesión: las AMIs quedan como las dejó aws-doctor
            errores.append({"account_id": None, "region": None, "collector": "amis", "error": _corto(e)})
            lector = None

        def leer(f):
            try:
                return (f["account_id"], f["region"], f["resource_id"]), (True, lector(f["account_id"], f["region"],
                                                                                        f["resource_id"]))
            except Exception as e:  # noqa: BLE001
                return (f["account_id"], f["region"], f["resource_id"]), (False, _corto(e))
        if lector:
            with ThreadPoolExecutor(max_workers=min(8, cfg.get("max_workers", 4))) as pool:
                resultados = dict(pool.map(leer, por_leer))
    vistos = set()
    for f in amis:
        llave = (f["account_id"], f["region"], f["resource_id"])
        leido, valor = resultados.get(llave, (False, None))
        if not leido and valor and valor not in vistos:
            vistos.add(valor)
            errores.append({"account_id": f["account_id"], "region": f["region"], "collector": "amis",
                            "error": f"LastLaunchedTime: {valor}"})
        ultimo = valor if leido else None
        clase, dias = clasificar(_nombre(f["detail"]), ultimo, leido, ahora)
        _aplicar(f, clase, ultimo, dias)
    return findings


def _corto(e):
    from common import error_corto
    return error_corto(e)


# ---------- snapshots gestionados por AWS Backup ----------

REGLAS_SNAPSHOT = {"aws-doctor.orphaned_snapshots", "aws-doctor.stale_snapshots", "regla.snapshots_viejos"}
REGLAS_SNAPSHOT_RDS = {"aws-doctor.old_rds_snapshots"}
_DESC_BACKUP = "this snapshot is created by the aws backup service"


def _es_de_backup(recurso):
    tags = recurso.get("tags") or {}
    desc = ((recurso.get("attrs") or {}).get("description") or "").lower()
    return desc.startswith(_DESC_BACKUP) or any(str(k).lower().startswith("aws:backup") for k in tags)


def marcar_aws_backup(findings, inventory):
    """Un snapshot que creó AWS Backup (descripción o tags del inventario; en RDS, el id `awsbackup:…`) es un
    punto de recuperación: lo retira la retención del vault, no un `delete-snapshot`. Se muestra, pero no suma
    como ahorro. No consulta AWS: el inventario ya trae la descripción y las tags."""
    de_backup = {(r["account_id"], r["region"], r["id"]) for r in inventory.get("resources", [])
                 if r.get("type") == "snapshot" and _es_de_backup(r)}
    for f in findings:
        rule, rid = f.get("rule_id"), f.get("resource_id") or ""
        ebs = rule in REGLAS_SNAPSHOT and (f["account_id"], f["region"], rid) in de_backup
        rds = rule in REGLAS_SNAPSHOT_RDS and rid.startswith("awsbackup:")
        if ebs or rds:
            f["title"] = "Snapshot de AWS Backup (lo retira la retención del vault)"
            f["detail"] = (f.get("detail") or "") + ", gestionado=aws_backup"
            f["is_saving"] = False
    return findings
