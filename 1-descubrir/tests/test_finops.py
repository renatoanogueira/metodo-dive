"""Datos FinOps de la fase 1 (finops.py) con clientes falsos. Sin llamadas a AWS.

Uso: .venv/bin/python -m pytest 1-descubrir/tests -q
"""

import sys
from datetime import date
from pathlib import Path

from botocore.exceptions import ClientError

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

import finops  # noqa: E402

HOY = date(2026, 10, 18)
MGMT, MIEMBRO, EXCLUIDA = "111111111111", "222222222222", "333333333333"


def _error(codigo):
    return ClientError({"Error": {"Code": codigo, "Message": "x"}}, "Op")


class Falso:
    """Cliente con respuestas fijas por operación (un valor, una lista de páginas o una excepción)."""

    def __init__(self, **respuestas):
        self.respuestas = respuestas
        self.llamadas = []

    def __getattr__(self, op):
        def llamar(**kw):
            self.llamadas.append((op, kw))
            r = self.respuestas[op]
            if isinstance(r, Exception):
                raise r
            return r(**kw) if callable(r) else r
        return llamar

    def get_paginator(self, op):
        cliente = self

        class Pag:
            def paginate(self, **kw):
                cliente.llamadas.append((op, kw))
                return cliente.respuestas[op]
        return Pag()


def _sp(**kw):
    if kw["states"] != ["active"]:
        return {"savingsPlans": [{"savingsPlanType": "Compute", "start": "2027-01-09T00:00:00Z"}]}
    if not kw.get("nextToken"):
        return {"savingsPlans": [{"savingsPlanId": "sp-2", "savingsPlanType": "Compute", "commitment": "1.005",
                                  "start": "2026-05-19T00:00:00Z", "end": "2027-05-19T00:00:00Z"}], "nextToken": "t"}
    return {"savingsPlans": [{"savingsPlanId": "sp-1", "savingsPlanType": "Compute", "commitment": "3.19",
                              "start": "2026-01-09T00:00:00Z", "end": "2027-01-09T00:00:00Z"}]}


def _cobertura(**kw):
    assert kw["Granularity"] == "MONTHLY"
    if not kw.get("NextToken"):  # dos meses en la ventana, y una segunda página
        return {"SavingsPlansCoverages": [{"Coverage": {"OnDemandCost": "10", "SpendCoveredBySavingsPlans": "1849.28"}}],
                "NextToken": "p2"}
    return {"SavingsPlansCoverages": [{"Coverage": {"OnDemandCost": "30", "SpendCoveredBySavingsPlans": "2271.67"}}]}


def test_savings_plans_activos_utilizacion_y_cobertura():
    sp = Falso(describe_savings_plans=_sp)
    ce = Falso(get_savings_plans_utilization={"Total": {
        "Utilization": {"TotalCommitment": "3502.272", "UsedCommitment": "3304.4", "UnusedCommitment": "197.87",
                        "UtilizationPercentage": "94.35"}, "Savings": {"NetSavings": "618.67"}}},
        get_savings_plans_coverage=_cobertura)
    s = finops.savings_plans(sp, ce, HOY)
    assert [a["id"] for a in s["activos"]] == ["sp-1", "sp-2"]  # ordenados por vencimiento
    assert s["activos"][0]["compromiso_usd_hora"] == 3.19 and s["activos"][0]["fin"] == "2027-01-09"
    assert s["utilizacion"]["no_usado_usd"] == 197.87 and s["utilizacion"]["porcentaje"] == 94.35
    assert s["utilizacion"]["periodo"] == {"Start": "2026-09-18", "End": "2026-10-18"}
    # Se suman los dos períodos (y páginas) y se recalcula: 4120.95 cubierto de 4160.95.
    assert s["cobertura"]["on_demand_usd"] == 40.0 and s["cobertura"]["porcentaje"] == round(100 * 4120.95 / 4160.95, 2)
    assert s["en_cola"] == [{"tipo": "Compute", "region": None, "inicio": "2027-01-09"}]


def test_sin_savings_plans_no_consulta_cost_explorer():
    ce = Falso()
    s = finops.savings_plans(Falso(describe_savings_plans={"savingsPlans": []}), ce, HOY)
    assert s == {"activos": [], "en_cola": [], "utilizacion": None, "cobertura": None} and ce.llamadas == []


def test_reservas_sin_horas_compradas_o_sin_datos_es_leida():
    assert finops.reservas(Falso(get_reservation_utilization={"Total": {"PurchasedHours": "0"}}), HOY) == \
        {"utilizacion": None}
    # Sin RIs Cost Explorer responde DataUnavailable: la sección se leyó (no "no leída" para el panel).
    assert finops.reservas(Falso(get_reservation_utilization=_error("DataUnavailableException")), HOY) == \
        {"utilizacion": None}


def test_coh_filtra_cuentas_del_config_y_respeta_estado():
    rec = lambda cuenta, ahorro: {"recommendationId": cuenta[:3], "accountId": cuenta, "region": "eu-west-1",  # noqa: E731
                                  "resourceId": "vol-1", "currentResourceType": "EbsVolume", "actionType": "Delete",
                                  "estimatedMonthlySavings": ahorro, "resourceArn": f"arn:aws:ec2::{cuenta}:volume/vol-1"}
    coh = Falso(list_enrollment_statuses={"items": [{"accountId": MGMT, "status": "Active"}]},
                get_preferences={"savingsEstimationMode": "AfterDiscounts"},
                list_recommendations=[{"items": [rec(MIEMBRO, 11.264), rec(EXCLUIDA, 5.0)]}])
    c = finops.cost_optimization_hub(coh, {MGMT, MIEMBRO}, MGMT)
    assert c["estado"] == "Active" and c["modo_estimacion"] == "AfterDiscounts"
    assert [r["cuenta"] for r in c["recomendaciones"]] == [MIEMBRO]
    assert c["recomendaciones"][0]["ahorro_usd"] == 11.264
    assert "resourceArn" not in c["recomendaciones"][0] and "arn" not in str(c["recomendaciones"][0])
    assert "id" not in c["recomendaciones"][0]  # recommendationId: opaco, no hace falta y no se anonimiza
    inactivo = Falso(list_enrollment_statuses={"items": [{"accountId": MGMT, "status": "Inactive"}]})
    assert finops.cost_optimization_hub(inactivo, {MGMT}, MGMT) == \
        {"estado": "Inactive", "modo_estimacion": None, "recomendaciones": []}


def _factura(entidad, usd, moneda="EUR", tasa="0.8779", con_pago=True):
    x = {"Entity": {"InvoicingEntity": entidad},
         "BaseCurrencyAmount": {"CurrencyCode": "USD", "TotalAmountBeforeTax": str(usd), "TotalAmount": str(usd * 1.2)},
         "TaxCurrencyAmount": {"CurrencyCode": moneda}}
    if con_pago:
        x["PaymentCurrencyAmount"] = {"CurrencyCode": moneda, "TotalAmount": "1",
                                      "CurrencyExchangeDetails": {"SourceCurrencyCode": "USD",
                                                                  "TargetCurrencyCode": moneda, "Rate": tasa}}
    return x


def test_moneda_pondera_entidades_e_ignora_facturas_sin_moneda_de_pago():
    def resumenes(Selector, Filter):  # noqa: N803
        assert Selector == {"ResourceType": "ACCOUNT_ID", "Value": MGMT}
        if Filter["BillingPeriod"] == {"Month": 9, "Year": 2026}:
            return {"InvoiceSummaries": [_factura("AWS EMEA SARL", 900.0, tasa="0.87"),
                                         _factura("AWS, Inc.", 100.0, tasa="0.89"),
                                         _factura("AWS, Inc.", 50.0, con_pago=False)]}
        return {"InvoiceSummaries": []}
    m = finops.moneda_de_pago(Falso(list_invoice_summaries=resumenes), MGMT, HOY)
    assert m["codigo"] == "EUR" and m["periodo"] == "2026-09" and m["fuente"] == "invoice"
    assert m["tasa"] == round(0.87 * 0.9 + 0.89 * 0.1, 6)
    assert [(e["entidad"], e["peso"]) for e in m["por_entidad"]] == [("AWS EMEA SARL", 0.9), ("AWS, Inc.", 0.1)]
    assert "900" not in str(m) and "1080" not in str(m)  # sin totales de la factura


def test_moneda_mes_sin_facturas_usa_el_anterior_y_usd():
    def resumenes(Selector, Filter):  # noqa: N803
        if Filter["BillingPeriod"] == {"Month": 8, "Year": 2026}:
            return {"InvoiceSummaries": [_factura("AWS, Inc.", 10.0, moneda="USD", tasa=None)]}
        return {"InvoiceSummaries": []}
    m = finops.moneda_de_pago(Falso(list_invoice_summaries=resumenes), MGMT, HOY)
    assert (m["codigo"], m["tasa"], m["periodo"]) == ("USD", 1.0, "2026-08")


def test_enero_pide_diciembre_del_anio_anterior():
    pedidos = []

    def resumenes(Selector, Filter):  # noqa: N803
        pedidos.append(Filter["BillingPeriod"])
        return {"InvoiceSummaries": []}
    assert finops.moneda_de_pago(Falso(list_invoice_summaries=resumenes), MGMT, date(2027, 1, 5)) is None
    assert pedidos == [{"Month": 12, "Year": 2026}, {"Month": 11, "Year": 2026}, {"Month": 10, "Year": 2026}]


class SesionFalsa:
    def __init__(self, clientes):
        self.clientes = clientes

    def client(self, servicio, region_name=None, config=None):
        return self.clientes[servicio]


def test_recolectar_nunca_aborta_y_clasifica_avisos():
    denegado = _error("AccessDeniedException")
    sesion = SesionFalsa({
        "ce": Falso(get_reservation_utilization=_error("DataUnavailableException"),
                    get_anomaly_monitors=_error("ThrottlingException")),
        "savingsplans": Falso(describe_savings_plans=denegado),
        "cost-optimization-hub": Falso(list_enrollment_statuses=denegado),
        "invoicing": Falso(list_invoice_summaries=denegado),
    })
    f = finops.recolectar(sesion, {MGMT: "m"}, MGMT, {"moneda": {"codigo": "eur", "tasa": "0.9"}}, hoy=HOY)
    assert f["savings_plans"] is None and f["reservas"] == {"utilizacion": None} and f["hub_optimizacion"] is None
    assert f["moneda_pago"] == {"codigo": "EUR", "tasa": 0.9, "periodo": None, "fuente": "config", "por_entidad": []}
    tipos = {a["seccion"]: a["tipo"] for a in f["avisos"]}
    # Sin datos (no hay RIs) no es un aviso; sin permiso y error sí, por separado.
    assert tipos == {"savings_plans": "sin_permiso", "anomalias": "error", "hub_optimizacion": "sin_permiso",
                     "moneda_pago": "sin_permiso"}


def test_moneda_config_invalida_se_ignora():
    assert finops._moneda_config({"moneda": {"codigo": "EUR", "tasa": "x"}}) is None
    assert finops._moneda_config({"moneda": {"codigo": "EUR", "tasa": -1}}) is None
    assert finops._moneda_config({}) is None


def test_moneda_varias_monedas_gana_la_de_mas_peso_y_pagina():
    def resumenes(Selector, Filter, NextToken=None):  # noqa: N803
        if Filter["BillingPeriod"] != {"Month": 9, "Year": 2026}:
            return {"InvoiceSummaries": []}
        if NextToken is None:
            return {"InvoiceSummaries": [_factura("AWS EMEA SARL", 900.0, tasa="0.87")], "NextToken": "n"}
        return {"InvoiceSummaries": [_factura("AWS Marketplace", 50.0, moneda="USD")]}
    m = finops.moneda_de_pago(Falso(list_invoice_summaries=resumenes), MGMT, HOY)
    assert (m["codigo"], m["tasa"]) == ("EUR", 0.87) and [e["entidad"] for e in m["por_entidad"]] == ["AWS EMEA SARL"]


def test_coh_ignora_montos_en_otra_moneda():
    coh = Falso(list_enrollment_statuses={"items": [{"accountId": MGMT, "status": "Active"}]},
                get_preferences={"savingsEstimationMode": "AfterDiscounts"},
                list_recommendations=[{"items": [{"accountId": MGMT, "currencyCode": "CNY", "actionType": "Delete",
                                                  "currentResourceType": "EbsVolume", "estimatedMonthlySavings": 9}]}])
    assert finops.cost_optimization_hub(coh, {MGMT}, MGMT)["recomendaciones"] == []


def test_moneda_config_exige_codigo_iso():
    assert finops._moneda_config({"moneda": {"codigo": "E", "tasa": 0.9}}) is None
    assert finops._moneda_config({"moneda": {"codigo": "<b>", "tasa": 0.9}}) is None
    assert finops._moneda_config({"moneda": {"codigo": "gbp", "tasa": 0.8}})["codigo"] == "GBP"


def test_plan_recien_comprado_sin_datos_conserva_activos():
    ce = Falso(get_savings_plans_utilization=_error("DataUnavailableException"))
    s = finops.savings_plans(Falso(describe_savings_plans=_sp), ce, HOY)
    assert [a["id"] for a in s["activos"]] == ["sp-1", "sp-2"] and s["utilizacion"] is None and s["en_cola"]


def test_tasa_sin_redondear_antes_de_ponderar():
    def resumenes(Selector, Filter, NextToken=None):  # noqa: N803
        if Filter["BillingPeriod"] == {"Month": 9, "Year": 2026}:
            return {"InvoiceSummaries": [_factura("A", 100.0, tasa="0.87654321")]}
        return {"InvoiceSummaries": []}
    assert finops.moneda_de_pago(Falso(list_invoice_summaries=resumenes), MGMT, HOY)["tasa"] == 0.876543
