"""Findings de discovery["finops"] (hallazgos_finops.py): Cost Optimization Hub, compromisos y anomalías.
Sin llamadas a AWS.

Uso: .venv/bin/python -m pytest 3-validar/tests -q
"""

import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

import validar  # noqa: E402  (agrega RAIZ al path)
import hallazgos_finops as hf  # noqa: E402
import reglas  # noqa: E402
from hallazgos import completar_v2, hallazgo  # noqa: E402

MGMT, CUENTA, REGION = "900000000000", "111111111111", "eu-west-1"
SP_HOLGADO = {"activos": [{"id": "sp-1", "tipo": "Compute", "compromiso_usd_hora": 3.19, "fin": "2027-01-09"}],
              "en_cola": [],
              "utilizacion": {"porcentaje": 94.35, "no_usado_usd": 197.87, "compromiso_usd": 3502.27},
              "cobertura": {"porcentaje": 100.0, "on_demand_usd": 0.0, "cubierto_usd": 4120.94}}


def rec(accion, tipo, rid, ahorro, cuenta=CUENTA, region=REGION, **extra):
    return {"cuenta": cuenta, "region": region, "resource_id": rid, "tipo": tipo, "accion": accion,
            "ahorro_usd": ahorro, "actual": "a", "recomendado": "b", "esfuerzo": "Low", "reinicio": False,
            "reversible": True, "dias_analizados": 14, "origen": "ComputeOptimizer", **extra}


def disc(recs=(), sp=None, anomalias=None, coh_estado="Active", modo="AfterDiscounts", reservas=None):
    return {"run": {"management_account_id": MGMT}, "generated_at": "2026-10-18T00:00:00Z", "finops": {
        "savings_plans": sp, "reservas": reservas or {"utilizacion": None},
        "anomalias": anomalias if anomalias is not None else {"monitores": [{"tipo": "DIMENSIONAL"}], "suscripciones": 1},
        "hub_optimizacion": {"estado": coh_estado, "modo_estimacion": modo, "recomendaciones": list(recs)}}}


def inv(*volumenes):
    return {"resources": [{"account_id": CUENTA, "region": REGION, "service": "ebs", "type": "volume", "id": v,
                           "state": s, "attrs": {"size_gb": 128, "volume_type": "gp3"}, "relations": {}, "tags": {}}
                          for v, s in volumenes]}


def por_rule(findings):
    return {f["rule_id"]: f for f in (completar_v2(x) for x in findings)}


# ---------- Cost Optimization Hub ----------

def test_ebs_delete_es_ebs_unattached_solo_si_el_volumen_esta_available():
    d = disc([rec("Delete", "EbsVolume", "vol-suelto", 11.264), rec("Delete", "EbsVolume", "vol-adjunto", 3.0)])
    f = {x["resource_id"]: completar_v2(x) for x in hf.hallazgos(d, inv(("vol-suelto", "available"),
                                                                        ("vol-adjunto", "in-use")))}
    assert f["vol-suelto"]["issue_id"] == "ebs.unattached" and f["vol-suelto"]["category"] == "desperdicio"
    assert f["vol-adjunto"]["issue_id"] == "cost-optimization-hub.delete.ebsvolume"  # adjunto sin I/O: otro problema
    assert f["vol-suelto"]["price_source"] == "cost_optimization_hub" and f["vol-suelto"]["confidence"] == "alta"


def test_coh_gana_el_dedup_y_la_regla_queda_sin_sumar():
    d = disc([rec("Delete", "EbsVolume", "vol-1", 7.0)])
    inventario = inv(("vol-1", "available"))
    inventario["resources"][0]["attrs"]["volume_type"] = "gp2"
    findings = hf.hallazgos(d, inventario) + reglas.aplicar(inventario)
    salida = validar.deduplicar(findings)
    unattached = [f for f in salida if f["issue_id"] == "ebs.unattached"]
    assert len(unattached) == 1 and unattached[0]["source"] == "cost-optimization-hub"
    assert unattached[0]["monthly_usd"] == 7.0 and unattached[0]["is_saving"]
    gp2 = [f for f in salida if f["rule_id"] == "regla.gp2"]
    assert gp2 and gp2[0]["is_saving"] is False  # mismo recurso: el ahorro cuenta una vez
    assert sum(f["monthly_usd"] or 0 for f in salida if f["is_saving"]) == 7.0


def test_sin_ahorro_hoy_agrupado_con_vencimiento_del_sp():
    recs = [rec("MigrateToGraviton", "Ec2Instance", f"i-{n}", 0.0) for n in range(7)]
    f = por_rule(hf.hallazgos(disc(recs, sp=SP_HOLGADO), {"resources": []}))
    [g] = [x for k, x in f.items() if k.startswith("cost-optimization-hub.")]
    assert g["title"] == "Migrar a Graviton (Ec2Instance): 7 sin ahorro hoy" and g["monthly_usd"] is None
    assert "2027-01-09" in g["detail"] and "(+2 más)" in g["detail"]


def test_compras_agregadas_por_tipo_con_o_sin_resource_id_y_region_none_no_rompe():
    recs = [rec("PurchaseReservedInstances", "ElastiCacheReservedInstances", None, 81.587),
            rec("PurchaseReservedInstances", "ElastiCacheReservedInstances", "ri-x", 10.0),   # resource_id no estable
            rec("PurchaseSavingsPlans", "ComputeSavingsPlans", None, 50.0, region=None)]       # sin región
    f = por_rule(hf.hallazgos(disc(recs), {"resources": []}))
    ri = f["cost-optimization-hub.purchasereservedinstances.elasticachereservedinstances"]
    assert ri["monthly_usd"] == 91.59 and ri["resource_id"] is None
    assert f["cost-optimization-hub.purchasesavingsplans.computesavingsplans"]["region"] is None


def test_sin_ahorro_con_region_none_tampoco_rompe():
    recs = [rec("Rightsize", "Ec2Instance", "i-1", 0.0, region=None), rec("Rightsize", "Ec2Instance", "i-2", 0.0)]
    assert len(hf.hallazgos(disc(recs), {"resources": []})) == 2


def test_antes_de_descuentos_con_sp_holgado_baja_la_confianza_del_computo():
    d = disc([rec("Rightsize", "Ec2Instance", "i-1", 40.0), rec("Delete", "EbsVolume", "vol-1", 5.0)],
             sp=SP_HOLGADO, modo="BeforeDiscounts")
    f = {x["resource_id"]: completar_v2(x) for x in hf.hallazgos(d, {"resources": []})}
    assert f["i-1"]["confidence"] == "baja" and "2027-01-09" in f["i-1"]["detail"]
    assert f["i-1"]["is_saving"] is False and f["vol-1"]["is_saving"] is True  # cómputo absorbido: no suma
    assert f["vol-1"]["confidence"] == "alta"  # EBS no lo cubre un Savings Plan


# ---------- compromisos y guardarraíles ----------

def test_sp_sin_usar_es_gasto_y_vencimiento_con_y_sin_renovacion():
    sp = {**SP_HOLGADO, "activos": [{"id": "sp-1", "tipo": "Compute", "compromiso_usd_hora": 3.19, "fin": "2026-11-30"}]}
    f = por_rule(hf.hallazgos(disc(sp=sp), {"resources": []}))
    usar = f["finops.savings_plans_sin_usar"]
    assert usar["monthly_usd"] == 197.87 and usar["is_saving"] is False and usar["price_source"] == "ce_actual"
    assert f["finops.savings_plan_vence"]["title"] == "Savings Plan vence en 43 días"
    renovado = {**sp, "en_cola": [{"tipo": "Compute", "inicio": "2026-11-30"}]}
    assert "finops.savings_plan_vence" not in por_rule(hf.hallazgos(disc(sp=renovado), {"resources": []}))


def test_cobertura_baja_y_reservas_sin_usar():
    sp = {**SP_HOLGADO, "utilizacion": {"porcentaje": 100.0, "no_usado_usd": 0},
          "cobertura": {"porcentaje": 40.0, "on_demand_usd": 900.0}}
    ri = {"utilizacion": {"porcentaje": 80.0, "horas_sin_usar": 100, "costo_horas_sin_usar_usd": 12.5}}
    f = por_rule(hf.hallazgos(disc(sp=sp, reservas=ri), {"resources": []}))
    assert "finops.savings_plans_sin_usar" not in f and f["finops.cobertura_baja"]["monthly_usd"] is None
    assert f["finops.reservas_sin_usar"]["monthly_usd"] == 12.5 and f["finops.reservas_sin_usar"]["is_saving"] is False


def test_anomalias_y_coh_inactivo():
    f = por_rule(hf.hallazgos(disc(anomalias={"monitores": [], "suscripciones": 0}, coh_estado="Inactive"),
                              {"resources": []}))
    assert f["finops.sin_anomalias"]["category"] == "riesgo" and "finops.coh_inactivo" in f
    f = por_rule(hf.hallazgos(disc(anomalias={"monitores": [{"tipo": "DIMENSIONAL"}], "suscripciones": 0}),
                              {"resources": []}))
    assert "finops.anomalias_sin_suscripcion" in f and "finops.coh_inactivo" not in f
    # Sección no leída (None): no se afirma nada.
    d = disc()
    d["finops"]["anomalias"] = d["finops"]["hub_optimizacion"] = None
    assert hf.hallazgos(d, {"resources": []}) == []


def test_sin_finops_no_hay_findings():
    assert hf.hallazgos({"run": {}}, {"resources": []}) == []


def test_idle_ec2_de_aws_doctor_no_suma_con_sp_holgado():
    doc = hallazgo("desperdicio", "media", CUENTA, REGION, "i-1", "ociosa", "d", 70.0, "aws-doctor",
                   rule_id="aws-doctor.idle_ec2_instances")
    [f] = hf.ajustar_por_compromisos([dict(doc)], disc(sp=SP_HOLGADO))
    f = completar_v2(f)
    assert f["is_saving"] is False and f["confidence"] == "baja" and "2027-01-09" in f["detail"]
    # Sin dato de cobertura no se afirma que el compromiso absorba el ahorro.
    sin_cob = {**SP_HOLGADO, "cobertura": None}
    [g] = hf.ajustar_por_compromisos([dict(doc)], disc(sp=sin_cob))
    assert completar_v2(g)["is_saving"] is True
    # Con el SP casi lleno (≥ 99.5%) tampoco.
    lleno = {**SP_HOLGADO, "utilizacion": {"porcentaje": 99.8, "no_usado_usd": 1.0}}
    [h] = hf.ajustar_por_compromisos([dict(doc)], disc(sp=lleno))
    assert completar_v2(h)["is_saving"] is True


def test_sin_ahorro_no_computo_no_habla_de_savings_plans_y_sin_vence_no_imprime_none():
    recs = [rec("Delete", "EbsVolume", "vol-1", 0.0)]
    [g] = [x for x in hf.hallazgos(disc(recs, sp=SP_HOLGADO), {"resources": []})
           if x["rule_id"].startswith("cost-optimization-hub.")]
    assert "Savings Plans" not in g["detail"]
    sin_fin = {**SP_HOLGADO, "activos": [{"id": "sp-1", "tipo": "Compute"}]}
    doc = hallazgo("desperdicio", "media", CUENTA, REGION, "i-1", "ociosa", "d", 70.0, "aws-doctor",
                   rule_id="aws-doctor.idle_ec2_instances")
    [f] = hf.ajustar_por_compromisos([dict(doc)], disc(sp=sin_fin))
    assert "None" not in f["detail"]


def test_renovacion_en_cola_solo_si_empalma_y_misma_region():
    sp = {**SP_HOLGADO, "activos": [{"id": "sp-1", "tipo": "EC2Instance", "region": "eu-west-1", "fin": "2026-11-30"}]}
    vence = lambda en_cola: "finops.savings_plan_vence" in por_rule(  # noqa: E731
        hf.hallazgos(disc(sp={**sp, "en_cola": en_cola}), {"resources": []}))
    assert not vence([{"tipo": "EC2Instance", "region": "eu-west-1", "inicio": "2026-11-30"}])
    assert vence([{"tipo": "EC2Instance", "region": "us-east-1", "inicio": "2026-11-30"}])  # otra región
    assert vence([{"tipo": "EC2Instance", "region": "eu-west-1", "inicio": "2027-03-01"}])  # no empalma


def test_accion_sin_resource_id_no_es_compra():
    f = hf.hallazgos(disc([rec("Delete", "EbsVolume", None, 5.0)]), {"resources": []})
    assert not [x for x in f if x["rule_id"].startswith("cost-optimization-hub.")]
