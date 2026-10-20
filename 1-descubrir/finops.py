"""Datos FinOps de la organización, desde la cuenta management: compromisos (Savings Plans y reservas),
detección de anomalías, recomendaciones de Cost Optimization Hub y la moneda en la que se paga la factura.

Todo es lectura y nada aborta la fase 1: cada sección va por separado y, si falla, queda un aviso que dice si
fue falta de permiso, algo no habilitado en la cuenta o un error. Los montos van en claves con "_usd" para que
el anonimizador los trate como costos; de las facturas solo se guardan tasas de cambio y el peso de cada
entidad, no los totales.

Uso: from finops import recolectar; finops = recolectar(session, cuentas, management_id, cfg)
"""

import re
from datetime import date, timedelta

from common import error_corto
from common.aws import client

DIAS = 30  # ventana de utilización y cobertura de los compromisos


def _num(valor):
    try:
        return round(float(valor), 4)
    except (TypeError, ValueError):
        return None


def _tipo_error(e):
    codigo = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
    texto = f"{type(e).__name__} {codigo} {e}"
    if "AccessDenied" in texto or "UnauthorizedOperation" in texto or "not authorized" in texto:
        return "sin_permiso"
    if codigo in ("DataUnavailableException",):
        return "sin_datos"
    return "error"


def _seccion(nombre, fn, avisos):
    """Corre una sección; si falla, deja el aviso (sin_permiso / sin_datos / error) y devuelve None."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — una sección que falla no tumba la fase 1
        tipo = _tipo_error(e)
        if tipo != "sin_datos":
            avisos.append({"seccion": nombre, "tipo": tipo, "error": error_corto(e)})
        return None


def _periodo(hoy):
    fin = hoy
    return {"Start": (fin - timedelta(days=DIAS)).isoformat(), "End": fin.isoformat()}


# ---------- compromisos ----------

def _paginar_sp(sp, estados):
    planes, token = [], None
    while True:
        r = sp.describe_savings_plans(states=estados, **({"nextToken": token} if token else {}))
        planes += r.get("savingsPlans", [])
        token = r.get("nextToken")
        if not token:
            return planes


def savings_plans(sp, ce, hoy):
    activos = []
    for x in _paginar_sp(sp, ["active"]):
        activos.append({
            "id": x.get("savingsPlanId"),
            "tipo": x.get("savingsPlanType"),
            "pago": x.get("paymentOption"),
            "region": x.get("region") or None,
            "compromiso_usd_hora": _num(x.get("commitment")),
            "inicio": (x.get("start") or "")[:10],
            "fin": (x.get("end") or "")[:10],
        })
    # Renovaciones ya compradas que empiezan más adelante: con ellas, un vencimiento no es un aviso.
    en_cola = [{"tipo": x.get("savingsPlanType"), "region": x.get("region") or None, "inicio": (x.get("start") or "")[:10]}
               for x in _paginar_sp(sp, ["queued", "payment-pending"])]
    salida = {"activos": sorted(activos, key=lambda a: a["fin"]), "en_cola": en_cola, "utilizacion": None,
              "cobertura": None}
    if not activos:
        return salida
    try:
        return _uso_y_cobertura(ce, _periodo(hoy), salida)
    except Exception as e:  # noqa: BLE001
        if _tipo_error(e) == "sin_datos":  # un plan recién comprado aún sin datos: se conservan activos y en_cola
            return salida
        raise


def _uso_y_cobertura(ce, p, salida):
    u = ce.get_savings_plans_utilization(TimePeriod=p)["Total"]
    salida["utilizacion"] = {
        "periodo": p,
        "compromiso_usd": _num(u["Utilization"].get("TotalCommitment")),
        "usado_usd": _num(u["Utilization"].get("UsedCommitment")),
        "no_usado_usd": _num(u["Utilization"].get("UnusedCommitment")),
        "porcentaje": _num(u["Utilization"].get("UtilizationPercentage")),
        "ahorro_neto_usd": _num((u.get("Savings") or {}).get("NetSavings")),
    }
    # Sin Granularity, Cost Explorer devuelve un solo período recortado; MONTHLY parte la ventana en el cambio
    # de mes: se suman todos los períodos (y páginas) y se recalcula el porcentaje.
    on_demand = cubierto = 0.0
    token = None
    while True:
        r = ce.get_savings_plans_coverage(TimePeriod=p, Granularity="MONTHLY", **({"NextToken": token} if token else {}))
        for x in r.get("SavingsPlansCoverages") or []:
            cov = x.get("Coverage") or {}
            on_demand += _num(cov.get("OnDemandCost")) or 0
            cubierto += _num(cov.get("SpendCoveredBySavingsPlans")) or 0
        token = r.get("NextToken")
        if not token:
            break
    if on_demand + cubierto > 0:
        salida["cobertura"] = {"periodo": p, "porcentaje": round(100 * cubierto / (on_demand + cubierto), 2),
                               "on_demand_usd": round(on_demand, 4), "cubierto_usd": round(cubierto, 4)}
    return salida


def reservas(ce, hoy):
    p = _periodo(hoy)
    try:
        u = ce.get_reservation_utilization(TimePeriod=p).get("Total") or {}
    except Exception as e:  # noqa: BLE001
        if _tipo_error(e) == "sin_datos":  # sin RIs: la sección se leyó y no hay nada (no es "no leída")
            return {"utilizacion": None}
        raise
    horas = _num(u.get("PurchasedHours")) or 0
    if not horas:
        return {"utilizacion": None}
    return {"utilizacion": {"periodo": p, "porcentaje": _num(u.get("UtilizationPercentage")),
                            "horas_compradas": horas, "horas_sin_usar": _num(u.get("UnusedHours")),
                            "costo_horas_sin_usar_usd": _num(u.get("RICostForUnusedHours"))}}


def anomalias(ce):
    monitores, token = [], None
    while True:
        r = ce.get_anomaly_monitors(**({"NextPageToken": token} if token else {}))
        monitores += [{"tipo": m.get("MonitorType"), "dimension": m.get("MonitorDimension")}
                      for m in r.get("AnomalyMonitors", [])]
        token = r.get("NextPageToken")
        if not token:
            break
    subs, token = 0, None
    while True:  # solo se cuentan: las suscripciones traen emails
        r = ce.get_anomaly_subscriptions(**({"NextPageToken": token} if token else {}))
        subs += len(r.get("AnomalySubscriptions", []))
        token = r.get("NextPageToken")
        if not token:
            break
    return {"monitores": monitores, "suscripciones": subs}


# ---------- Cost Optimization Hub ----------

def cost_optimization_hub(coh, cuentas, management_id):
    estados = coh.list_enrollment_statuses().get("items", [])
    estado = next((e.get("status") for e in estados if e.get("accountId") == management_id),
                  estados[0].get("status") if estados else None)
    salida = {"estado": estado, "modo_estimacion": None, "recomendaciones": []}
    if estado != "Active":
        return salida
    salida["modo_estimacion"] = coh.get_preferences().get("savingsEstimationMode")
    for pagina in coh.get_paginator("list_recommendations").paginate():
        for r in pagina.get("items", []):
            if r.get("accountId") not in cuentas:  # include/exclude del config: el COH responde por toda la org
                continue
            if (r.get("currencyCode") or "USD") != "USD":  # los montos se guardan como _usd
                continue
            salida["recomendaciones"].append({
                "cuenta": r.get("accountId"),
                "region": r.get("region"),
                "resource_id": r.get("resourceId"),
                "tipo": r.get("currentResourceType"),
                "accion": r.get("actionType"),
                "ahorro_usd": _num(r.get("estimatedMonthlySavings")),
                "costo_actual_usd": _num(r.get("estimatedMonthlyCost")),
                "esfuerzo": r.get("implementationEffort"),
                "reinicio": r.get("restartNeeded"),
                "reversible": r.get("rollbackPossible"),
                "actual": r.get("currentResourceSummary"),
                "recomendado": r.get("recommendedResourceSummary"),
                "dias_analizados": r.get("recommendationLookbackPeriodInDays"),
                "origen": r.get("source"),
            })
    return salida


# ---------- moneda de pago ----------

def _mes_anterior(hoy, n):
    y, m = hoy.year, hoy.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return y, m


def moneda_de_pago(inv, management_id, hoy):
    """Tasa USD → moneda de pago del último mes con facturas emitidas. Varias entidades de facturación (AWS EMEA
    SARL, AWS Inc.) tienen tasas distintas: se pondera por lo facturado en USD por cada una."""
    for n in (1, 2, 3):
        y, m = _mes_anterior(hoy, n)
        facturas, token = [], None
        while True:
            r = inv.list_invoice_summaries(Selector={"ResourceType": "ACCOUNT_ID", "Value": management_id},
                                           Filter={"BillingPeriod": {"Month": m, "Year": y}},
                                           **({"NextToken": token} if token else {}))
            facturas += r.get("InvoiceSummaries", [])
            token = r.get("NextToken")
            if not token:
                break
        por_entidad = {}
        for x in facturas:
            pago = x.get("PaymentCurrencyAmount") or {}
            base = x.get("BaseCurrencyAmount") or {}
            cambio = pago.get("CurrencyExchangeDetails") or {}
            usd = _num(base.get("TotalAmountBeforeTax")) or 0
            if not pago.get("CurrencyCode") or base.get("CurrencyCode") != "USD" or usd <= 0:
                continue  # facturas sin moneda de pago (solo impuestos) o sin base en USD
            try:  # sin redondear: se pondera y se redondea al final
                tasa = float(cambio.get("Rate")) if pago["CurrencyCode"] != "USD" else 1.0
            except (TypeError, ValueError):
                continue
            if not tasa > 0:
                continue
            ent = (x.get("Entity") or {}).get("InvoicingEntity") or "?"
            acc = por_entidad.setdefault((ent, pago["CurrencyCode"]), {"usd": 0.0, "tasa_x_usd": 0.0})
            acc["usd"] += usd
            acc["tasa_x_usd"] += tasa * usd
        if not por_entidad:
            continue
        # Varias monedas (ej. una factura de Marketplace en USD junto a las de AWS en EUR): la de más peso.
        peso = {}
        for (_, c), v in por_entidad.items():
            peso[c] = peso.get(c, 0) + v["usd"]
        codigo = max(peso, key=peso.get)
        por_entidad = {k: v for k, v in por_entidad.items() if k[1] == codigo}
        total = sum(v["usd"] for v in por_entidad.values())
        return {
            "codigo": codigo,
            "tasa": round(sum(v["tasa_x_usd"] for v in por_entidad.values()) / total, 6),
            "periodo": f"{y}-{m:02d}",
            "fuente": "invoice",
            # Solo tasa y peso de cada entidad: el total facturado no sale de aquí.
            "por_entidad": [{"entidad": e, "tasa": round(v["tasa_x_usd"] / v["usd"], 6), "peso": round(v["usd"] / total, 4)}
                            for (e, _), v in sorted(por_entidad.items())],
        }
    return None


def _moneda_config(cfg):
    m = cfg.get("moneda") or {}
    tasa = _num(m.get("tasa"))
    codigo = str(m.get("codigo") or "").upper()
    if re.fullmatch(r"[A-Z]{3}", codigo) and tasa and tasa > 0:
        return {"codigo": codigo, "tasa": tasa, "periodo": None, "fuente": "config",
                "por_entidad": []}
    return None


# ---------- todo junto ----------

def recolectar(session, cuentas, management_id, cfg, hoy=None):
    hoy = hoy or date.today()
    avisos = []
    ce = client(session, "ce", "us-east-1")
    finops = {
        "savings_plans": _seccion("savings_plans",
                                  lambda: savings_plans(client(session, "savingsplans", "us-east-1"), ce, hoy), avisos),
        "reservas": _seccion("reservas", lambda: reservas(ce, hoy), avisos),
        "anomalias": _seccion("anomalias", lambda: anomalias(ce), avisos),
        "hub_optimizacion": _seccion(
            "hub_optimizacion",
            lambda: cost_optimization_hub(client(session, "cost-optimization-hub", "us-east-1"), set(cuentas),
                                          management_id), avisos),
    }
    moneda = _seccion("moneda_pago",
                      lambda: moneda_de_pago(client(session, "invoicing", "us-east-1"), management_id, hoy), avisos)
    finops["moneda_pago"] = moneda or _moneda_config(cfg)
    finops["avisos"] = avisos
    return finops
