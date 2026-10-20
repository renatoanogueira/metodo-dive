"""Runner de lectura para la skill metodo-dive: la única forma en que la skill consulta AWS.

La skill decide cuándo hace falta un dato que DIVE no trae (ej. el CloudTrail de un volumen, el tráfico de un load
balancer en 30 días). Este script ejecuta esa consulta y guarda la salida cruda, con su hash, en
<corrida>/aws/<id>.json: los insights la referencian (`salida_ref`) y el validador comprueba que los valores
citados aparecen en ella. Así la skill no transcribe resultados ni escribe comandos de shell.

Garantías en código (no en el prompt):
- solo operaciones de lectura de metadatos (describe_*, list_*, lookup_events, get_metric_*, algunos get_*);
- siempre con el perfil de solo lectura de DIVE de esa cuenta (sesion_cuenta), nunca otro;
- parámetros como JSON validado (claves y valores acotados), sin shell;
- cuenta y región de la corrida; la salida se acota a 200 KB.

Uso: .venv/bin/python -I skill/scripts/aws_leer.py --config <config> <carpeta de la corrida> <cuenta> <región> \\
         <servicio> <operación> ['{"Parametro": "valor"}']
Ej.: ... cloudtrail lookup_events '{"LookupAttributes": [{"AttributeKey": "ResourceName", "AttributeValue": "vol-0abc12345"}]}'
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from resumen import RAIZ, _leer  # noqa: E402,F401  (resumen agrega RAIZ al path)

# Lista blanca de pares (servicio, operación): solo lecturas de metadatos. Lo que puede devolver contenido o secretos
# (variables de entorno de Lambda/ECS, user-data, parámetros de EventBridge, suscripciones de SNS…) no está, aunque
# su nombre empiece por describe_ o list_.
PERMITIDAS = {
    "ec2": {"describe_volumes", "describe_instances", "describe_images", "describe_image_attribute", "describe_snapshots",
            "describe_addresses", "describe_network_interfaces", "describe_nat_gateways", "describe_vpc_endpoints",
            "describe_security_groups", "describe_launch_templates"},
    "elbv2": {"describe_load_balancers", "describe_target_groups", "describe_target_health", "describe_listeners",
              "describe_rules", "describe_tags"},
    "elb": {"describe_load_balancers", "describe_instance_health"},
    "rds": {"describe_db_instances", "describe_db_snapshots", "describe_db_clusters", "describe_db_cluster_snapshots"},
    "cloudwatch": {"get_metric_statistics", "get_metric_data", "list_metrics", "describe_alarms"},
    "cloudtrail": {"lookup_events"},
    "logs": {"describe_log_groups"},
    "backup": {"list_backup_plans", "list_backup_selections", "list_recovery_points_by_resource",
               "list_recovery_points_by_backup_vault", "list_backup_vaults", "describe_backup_vault", "list_backup_jobs"},
    "autoscaling": {"describe_auto_scaling_groups"},
    "elasticache": {"describe_cache_clusters", "describe_replication_groups", "describe_reserved_cache_nodes"},
    "s3": {"get_bucket_encryption", "get_bucket_lifecycle_configuration", "get_bucket_versioning", "get_bucket_tagging",
           "get_bucket_policy_status", "get_public_access_block", "get_bucket_location"},
    "secretsmanager": {"list_secrets"},
    "ecs": {"list_clusters", "describe_clusters", "list_services", "describe_services"},
    "efs": {"describe_file_systems"},
    "ecr": {"describe_repositories"},
    "dynamodb": {"list_tables", "describe_table"},
    "kms": {"describe_key", "list_aliases"},
    "cost-optimization-hub": {"get_recommendation", "list_recommendations"},
    "savingsplans": {"describe_savings_plans"},
    "budgets": {"describe_budgets"},
    "route53": {"list_hosted_zones"},
    "cloudfront": {"list_distributions"},
    "sns": {"list_topics"},
    "sqs": {"list_queues"},
    "events": {"list_rules"},
}
# Defensa en profundidad: aunque una operación permitida devolviera alguno de estos campos, no se guarda ni se imprime.
CAMPOS_SENSIBLES = {"userdata", "environment", "variables", "input", "headerparameters", "querystringparameters",
                    "authparameters", "secretstring", "password", "masteruserpassword"}
RE_OPERACION = re.compile(r"[a-z][a-z0-9_]{2,60}")
RE_CLAVE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,40}")
RE_VALOR = re.compile(r"[A-Za-z0-9._:/@+=#,\- ]{0,512}")
RE_CUENTA = re.compile(r"[0-9]{12}")
RE_ARN_PERSONA = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:user/(?:[\w+=,.@/-]*/)?([\w+=,.@-]+)|assumed-role/[\w+=,.@-]+/([\w+=,.@-]+)")
RE_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]")
MAX_SALIDA = 200_000


def _valor_ok(v, nivel=0):
    if nivel > 4:
        return False
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return math.isfinite(v)
    if isinstance(v, str):
        return bool(RE_VALOR.fullmatch(v))
    if isinstance(v, list):
        return len(v) <= 100 and all(_valor_ok(x, nivel + 1) for x in v)
    if isinstance(v, dict):
        return len(v) <= 20 and all(isinstance(k, str) and RE_CLAVE.fullmatch(k) and _valor_ok(x, nivel + 1)
                                    for k, x in v.items())
    return False


def validar_llamada(cuenta, region, servicio, operacion, params, cuentas_corrida, regiones_corrida=None):
    """→ lista de problemas (vacía = se puede ejecutar)."""
    p = []
    if not RE_CUENTA.fullmatch(str(cuenta)) or cuenta not in cuentas_corrida:
        p.append(f"cuenta {cuenta!r}: no es una cuenta de esta corrida")
    if not RE_REGION.fullmatch(str(region)):
        p.append(f"región {region!r} inválida")
    elif regiones_corrida and region not in regiones_corrida:
        p.append(f"región {region!r}: no es una región de esa cuenta en esta corrida")
    if servicio not in PERMITIDAS:
        p.append(f"servicio {servicio!r} no permitido")
    elif not RE_OPERACION.fullmatch(str(operacion)):
        p.append(f"operación {operacion!r} inválida (snake_case, ej. describe_volumes)")
    elif operacion not in PERMITIDAS[servicio]:
        p.append(f"operación {operacion!r} no permitida en {servicio}: la lista blanca solo trae lecturas de metadatos "
                 "(nada que pueda leer contenido, secretos o credenciales)")
    if not isinstance(params, dict) or not _valor_ok(params):
        p.append("parámetros: debe ser un objeto JSON con claves PascalCase y valores simples acotados")
    return p


def _usuarios(o, vistos=None, nivel=0):
    """Todos los nombres de persona que aparecen en la respuesta (CloudTrail: Username; IAM: UserName), también dentro
    del JSON embebido en strings (CloudTrailEvent) y en los ARN (user/<nombre>, assumed-role/<rol>/<sesión>)."""
    vistos = set() if vistos is None else vistos
    if nivel > 8:
        return vistos
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("Username", "UserName", "userName") and isinstance(v, str) and v:
                vistos.add(v)
            else:
                _usuarios(v, vistos, nivel + 1)
    elif isinstance(o, list):
        for v in o:
            _usuarios(v, vistos, nivel + 1)
    elif isinstance(o, str):
        if o.startswith("{") and len(o) <= 300_000:
            try:
                _usuarios(json.loads(o), vistos, nivel + 1)
            except ValueError:
                pass
        for m in RE_ARN_PERSONA.finditer(o):
            vistos.add(m.group(1) or m.group(2))
    return vistos


def _redactar(o, nivel=0):
    """Copia de la respuesta sin los campos que pueden traer secretos o contenido."""
    if nivel > 30:
        return "[demasiado profundo]"
    if isinstance(o, dict):
        return {k: "[redactado]" if str(k).lower() in CAMPOS_SENSIBLES else _redactar(v, nivel + 1) for k, v in o.items()}
    if isinstance(o, list):
        return [_redactar(v, nivel + 1) for v in o]
    return o


def leer(cfg, carpeta, cuenta, region, servicio, operacion, params, sesion=None):
    """Ejecuta la lectura y guarda <carpeta>/aws/<id>.json. → (ref, doc). Lanza ValueError si no es válida."""
    carpeta = Path(carpeta)
    discovery = _leer(carpeta / "discovery.json")
    cuentas = set(discovery.get("accounts") or {})
    regiones = set(((discovery.get("accounts") or {}).get(cuenta) or {}).get("regions") or {})
    problemas = validar_llamada(cuenta, region, servicio, operacion, params, cuentas, regiones)
    if problemas:
        raise ValueError("; ".join(problemas))
    if sesion is None:
        # La garantía "solo lectura de DIVE" depende del rol de auditoría: con otro rol (assume_role a un rol
        # distinto de DIVE_ReadOnly) el Deny de la política no aplica, y las credenciales no pueden venir del entorno.
        if cfg["access_mode"] != "sso" and cfg.get("scan_role_name") != "DIVE_ReadOnly":
            raise ValueError("el runner solo usa el rol DIVE_ReadOnly (o los perfiles dive-* en modo sso)")
        for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                    "AWS_SHARED_CREDENTIALS_FILE"):
            os.environ.pop(var, None)
        from common.aws import sesion_cuenta, sesion_management
        sesion = sesion_cuenta(cfg, cuenta, sesion_management(cfg) if cfg["access_mode"] != "sso" else None)
    from common.aws import client
    cli = client(sesion, servicio, region)
    metodo = getattr(cli, operacion, None)
    if not callable(metodo) or operacion.startswith("_"):
        raise ValueError(f"{servicio} no tiene la operación {operacion}")
    respuesta = metodo(**params)
    respuesta.pop("ResponseMetadata", None)
    usuarios = sorted(_usuarios(respuesta))
    respuesta = _redactar(respuesta)
    salida = json.dumps(respuesta, indent=1, ensure_ascii=False, default=str, sort_keys=True)
    truncada = len(salida) > MAX_SALIDA
    if truncada:
        salida = salida[:MAX_SALIDA] + "\n… (salida truncada)"
    destino = carpeta / "aws"
    destino.mkdir(exist_ok=True)
    n = 1 + max([int(m.group(1)) for f in destino.glob("A*.json") if (m := re.fullmatch(r"A(\d{3})\.json", f.name))] or [0])
    for _ in range(20):  # dos lecturas a la vez pueden elegir el mismo número: se reintenta con el siguiente
        ref = f"A{n:03d}.json"
        doc = {"id": ref[:-5], "cuenta": cuenta, "region": region, "servicio": servicio, "operacion": operacion,
               "parametros": params, "salida": salida, "truncada": truncada,
               # Personas que aparecen en la salida, bajo una clave que el anonimizador trata como nombres de persona.
               "username": usuarios,
               "sha256": hashlib.sha256(salida.encode()).hexdigest(),
               "ejecutado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        try:
            fd = os.open(destino / ref, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            n += 1
            continue
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=1, ensure_ascii=False)
        break
    else:
        raise ValueError("no se pudo reservar un número de salida (A###)")
    return ref, doc


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="dive.config.yaml (por defecto DIVE_CONFIG)")
    ap.add_argument("carpeta")
    ap.add_argument("cuenta")
    ap.add_argument("region")
    ap.add_argument("servicio")
    ap.add_argument("operacion")
    ap.add_argument("params", nargs="?", default="{}")
    a = ap.parse_args()
    try:
        params = json.loads(a.params)
    except ValueError:
        print("ERROR: los parámetros no son JSON válido", file=sys.stderr)
        return 2
    sys.path.insert(0, str(RAIZ))
    from common import cargar_config, error_corto
    try:
        ref, doc = leer(cargar_config(a.config), a.carpeta, a.cuenta, a.region, a.servicio, a.operacion, params)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — sesión expirada, sin permiso, red: un mensaje claro y no un traceback
        print(f"ERROR: la consulta a AWS falló ({error_corto(e)}). Si la sesión expiró, pide al usuario que haga login "
              "con el comando que imprime dive.sh; no pruebes con otro perfil.", file=sys.stderr)
        return 3
    print(f"guardado {ref} ({len(doc['salida'])} caracteres, sha256 {doc['sha256'][:12]}…): úsalo como salida_ref")
    print(doc["salida"][:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
