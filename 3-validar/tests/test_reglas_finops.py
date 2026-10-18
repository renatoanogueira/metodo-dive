"""Reglas FinOps (bloque B): qué disparan y qué no, y que los costos informativos no sumen ahorro.
Sin llamadas a AWS.

Uso: .venv/bin/python -m pytest 3-validar/tests -q
"""

import json
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

import validar  # noqa: E402  (agrega RAIZ al path)
import doctor  # noqa: E402
import reglas  # noqa: E402
from common.precios import CONSULTAS, elegir  # noqa: E402
from hallazgos import completar_v2  # noqa: E402

RESPUESTAS = json.loads((AQUI / "fixtures" / "pricing" / "get_products.json").read_text())
CUENTA, REGION = "111111111111", "eu-west-1"


def r(service, tipo, rid, state=None, relations=None, **attrs):
    return {"account_id": CUENTA, "region": REGION, "service": service, "type": tipo, "id": rid, "name": rid,
            "state": state, "attrs": attrs, "relations": relations or {}, "tags": {}}


def precios(*claves):
    out = {}
    for c in claves:
        _, _, sufijo, unidad = CONSULTAS[c]
        out[c] = elegir(RESPUESTAS[REGION][c], sufijo, unidad)
    return {REGION: out}


def correr(regla, recursos, con_precios=None):
    ctx = {"ahora": None, "precios": con_precios or {}}
    return [completar_v2({**f, "rule_id": "regla." + regla.__name__.removeprefix("regla_")})
            for f in regla(recursos, ctx)]


# ---------- load balancer sin targets ----------

def _lb(arn, tipo="application", acciones=("forward",)):
    return r("elb", "load_balancer", arn, "active", lb_type=tipo,
             listeners=[{"port": 443, "default_actions": list(acciones)}])


def _tg(arn, lbs, targets):
    return r("elb", "target_group", arn, relations={"attached_to": lbs},
             targets=[{"id": t, "health": "unhealthy"} for t in targets])


def test_lb_sin_targets_con_precio_de_la_region():
    recursos = [_lb("lb-vacio"), _tg("tg-vacio", ["lb-vacio"], []),
                _lb("lb-ok"), _tg("tg-ok", ["lb-ok"], ["i-1"]),  # targets no sanos: no es "sin targets"
                _lb("lb-sin-tg", "network"),
                _lb("lb-redirect", acciones=("redirect",)),
                _lb("lb-fijo", acciones=("fixed-response",)),
                {**_lb("lb-creando"), "state": "provisioning"},
                _lb("lb-gw", "gateway"),
                r("elb", "classic_load_balancer", "clb-vacio", relations={"target_ids": []}, lb_type="classic") | {
                    "arn": "arn:aws:elasticloadbalancing:eu-west-1:111111111111:loadbalancer/clb-vacio"},
                r("elb", "classic_load_balancer", "clb-ok", relations={"target_ids": ["i-1"]}, lb_type="classic")]
    f = {x["resource_id"]: x for x in correr(reglas.regla_lb_sin_targets, recursos, precios("alb_hora", "nlb_hora"))}
    assert set(f) == {"lb-vacio", "lb-sin-tg", "lb-gw",
                      "arn:aws:elasticloadbalancing:eu-west-1:111111111111:loadbalancer/clb-vacio"}
    assert f["lb-vacio"]["monthly_usd"] == 18.4  # 0.0252 × 730 = 18.396, igual que aws-doctor
    assert f["lb-sin-tg"]["monthly_usd"] == 18.4 and f["lb-vacio"]["is_saving"]
    assert f["lb-gw"]["monthly_usd"] is None  # gateway LB: otro precio, sin estimado
    assert f["lb-vacio"]["issue_id"] == "aws-doctor.unused_load_balancers"


def test_lb_sin_targets_mismo_fingerprint_que_aws_doctor():
    items = [{"clave": "unused_load_balancers", "id": "arn:lb", "usd": 18.396, "info": ""}]
    [doc] = [completar_v2(f) for f in doctor.items_a_findings({(CUENTA, REGION): items})]
    [regla] = correr(reglas.regla_lb_sin_targets, [_lb("arn:lb")])
    assert doc["fingerprint"] == regla["fingerprint"]


def test_aws_doctor_usa_el_arn_del_lb():
    datos = {"idle_load_balancers": [{"name": "web", "arn": "arn:lb/web", "estimated_monthly_cost": 18.4}]}
    items = doctor.items_desde_json(datos)
    assert [i["id"] for i in items] == ["arn:lb/web"]


# ---------- logs sin retención ----------

def test_logs_sin_retencion_agregado_e_informativo():
    recursos = [r("logs", "log_group", "/a", retention_days=None, stored_gb=100.0),
                r("logs", "log_group", "/b", retention_days=None, stored_gb=10.0),
                r("logs", "log_group", "/c", retention_days=30, stored_gb=500.0)]
    [f] = correr(reglas.regla_logs_sin_retencion, recursos, precios("logs_gb_mes"))
    assert f["resource_id"] is None and f["title"] == "Log groups sin retención (2)"
    assert round(f["monthly_usd"], 2) == 3.3 and f["is_saving"] is False  # 110 GB × 0.03, gasto
    assert f["detail"].index("/a") < f["detail"].index("/b")  # los más grandes primero
    assert f["issue_id"] == "aws-doctor.cloudwatch_log_groups_without_retention_policy"


def test_logs_sin_retencion_mismo_fingerprint_y_no_ahorro_en_aws_doctor():
    items = [{"clave": "cloudwatch_log_groups_without_retention_policy", "id": "/a", "usd": 3.0, "info": ""}]
    [doc] = [completar_v2(f) for f in doctor.items_a_findings({(CUENTA, REGION): items})]
    [regla] = correr(reglas.regla_logs_sin_retencion, [r("logs", "log_group", "/a", retention_days=None, stored_gb=1)])
    assert doc["fingerprint"] == regla["fingerprint"]
    assert doc["is_saving"] is False  # el mismo problema no puede contar como ahorro solo si falta la API


# ---------- NAT sin endpoint ----------

def _nat(rid, vpc):
    return r("vpc", "nat_gateway", rid, "available", relations={"vpc_id": vpc}, public_ips=["203.0.113.9"])


def _endpoint(vpc, servicio, tipo="Gateway"):
    return r("vpc", "vpc_endpoint", f"vpce-{vpc}-{servicio}", relations={"vpc_id": vpc},
             endpoint_type=tipo, service_name=f"com.amazonaws.{REGION}.{servicio}")


def test_nat_privado_y_endpoint_borrado():
    privado = _nat("nat-p", "vpc-p")
    privado["attrs"]["connectivity"] = "private"
    borrado = {**_endpoint("vpc-b", "s3"), "state": "deleted"}
    f = correr(reglas.regla_nat_sin_endpoint, [privado, _nat("nat-b", "vpc-b"), borrado])
    assert [x["resource_id"] for x in f] == ["nat-b"]


def test_nat_sin_endpoint():
    recursos = [_nat("nat-a", "vpc-a"), _endpoint("vpc-a", "s3"),
                _nat("nat-b", "vpc-b"), _endpoint("vpc-b", "s3", "Interface"),  # interface no es gratis
                _nat("nat-c", "vpc-c"), _endpoint("vpc-c", "s3")]
    assert {f["resource_id"] for f in correr(reglas.regla_nat_sin_endpoint, recursos)} == {"nat-b"}
    # Con tablas DynamoDB en la región, también falta el de DynamoDB.
    recursos.append(r("dynamodb", "table", "t1"))
    f = {x["resource_id"]: x for x in correr(reglas.regla_nat_sin_endpoint, recursos)}
    assert set(f) == {"nat-a", "nat-b", "nat-c"} and "DynamoDB" in f["nat-a"]["title"]
    assert all(x["monthly_usd"] is None for x in f.values())


# ---------- IPv4 pública ----------

def test_ipv4_publica_cuenta_ips_distintas_y_no_suma_ahorro():
    recursos = [r("ec2", "instance", "i-1", "running", public_ip="198.51.100.1"),
                r("ec2", "instance", "i-2", "running", public_ip="198.51.100.2"),
                r("ec2", "instance", "i-3", "stopped", public_ip="198.51.100.3"),
                r("eip", "elastic_ip", "eip-1", associated=True, public_ip="198.51.100.1"),  # la misma de i-1
                r("eip", "elastic_ip", "eip-2", associated=False, public_ip="198.51.100.4"),  # ociosa: otra regla
                _nat("nat-a", "vpc-a")]
    [f] = correr(reglas.regla_ipv4_publica, recursos, precios("ipv4_hora"))
    assert f["title"] == "IPv4 públicas en uso (3)" and round(f["monthly_usd"], 2) == 10.95
    assert f["is_saving"] is False and f["price_source"] == "pricing_api"
    assert reglas.precios_necesarios({"resources": recursos})[REGION] >= {"ipv4_hora", "ipv4_ociosa_hora"}


# ---------- RDS gp2 ----------

def test_rds_gp2_sin_diferencia_de_precio_no_inventa_ahorro():
    recursos = [r("rds", "instance", "db-1", "available", storage_type="gp2", allocated_storage_gb=100, multi_az=True),
                r("rds", "instance", "db-2", "available", storage_type="gp3", allocated_storage_gb=100)]
    [f] = correr(reglas.regla_rds_gp2, recursos, precios("rds_gb_mes:gp2", "rds_gb_mes:gp3"))  # 0.127 = 0.127
    assert f["resource_id"] == "db-1" and f["monthly_usd"] is None
    [f] = correr(reglas.regla_rds_gp2, recursos)  # sin precios: tampoco
    assert f["monthly_usd"] is None and "sin precio de la región" in f["detail"]


def test_rds_gp2_con_diferencia_multi_az():
    p = precios("rds_gb_mes:gp2", "rds_gb_mes:gp3")
    p[REGION]["rds_gb_mes:gp2"] = {**p[REGION]["rds_gb_mes:gp2"], "usd": 0.137}
    [f] = correr(reglas.regla_rds_gp2, [r("rds", "instance", "db-1", storage_type="gp2", allocated_storage_gb=100,
                                          multi_az=True)], p)
    assert round(f["monthly_usd"], 2) == 2.0 and f["is_saving"]  # 100 GB × 2 AZ × 0.01


# ---------- Lambda, EC2, S3 ----------

def test_lambda_x86_agregado_sin_monto():
    recursos = [r("lambda", "function", "f1", architectures=["x86_64"]),
                r("lambda", "function", "f2", architectures=["x86_64"], package_type="Image"),
                r("lambda", "function", "f3", architectures=["arm64"])]
    [f] = correr(reglas.regla_lambda_x86, recursos)
    assert f["title"].endswith("(2)") and f["monthly_usd"] is None and "1 usan imagen" in f["detail"]


def test_ec2_generacion_antigua():
    recursos = [r("ec2", "instance", "i-1", "running", instance_type="m4.large"),
                r("ec2", "instance", "i-2", "running", instance_type="m7g.large"),
                r("ec2", "instance", "i-3", "stopped", instance_type="t2.micro")]
    assert [f["resource_id"] for f in correr(reglas.regla_ec2_generacion_antigua, recursos)] == ["i-1"]


def test_s3_kms_sin_bucket_key():
    recursos = [r("s3", "bucket", "b1", encryption="aws:kms", bucket_key_enabled=False),
                r("s3", "bucket", "b2", encryption="aws:kms", bucket_key_enabled=True),
                r("s3", "bucket", "b3", encryption="AES256", bucket_key_enabled=False),
                r("s3", "bucket", "b4", encryption="aws:kms:dsse", bucket_key_enabled=False)]  # DSSE: sin Bucket Key
    assert {f["resource_id"] for f in correr(reglas.regla_s3_kms_sin_bucket_key, recursos)} == {"b1"}


# ---------- totales ----------

def test_informativos_no_suman_en_findings_md():
    recursos = [r("ec2", "instance", "i-1", "running", public_ip="198.51.100.1"),
                r("ebs", "volume", "vol-1", "available", size_gb=100, volume_type="gp3")]
    findings = validar.ordenar_y_numerar(validar.deduplicar(reglas.aplicar({"resources": recursos})))
    md = validar.generar_md(findings, {"accounts": {CUENTA: {"name": "test"}}}, [])
    assert "**$8.00/mes**" in md  # solo el volumen (100 GB × 0.08); la IPv4 (3.65) es gasto
    assert "$3.65 (no suma)" in md
    assert "| baja | 1 | $0.00 |" in md


def test_rds_gp2_grande_avisa_iops():
    [f] = correr(reglas.regla_rds_gp2, [r("rds", "instance", "db-1", storage_type="gp2", allocated_storage_gb=5000)])
    assert "15000 IOPS base" in f["detail"]


def test_precios_necesarios_de_las_reglas_nuevas():
    recursos = [_lb("lb-a"), _lb("lb-n", "network"), _lb("lb-g", "gateway"),
                r("logs", "log_group", "/a", retention_days=None, stored_gb=1),
                r("logs", "log_group", "/b", retention_days=7, stored_gb=1),
                r("rds", "instance", "db-1", storage_type="gp2", allocated_storage_gb=10),
                r("rds", "instance", "db-2", storage_type="gp3", allocated_storage_gb=10)]
    assert reglas.precios_necesarios({"resources": recursos}) == {REGION: {
        "alb_hora", "nlb_hora", "logs_gb_mes", "rds_gb_mes:gp2", "rds_gb_mes:gp3"}}


def _todo():
    """Un recurso de cada caso de las reglas nuevas."""
    return [_lb("lb-vacio"), r("logs", "log_group", "/a", retention_days=None, stored_gb=100.0),
            _nat("nat-b", "vpc-b"), r("ec2", "instance", "i-1", "running", public_ip="198.51.100.1",
                                      instance_type="m4.large"),
            r("rds", "instance", "db-1", storage_type="gp2", allocated_storage_gb=100),
            r("lambda", "function", "f1", architectures=["x86_64"]),
            r("s3", "bucket", "b1", encryption="aws:kms", bucket_key_enabled=False),
            r("ebs", "volume", "vol-1", "available", size_gb=100, volume_type="gp2")]


def test_con_y_sin_precios_mismos_fingerprints_e_is_saving():
    inv = {"resources": _todo()}
    p = precios("alb_hora", "logs_gb_mes", "ipv4_hora", "rds_gb_mes:gp2", "rds_gb_mes:gp3", "ebs_gb_mes:gp2",
                "ebs_gb_mes:gp3")
    con = {f["fingerprint"]: f["is_saving"] for f in validar.deduplicar(reglas.aplicar(inv, p))}
    sin = {f["fingerprint"]: f["is_saving"] for f in validar.deduplicar(reglas.aplicar(inv))}
    assert con == sin and len(con) >= 9


def test_aws_doctor_no_borra_hallazgos_de_las_reglas():
    # Un "idle" de aws-doctor sobre el mismo recurso no puede hacer desaparecer el finding de la regla.
    inv = {"resources": _todo()}
    items = {(CUENTA, REGION): [
        {"clave": "idle_ec2_instances", "id": "i-1", "usd": 70.0, "info": ""},
        {"clave": "idle_nat_gateways", "id": "nat-b", "usd": 32.0, "info": ""},
        {"clave": "idle_load_balancers", "id": "lb-vacio", "usd": 18.4, "info": ""},
        {"clave": "unused_ebs_volumes", "id": "vol-1", "usd": 10.0, "info": ""}]}
    sin = {f["fingerprint"] for f in validar.deduplicar(reglas.aplicar(inv))}
    con = validar.deduplicar(doctor.items_a_findings(items) + reglas.aplicar(inv))
    assert sin <= {f["fingerprint"] for f in con}
    # y cada recurso suma ahorro una sola vez
    por_recurso = {}
    for f in con:
        if f["is_saving"] and f["monthly_usd"] is not None and f["resource_id"]:
            por_recurso[f["resource_id"]] = por_recurso.get(f["resource_id"], 0) + 1
    assert all(n == 1 for n in por_recurso.values())


def test_desperdicio_sin_monto_bloquea_el_costo_del_mismo_recurso():
    from hallazgos import hallazgo
    desp = hallazgo("desperdicio", "media", CUENTA, REGION, "i-1", "detenida", "d", None, "regla", rule_id="regla.ec2_detenida")
    costo = hallazgo("costo", "baja", CUENTA, REGION, "i-1", "otro", "d", 5.0, "regla", rule_id="regla.otro")
    salida = validar.deduplicar([costo, desp])
    assert {f["rule_id"]: f["is_saving"] for f in salida} == {"regla.ec2_detenida": True, "regla.otro": False}
