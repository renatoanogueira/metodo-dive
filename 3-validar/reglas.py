"""Fuente c) de la fase 3: reglas sobre inventory.json.

Cada regla es una función pequeña `regla_x(recursos, ctx) -> [findings]` registrada en REGLAS.
Para agregar una regla: escribe la función y súmala a la lista del final.
Los attrs se leen con .get() defensivo: si un colector no trae el dato, la regla no dispara.

Uso: from reglas import aplicar; findings = aplicar(inventory)
"""

from datetime import datetime, timezone

from common.precios import CONSULTAS, precio
from hallazgos import hallazgo, parsear_fecha, severidad_por_costo


def _precio(ctx, region, clave, estatico):
    """(usd por unidad, fuente): el de la API para la región si la corrida lo trae; si no, el de respaldo."""
    return precio(ctx.get("precios"), region, clave, estatico)


def _texto_precio(usd, unidad, fuente):
    return f"precio {usd:g} USD/{unidad} ({'Price List API' if fuente == 'pricing_api' else 'estimado us-east-1'})"


def precios_necesarios(inventory):
    """{región: {claves de common.precios}} que las reglas van a pedir para este inventario."""
    pedidos = {}
    for r in inventory.get("resources", []):
        a, reg = r.get("attrs") or {}, r.get("region")
        if not reg or reg == "global":
            continue
        claves = pedidos.setdefault(reg, set())
        if r.get("type") == "volume" and r.get("service") in ("ebs", "ec2"):
            claves.update({f"ebs_gb_mes:{a.get('volume_type') or 'gp2'}", "ebs_gb_mes:gp2", "ebs_gb_mes:gp3"})
        elif r.get("type") == "snapshot":
            claves.add("snapshot_gb_mes")
        elif r.get("type") == "elastic_ip":
            claves.add("ipv4_hora" if a.get("associated") else "ipv4_ociosa_hora")
        elif r.get("type") == "instance" and r.get("service") == "ec2" and a.get("public_ip"):
            claves.add("ipv4_hora")
        elif r.get("type") == "nat_gateway" and a.get("public_ips"):
            claves.add("ipv4_hora")
        elif r.get("type") == "load_balancer" and a.get("lb_type") in ("application", "network"):
            claves.add("alb_hora" if a["lb_type"] == "application" else "nlb_hora")
        elif r.get("type") == "log_group" and a.get("retention_days") is None:
            claves.add("logs_gb_mes")
        elif r.get("service") == "rds" and r.get("type") == "instance" and a.get("storage_type") == "gp2":
            claves.update({"rds_gb_mes:gp2", "rds_gb_mes:gp3"})
    # (RDS: el almacenamiento cuesta lo mismo en todos los motores, verificado en la API; no se filtra por motor.)
    # Un volume_type que common.precios no sabe consultar (uno nuevo) usa el precio de respaldo de la regla.
    pedidos = {reg: {k for k in c if k in CONSULTAS} for reg, c in pedidos.items()}
    return {reg: c for reg, c in pedidos.items() if c}

FUENTE = "regla"

# Precios de respaldo (us-east-1, USD) para cuando la corrida no trae precios.json: las reglas usan el precio
# de la Price List API de la región del recurso (ctx["precios"], ver common/precios.py) y marcan la fuente.
PRECIO_EBS_GB = {"gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05}
PRECIO_SNAPSHOT_GB = 0.05
PRECIO_IPV4_HORA = 0.005
PRECIO_LB_HORA = {"application": 0.0225, "network": 0.0225}  # gateway y classic cobran distinto: sin estimado
PRECIO_LOGS_GB = 0.03
HORAS_MES = 730
MAX_EJEMPLOS = 5
# Familias EC2 antiguas (algunas AWS aún las lista como current generation): las nuevas rinden más por igual o
# menos dinero.
FAMILIAS_ANTIGUAS = {"t1", "t2", "m1", "m2", "m3", "m4", "c1", "c3", "c4", "r3", "r4", "i2", "d2", "g2", "g3",
                     "p2", "x1", "x1e"}

DIAS_SNAPSHOT_VIEJO = 180
DIAS_EC2_DETENIDA = 30
PUERTOS_WEB = {"80", "443"}
# Puertos que, abiertos al mundo, suben la severidad a alta (administración y bases de datos).
PUERTOS_CRITICOS = {"22", "3389", "3306", "5432", "1433", "1521", "27017", "6379", "9200", "5601", "11211"}
TAGS_OWNER = {"owner", "team", "responsable", "dueño", "dueno", "squad", "contact"}
TAGS_ENV = {"environment", "env", "stage", "ambiente", "entorno"}


def _es(r, servicios, tipo):
    return r.get("service") in servicios and r.get("type") == tipo


def _attrs(r):
    return r.get("attrs") or {}


# ---------- reglas ----------

def regla_ebs_sin_adjuntar(recursos, ctx):
    out = []
    for r in recursos:
        if _es(r, {"ebs", "ec2"}, "volume") and r.get("state") == "available":
            a = _attrs(r)
            gb = a.get("size_gb") or 0
            tipo = a.get("volume_type") or "gp2"
            unit, fuente = _precio(ctx, r["region"], f"ebs_gb_mes:{tipo}", PRECIO_EBS_GB.get(tipo, PRECIO_EBS_GB["gp2"]))
            usd = gb * unit
            iops = " (sin las IOPS provisionadas: el costo real es mayor)" if tipo in ("io1", "io2") else ""
            out.append(hallazgo("desperdicio", severidad_por_costo(usd, "media"), r["account_id"], r["region"], r["id"],
                                "Volumen EBS sin adjuntar",
                                f"{gb} GiB {tipo}, estado available. Snapshot y borrar si no se usa. "
                                f"{_texto_precio(unit, 'GB-mes', fuente)}{iops}.", usd, FUENTE, price_source=fuente))
    return out


def regla_eip_sin_asociar(recursos, ctx):
    out = []
    for r in recursos:
        if not _es(r, {"eip", "ec2"}, "elastic_ip"):
            continue
        a = _attrs(r)
        asociada = a.get("associated")
        if asociada is None:
            asociada = bool(a.get("association_id") or a.get("instance_id") or a.get("network_interface_id"))
        if not asociada and r.get("state") != "associated":
            hora, fuente = _precio(ctx, r["region"], "ipv4_ociosa_hora", PRECIO_IPV4_HORA)
            out.append(hallazgo("desperdicio", "media", r["account_id"], r["region"], r["id"],
                                "Elastic IP sin asociar",
                                f"IP {a.get('public_ip')} reservada sin uso; se cobra igual. "
                                f"{_texto_precio(hora, 'hora', fuente)} × {HORAS_MES} h.",
                                hora * HORAS_MES, FUENTE, price_source=fuente))
    return out


def regla_snapshots_viejos(recursos, ctx):
    # Un snapshot que respalda una AMI registrada no se puede borrar sin deregistrar la AMI: no es "viejo".
    de_amis = {(r["account_id"], r["region"], s) for r in recursos if _es(r, {"ami", "ec2"}, "image")
               for s in _attrs(r).get("snapshot_ids") or []}
    out = []
    for r in recursos:
        if not _es(r, {"ebs", "ec2"}, "snapshot") or (r["account_id"], r["region"], r["id"]) in de_amis:
            continue
        a = _attrs(r)
        fecha = parsear_fecha(a.get("start_time") or a.get("create_time"))
        if not fecha:
            continue
        dias = (ctx["ahora"] - fecha).days
        if dias > DIAS_SNAPSHOT_VIEJO:
            gb = a.get("size_gb") or 0
            unit, fuente = _precio(ctx, r["region"], "snapshot_gb_mes", PRECIO_SNAPSHOT_GB)
            usd = gb * unit  # tope: los snapshots son incrementales (se cobra lo cambiado, no el tamaño del volumen)
            origen = ", ".join(r.get("relations", {}).get("attached_to", [])) or "sin volumen origen"
            out.append(hallazgo("desperdicio", severidad_por_costo(usd, "baja"), r["account_id"], r["region"], r["id"],
                                f"Snapshot EBS de más de {DIAS_SNAPSHOT_VIEJO} días",
                                f"{dias} días, {gb} GiB ({origen}). Costo máximo (los snapshots son incrementales); "
                                f"revisar si sigue siendo necesario. {_texto_precio(unit, 'GB-mes', fuente)}.",
                                usd, FUENTE, price_source=fuente))
    return out


def _puertos_abiertos(a):
    """Lista de 'proto/puertos' abiertos a 0.0.0.0/0 o ::/0."""
    if a.get("open_to_world_ports"):
        return list(a["open_to_world_ports"])
    return sorted({f"{x.get('protocol')}/{x.get('ports')}" for x in a.get("ingress") or []
                   if x.get("source") in ("0.0.0.0/0", "::/0")})


def _es_critico(puerto):
    proto, _, rango = puerto.partition("/")
    if proto in ("icmp", "icmpv6", "1", "58"):
        return False
    if proto == "all" or rango == "all":
        return True
    if "-" in rango:
        desde, _, hasta = rango.partition("-")
        try:
            return any(int(desde) <= int(p) <= int(hasta) for p in PUERTOS_CRITICOS)
        except ValueError:
            return True
    return rango in PUERTOS_CRITICOS


def regla_sg_abierto(recursos, ctx):
    out = []
    for r in recursos:
        if not _es(r, {"vpc", "ec2"}, "security_group") or not _attrs(r).get("open_to_world"):
            continue
        puertos = [p for p in _puertos_abiertos(_attrs(r)) if p.partition("/")[2] not in PUERTOS_WEB]
        if not puertos:
            continue
        sev = "alta" if any(_es_critico(p) for p in puertos) else "media"
        out.append(hallazgo("riesgo", sev, r["account_id"], r["region"], r["id"],
                            "Security group abierto a internet en puertos no web",
                            f"{r.get('name') or r['id']}: 0.0.0.0/0 en {', '.join(puertos)}.", None, FUENTE))
    return out


def regla_rds_publico(recursos, ctx):
    out = []
    for r in recursos:
        if r.get("service") == "rds" and _attrs(r).get("publicly_accessible") is True:
            out.append(hallazgo("riesgo", "alta", r["account_id"], r["region"], r["id"],
                                "Base de datos RDS con acceso público",
                                f"{r.get('type')} {_attrs(r).get('engine')} con PubliclyAccessible=true.", None, FUENTE))
    return out


def regla_rds_sin_cifrar(recursos, ctx):
    out = []
    for r in recursos:
        a = _attrs(r)
        # Las instancias miembro de un cluster heredan el cifrado del cluster: se reporta solo el cluster.
        if r.get("service") == "rds" and a.get("storage_encrypted") is False and not a.get("cluster_id"):
            out.append(hallazgo("riesgo", "media", r["account_id"], r["region"], r["id"],
                                "Base de datos RDS sin cifrado en reposo",
                                f"{r.get('type')} {a.get('engine')} con StorageEncrypted=false.", None, FUENTE))
    return out


def _fecha_detencion(a):
    fecha = parsear_fecha(a.get("stopped_at") or a.get("stop_time"))
    if fecha:
        return fecha
    # StateTransitionReason: "User initiated (2025-01-31 10:00:00 GMT)"
    razon = a.get("state_transition_reason") or ""
    if "(" in razon and "GMT" in razon:
        return parsear_fecha(razon.split("(")[-1].replace("GMT", "").strip(" )"))
    return None


def regla_ec2_detenida(recursos, ctx):
    out = []
    for r in recursos:
        if not (_es(r, {"ec2"}, "instance") and r.get("state") == "stopped"):
            continue
        fecha = _fecha_detencion(_attrs(r))
        if fecha and (ctx["ahora"] - fecha).days > DIAS_EC2_DETENIDA:
            out.append(hallazgo("desperdicio", "media", r["account_id"], r["region"], r["id"],
                                f"Instancia EC2 detenida hace más de {DIAS_EC2_DETENIDA} días",
                                f"{r.get('name') or r['id']} detenida desde {fecha.date()}; sus volúmenes siguen cobrando.",
                                None, FUENTE))
    return out


def regla_gp2(recursos, ctx):
    out = []
    for r in recursos:
        a = _attrs(r)
        if _es(r, {"ebs", "ec2"}, "volume") and a.get("volume_type") == "gp2":
            gb = a.get("size_gb") or 0
            gp2, f2 = _precio(ctx, r["region"], "ebs_gb_mes:gp2", PRECIO_EBS_GB["gp2"])
            gp3, f3 = _precio(ctx, r["region"], "ebs_gb_mes:gp3", PRECIO_EBS_GB["gp3"])
            fuente = "pricing_api" if f2 == f3 == "pricing_api" else "static_estimate"
            if fuente == "static_estimate":  # no mezclar un precio de la región con uno de us-east-1
                gp2, gp3 = PRECIO_EBS_GB["gp2"], PRECIO_EBS_GB["gp3"]
            usd = gb * max(gp2 - gp3, 0)
            out.append(hallazgo("costo", "baja", r["account_id"], r["region"], r["id"], "Volumen gp2 migrable a gp3",
                                f"{gb} GiB gp2 → gp3 sin downtime: {gp2:g} → {gp3:g} USD/GB-mes "
                                f"({'Price List API' if fuente == 'pricing_api' else 'estimado us-east-1'}).",
                                usd, FUENTE, price_source=fuente))
    return out


def regla_opensearch_abierto(recursos, ctx):
    out = []
    for r in recursos:
        a = _attrs(r)
        abierta = a.get("access_policy_principal_star") or a.get("policy_open") or a.get("open_access_policy")
        if r.get("service") == "opensearch" and abierta is True:
            # Fuera de VPC el endpoint es público: el riesgo es mucho mayor.
            sev = "media" if a.get("in_vpc") else "alta"
            out.append(hallazgo("riesgo", sev, r["account_id"], r["region"], r["id"],
                                "OpenSearch con access policy abierta (Principal *)",
                                f"Dominio {'en VPC' if a.get('in_vpc') else 'con endpoint público'}; "
                                f"fine-grained access={a.get('fine_grained_access')}.", None, FUENTE))
    return out


def regla_s3_sin_bloqueo_publico(recursos, ctx):
    out = []
    for r in recursos:
        a = _attrs(r)
        # None = no se pudo leer (ver attrs.errors); solo reportamos el False explícito.
        if _es(r, {"s3"}, "bucket") and a.get("public_access_fully_blocked") is False:
            detalle = "sin public access block" if not a.get("public_access_block") else \
                "public access block parcial: " + ", ".join(k for k, v in a["public_access_block"].items() if not v)
            out.append(hallazgo("riesgo", "media", r["account_id"], r["region"], r["id"],
                                "Bucket S3 sin bloqueo de acceso público completo",
                                f"{detalle}. Verificar si el bloqueo a nivel de cuenta lo cubre.", None, FUENTE))
    return out


def _sin_tag(tags, claves):
    return not any(k.lower() in claves for k in (tags or {}))


def servicios_sin_lectura_de_tags():
    """Colectores con TAGS = False: no leen tags, así que sus recursos siempre parecerían sin tag."""
    try:
        import importlib
        import pkgutil

        import colectores
    except ImportError:  # reglas usado sin 2-inventariar en el path: no se excluye nada
        return set()
    sin = set()
    for info in pkgutil.iter_modules(colectores.__path__):
        try:
            mod = importlib.import_module(f"colectores.{info.name}")
        except Exception:  # noqa: BLE001 — un colector roto no impide validar
            continue
        if not getattr(mod, "TAGS", True):
            sin.add(getattr(mod, "NAME", info.name))
    return sin


def _llave(r):
    return (r["account_id"], r.get("region"), r.get("service"), r.get("type"), r["id"])


def regla_sin_tags(recursos, ctx):
    """Agregada por cuenta: cuántos recursos no tienen tag de owner y/o environment. Solo cuenta
    recursos de colectores que leen tags; los demás se informan aparte en el detalle."""
    excluidos = ctx.get("servicios_sin_tags")
    if excluidos is None:
        excluidos = servicios_sin_lectura_de_tags()
    por_cuenta, sin_lectura = {}, {}
    for r in recursos:
        if r.get("service") in excluidos:
            sin_lectura[r["account_id"]] = sin_lectura.get(r["account_id"], 0) + 1
            continue
        por_cuenta.setdefault(r["account_id"], []).append(r)
    out = []
    for account_id, lista in sorted(por_cuenta.items()):
        sin_owner = [r for r in lista if _sin_tag(r.get("tags"), TAGS_OWNER)]
        sin_env = [r for r in lista if _sin_tag(r.get("tags"), TAGS_ENV)]
        faltan = {_llave(r) for r in sin_owner} | {_llave(r) for r in sin_env}
        if not faltan:
            continue
        por_servicio = {}
        for r in lista:
            if _llave(r) in faltan:
                por_servicio[r["service"]] = por_servicio.get(r["service"], 0) + 1
        resumen = ", ".join(f"{s}={n}" for s, n in sorted(por_servicio.items(), key=lambda x: -x[1]))
        sev = "media" if len(faltan) > len(lista) / 2 else "baja"
        fuera = sin_lectura.get(account_id, 0)
        nota = f" {fuera} recursos de servicios cuyos tags no se leen quedan fuera del conteo." if fuera else ""
        out.append(hallazgo("inventario", sev, account_id, None, None,
                            f"{len(faltan)} de {len(lista)} recursos sin tag de owner/environment",
                            f"Sin owner: {len(sin_owner)} · sin environment: {len(sin_env)}. Por servicio: {resumen}.{nota}",
                            None, FUENTE))
    return out


def regla_root(recursos, ctx):
    """Usuario root (de colectores/iam.py). region=None: es global, y así se deduplica con aws-doctor."""
    out = []
    for r in recursos:
        if not _es(r, {"iam"}, "account_summary"):
            continue
        a = _attrs(r)
        if a.get("access_keys_present"):
            out.append(hallazgo("riesgo", "alta", r["account_id"], None, "root-access-keys",
                                "Usuario root con access keys",
                                "La cuenta tiene access keys del root: credenciales permanentes sin límite de permisos. "
                                "Revisa su último uso y bórralas.", None, FUENTE, rule_id="regla.root_access_keys"))
        if a.get("password_present") and not a.get("mfa_enabled"):
            out.append(hallazgo("riesgo", "alta", r["account_id"], None, "root", "Usuario root sin MFA",
                                "El root tiene password y no tiene MFA.", None, FUENTE, rule_id="regla.root_sin_mfa"))
    return out


def _por_cuenta_region(recursos, condicion):
    grupos = {}
    for r in recursos:
        if condicion(r):
            grupos.setdefault((r["account_id"], r["region"]), []).append(r)
    return grupos


def _ejemplos(nombres):
    extra = f" (+{len(nombres) - MAX_EJEMPLOS} más)" if len(nombres) > MAX_EJEMPLOS else ""
    return ", ".join(nombres[:MAX_EJEMPLOS]) + extra


def regla_lb_sin_targets(recursos, ctx):
    """LB sin ningún target registrado en sus target groups (o sin target groups) y sin listener que responda
    solo (redirect / fixed-response): se cobra la hora del LB sin servir a nadie. Un target group no cuesta:
    el costo es del LB. Targets registrados pero no sanos son otro problema (no se marcan aquí). Un classic
    sin instancias registradas también; gateway y classic cobran distinto, así que van sin monto."""
    con_targets = {lb for r in recursos if _es(r, {"elb"}, "target_group") and _attrs(r).get("targets")
                   for lb in (r.get("relations") or {}).get("attached_to", [])}
    out = []
    for r in recursos:
        if _es(r, {"elb"}, "classic_load_balancer") and not (r.get("relations") or {}).get("target_ids"):
            # El id de inventario de un classic es su nombre; el finding usa el ARN, como aws-doctor y los ALB/NLB.
            out.append(hallazgo("desperdicio", "media", r["account_id"], r["region"], r.get("arn") or r["id"],
                                "Load balancer sin targets", f"{r.get('name') or r['id']} (classic): ninguna instancia registrada. Confirmar que no "
                                "se usa y borrarlo.", None, FUENTE))
            continue
        if (not _es(r, {"elb"}, "load_balancer") or r["id"] in con_targets
                or r.get("state") not in (None, "active", "active_impaired")):
            continue
        a = _attrs(r)
        acciones = {t for li in a.get("listeners") or [] for t in li.get("default_actions") or []}
        if acciones & {"redirect", "fixed-response"}:
            continue
        tipo = a.get("lb_type")
        estatico = PRECIO_LB_HORA.get(tipo)
        usd, fuente, precio_txt = None, None, ""
        if estatico is not None:
            hora, fuente = _precio(ctx, r["region"], "alb_hora" if tipo == "application" else "nlb_hora", estatico)
            usd = hora * HORAS_MES
            precio_txt = f" Mínimo: {_texto_precio(hora, 'hora', fuente)} × {HORAS_MES} h, sin LCU."
        out.append(hallazgo("desperdicio", severidad_por_costo(usd, "media"), r["account_id"], r["region"], r["id"],
                            "Load balancer sin targets",
                            f"{r.get('name') or r['id']} ({tipo}): ningún target registrado y ningún listener con "
                            f"redirect o respuesta fija. Confirmar que no se usa y borrarlo.{precio_txt}",
                            usd, FUENTE, price_source=fuente))
    return out


def regla_logs_sin_retencion(recursos, ctx):
    """Agregado por cuenta × región (la misma granularidad que aws-doctor). El monto es lo que cuesta hoy el
    almacenamiento, no un ahorro: definir retención solo borra lo que sea más viejo que el plazo."""
    out = []
    grupos = _por_cuenta_region(recursos, lambda r: _es(r, {"logs"}, "log_group")
                                and _attrs(r).get("retention_days") is None)
    for (cuenta, region), grupo in sorted(grupos.items()):
        gb = sum(_attrs(r).get("stored_gb") or 0 for r in grupo)
        gb_mes, fuente = _precio(ctx, region, "logs_gb_mes", PRECIO_LOGS_GB)
        usd = gb * gb_mes
        grandes = sorted(grupo, key=lambda r: -(_attrs(r).get("stored_gb") or 0))
        ejemplos = _ejemplos([f"{r.get('name') or r['id']} ({_attrs(r).get('stored_gb') or 0:g} GB)" for r in grandes])
        out.append(hallazgo("costo", "baja", cuenta, region, None, f"Log groups sin retención ({len(grupo)})",
                            f"{len(grupo)} log groups guardan {gb:,.1f} GB para siempre. Almacenamiento actual: "
                            f"{_texto_precio(gb_mes, 'GB-mes', fuente)}; es gasto, no ahorro (la retención solo "
                            f"borra lo viejo). Más grandes: {ejemplos}.", usd, FUENTE, price_source=fuente,
                            ahorro=False))
    return out


def regla_nat_sin_endpoint(recursos, ctx):
    """El tráfico a S3/DynamoDB desde subredes privadas pasa por el NAT y paga procesamiento por GB; un gateway
    endpoint es gratis. Sin $: depende del tráfico, que el inventario no tiene."""
    endpoints = {}
    for r in recursos:
        if (_es(r, {"vpc"}, "vpc_endpoint") and (_attrs(r).get("endpoint_type") or "").lower() == "gateway"
                and r.get("state") in (None, "available")):
            servicio = (_attrs(r).get("service_name") or "").rsplit(".", 1)[-1]
            endpoints.setdefault((r["account_id"], r["region"], (r.get("relations") or {}).get("vpc_id")), set()).add(servicio)
    con_dynamo = {(r["account_id"], r["region"]) for r in recursos if _es(r, {"dynamodb"}, "table")}
    out = []
    for r in recursos:
        # Un NAT privado no sale a los endpoints públicos de S3/DynamoDB: no aplica.
        if (not _es(r, {"vpc"}, "nat_gateway") or r.get("state") not in (None, "available")
                or _attrs(r).get("connectivity") == "private"):
            continue
        vpc = (r.get("relations") or {}).get("vpc_id")
        tiene = endpoints.get((r["account_id"], r["region"], vpc), set())
        faltan = [s for s in ("s3", "dynamodb") if s not in tiene and
                  (s == "s3" or (r["account_id"], r["region"]) in con_dynamo)]
        if faltan:
            out.append(hallazgo("costo", "baja", r["account_id"], r["region"], r["id"],
                                "NAT Gateway sin gateway endpoint de " + " ni ".join(s.upper() if s == "s3" else "DynamoDB"
                                                                                    for s in faltan),
                                f"{'VPC ' + vpc if vpc else 'Su VPC'}: el tráfico a {', '.join(faltan)} pasa por el NAT y paga procesamiento por "
                                "GB. Un gateway endpoint es gratis; el ahorro depende del tráfico (ver NatGateway-Bytes "
                                "en Cost Explorer).", None, FUENTE))
    return out


def regla_ipv4_publica(recursos, ctx):
    """Desde feb/2024 AWS cobra cada IPv4 pública por hora, esté o no asociada. Se cuentan IPs distintas por
    cuenta × región (una EIP asociada es también la public_ip de la instancia o del NAT). Es gasto
    informativo, no un ahorro, y es un mínimo: los load balancers públicos también usan IPv4 y no se cuentan."""
    ips = {}
    for r in recursos:
        a = _attrs(r)
        llave = (r["account_id"], r["region"])
        if _es(r, {"ec2"}, "instance") and r.get("state") == "running" and a.get("public_ip"):
            ips.setdefault(llave, set()).add(a["public_ip"])
        elif _es(r, {"eip", "ec2"}, "elastic_ip") and a.get("associated") and a.get("public_ip"):
            ips.setdefault(llave, set()).add(a["public_ip"])
        elif _es(r, {"vpc"}, "nat_gateway") and r.get("state") in (None, "available"):
            ips.setdefault(llave, set()).update(a.get("public_ips") or [])
    out = []
    for (cuenta, region), conjunto in sorted(ips.items()):
        hora, fuente = _precio(ctx, region, "ipv4_hora", PRECIO_IPV4_HORA)
        out.append(hallazgo("costo", "baja", cuenta, region, None, f"IPv4 públicas en uso ({len(conjunto)})",
                            f"{len(conjunto)} IPv4 públicas (instancias, EIPs asociadas y NAT), "
                            f"{_texto_precio(hora, 'hora', fuente)} × {HORAS_MES} h cada una. Es gasto, no ahorro; "
                            "mínimo (sin las de load balancers públicos). Revisar si cada una necesita IP pública "
                            "(SSM Session Manager, ALB o IPv6 en su lugar).", hora * HORAS_MES * len(conjunto), FUENTE,
                            price_source=fuente, ahorro=False))
    return out


def regla_rds_gp2(recursos, ctx):
    """Solo con precios de la región: en RDS gp2 y gp3 suelen costar lo mismo, así que no se inventa una
    diferencia con precios de EBS. Sin diferencia, queda la mejora de rendimiento base (sin $)."""
    out = []
    for r in recursos:
        a = _attrs(r)
        if not (_es(r, {"rds"}, "instance") and a.get("storage_type") == "gp2"):
            continue
        gb = a.get("allocated_storage_gb") or 0
        gp2, f2 = precio(ctx.get("precios"), r["region"], "rds_gb_mes:gp2")
        gp3, f3 = precio(ctx.get("precios"), r["region"], "rds_gb_mes:gp3")
        az = 2 if a.get("multi_az") else 1
        # gp2 da 3 IOPS por GB: por encima de 12000 IOPS base, gp3 sin IOPS provisionadas rinde menos.
        aviso = (f" Ojo: con {gb} GB gp2 da {gb * 3} IOPS base, más que el base de gp3; provisionar IOPS al "
                 "migrar (y recalcular el costo)." if gb * 3 > 12000 else "")
        usd = gb * az * (gp2 - gp3) if f2 == f3 == "pricing_api" and gp2 > gp3 else None
        texto = (f"{gp2:g} → {gp3:g} USD/GB-mes (Price List API, Single-AZ{' ×2 por Multi-AZ' if az == 2 else ''})"
                 if f2 == f3 == "pricing_api" else "sin precio de la región")
        out.append(hallazgo("costo", "baja", r["account_id"], r["region"], r["id"], "RDS con almacenamiento gp2",
                            f"{gb} GB gp2 → gp3: {texto}. gp3 trae rendimiento base fijo (3000 IOPS y 125 MiB/s; "
                            "12000 y 500 desde 400 GB, salvo SQL Server) en vez de depender del tamaño; se cambia sin "
                            f"downtime (modify-db-instance).{aviso}", usd, FUENTE,
                            price_source="pricing_api" if usd is not None else None))
    return out


def regla_lambda_x86(recursos, ctx):
    """Agregado por cuenta × región. arm64 (Graviton) cobra ~20% menos por GB-s; sin $ porque depende de la
    duración real, y no toda función migra sin cambios (dependencias nativas, imágenes de contenedor)."""
    out = []
    grupos = _por_cuenta_region(recursos, lambda r: _es(r, {"lambda"}, "function")
                                and (_attrs(r).get("architectures") or ["x86_64"]) == ["x86_64"])
    for (cuenta, region), grupo in sorted(grupos.items()):
        imagenes = sum(1 for r in grupo if _attrs(r).get("package_type") == "Image")
        nota = f" {imagenes} usan imagen de contenedor (hay que reconstruirla para arm64)." if imagenes else ""
        out.append(hallazgo("costo", "baja", cuenta, region, None, f"Lambdas x86_64 migrables a arm64 ({len(grupo)})",
                            f"{len(grupo)} funciones en x86_64: {_ejemplos(sorted(r['id'] for r in grupo))}. arm64 "
                            f"cobra ~20% menos por GB-s; probar antes (dependencias nativas).{nota}", None, FUENTE))
    return out


def regla_ec2_generacion_antigua(recursos, ctx):
    out = []
    for r in recursos:
        a = _attrs(r)
        familia = (a.get("instance_type") or "").split(".")[0]
        if _es(r, {"ec2"}, "instance") and r.get("state") == "running" and familia in FAMILIAS_ANTIGUAS:
            out.append(hallazgo("costo", "baja", r["account_id"], r["region"], r["id"],
                                "Instancia EC2 de una familia antigua",
                                f"{r.get('name') or r['id']}: {a.get('instance_type')}. Las familias nuevas (ej. "
                                "t3/t4g, m6i/m7g, c6i/c7g, r6i/r7g) rinden más por un precio igual o menor (comparar "
                                "en la región); cambiar requiere parar la instancia.", None, FUENTE))
    return out


def regla_s3_kms_sin_bucket_key(recursos, ctx):
    """Con SSE-KMS y sin Bucket Key, cada objeto leído o escrito es una llamada a KMS (se cobra por 10.000).
    Bucket Key la reduce hasta ~99%. Sin $: depende del volumen de requests."""
    out = []
    for r in recursos:
        a = _attrs(r)
        # Solo aws:kms: DSSE-KMS (aws:kms:dsse) no admite Bucket Key.
        if _es(r, {"s3"}, "bucket") and a.get("encryption") == "aws:kms" and a.get("bucket_key_enabled") is False:
            out.append(hallazgo("costo", "baja", r["account_id"], r["region"], r["id"], "Bucket S3 con SSE-KMS sin Bucket Key",
                                f"{r.get('name') or r['id']}: cada request paga una llamada a KMS. Activar Bucket Key "
                                "(solo afecta a objetos nuevos o re-copiados).", None, FUENTE))
    return out


def regla_sin_budget(recursos, ctx):
    """Cuenta con costo y sin ningún budget que la vigile: ni uno propio, ni uno de la management filtrado a
    ella, ni uno de la management sin filtro de cuenta (vigila el total de la organización)."""
    mgmt = ctx.get("management")
    # Sin mirar la management no se puede afirmar nada de las miembro: ahí suelen estar los budgets centrales.
    if not mgmt or (mgmt, "budgets") not in ctx.get("escaneado", set()) or (mgmt, "budgets") in ctx.get("fallado", set()):
        return []
    # Solo los de costo: uno de cobertura/uso de Savings Plans o RIs, o de uso, no vigila el gasto.
    budgets = [r for r in recursos if _es(r, {"budgets"}, "budget") and _attrs(r).get("budget_type") == "COST"]
    cubiertas = {r["account_id"] for r in budgets}
    for r in budgets:
        filtradas = _attrs(r).get("linked_accounts") or []
        if r["account_id"] == mgmt and not filtradas and not _attrs(r).get("filtrado"):
            return []  # un budget del total de la organización (sin ningún filtro)
        cubiertas.update(filtradas)
    out = []
    for cuenta in sorted(ctx.get("cuentas") or {}):
        if (cuenta in cubiertas or (cuenta, "budgets") not in ctx.get("escaneado", set())
                or (cuenta, "budgets") in ctx.get("fallado", set())):
            continue
        out.append(hallazgo("riesgo", "baja", cuenta, None, None, "Cuenta sin AWS Budget",
                            "Ningún budget la vigila (ni propio ni uno de la management filtrado a esta cuenta): un "
                            "gasto que se dispara se ve recién en la factura. Un budget mensual con alerta al 80% y "
                            "al 100% del pronóstico es gratis (los dos primeros por cuenta).", None, FUENTE))
    return out


REGLAS = [
    regla_ebs_sin_adjuntar,
    regla_eip_sin_asociar,
    regla_snapshots_viejos,
    regla_sg_abierto,
    regla_rds_publico,
    regla_rds_sin_cifrar,
    regla_ec2_detenida,
    regla_gp2,
    regla_opensearch_abierto,
    regla_s3_sin_bloqueo_publico,
    regla_sin_tags,
    regla_root,
    regla_lb_sin_targets,
    regla_logs_sin_retencion,
    regla_nat_sin_endpoint,
    regla_ipv4_publica,
    regla_rds_gp2,
    regla_lambda_x86,
    regla_ec2_generacion_antigua,
    regla_s3_kms_sin_bucket_key,
    regla_sin_budget,
]


def aplicar(inventory, precios=None):
    """Corre todas las reglas. La fecha de referencia es la del inventario (reproducible). `precios` es
    {región: {clave: precio}} de common.precios (o None: las reglas usan sus estimados de respaldo)."""
    ahora = parsear_fecha(inventory.get("generated_at")) or datetime.now(timezone.utc)
    ctx = {"ahora": ahora, "precios": precios or {},
           # Dónde corrió cada colector sin error: una regla de "falta X" solo afirma donde se pudo mirar.
           "escaneado": {(s.get("account_id"), s.get("collector")) for s in inventory.get("scanned") or []},
           "fallado": {(e.get("account_id"), e.get("collector")) for e in inventory.get("errors") or []},
           "cuentas": inventory.get("accounts") or {},
           "management": (inventory.get("run") or {}).get("management_account_id")}
    recursos = inventory.get("resources", [])
    findings = []
    for regla in REGLAS:
        for f in regla(recursos, ctx):
            f.setdefault("rule_id", "regla." + regla.__name__.removeprefix("regla_"))
            findings.append(f)
    return findings
