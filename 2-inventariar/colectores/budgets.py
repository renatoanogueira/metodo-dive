"""AWS Budgets de cada cuenta: nombre, tipo, límite, gasto y a qué cuentas filtra (los budgets son por cuenta;
uno en la management puede vigilar cuentas miembro con un filtro LinkedAccount). Solo metadatos de lectura
(budgets:ViewBudget); las direcciones de las notificaciones no se leen.
"""

from common import recurso
from common.aws import client

NAME = "budgets"
TRIGGERS = []
ALWAYS = True   # un budget no factura: el billing nunca lo activa
GLOBAL = True
TAGS = False    # la regla "sin tag" no aplica a budgets


def _cuentas_filtradas(budget):
    """Cuentas a las que se limita el budget: CostFilters.LinkedAccount (API vieja) o una dimensión
    LINKED_ACCOUNT en FilterExpression. Vacío = sin filtro de cuenta."""
    cuentas = set((budget.get("CostFilters") or {}).get("LinkedAccount") or [])
    pendientes = [budget.get("FilterExpression") or {}]
    while pendientes:
        e = pendientes.pop()
        dim = e.get("Dimensions") or {}
        if dim.get("Key") == "LINKED_ACCOUNT":
            cuentas.update(dim.get("Values") or [])
        # Un Not excluye cuentas: no las cubre (no se recorre).
        pendientes += [x for k in ("And", "Or") for x in e.get(k) or []]
    return sorted(cuentas)


def _monto(m):
    try:
        return round(float((m or {}).get("Amount")), 2)
    except (TypeError, ValueError):
        return None


def collect(session, region, account_id):
    budgets = client(session, "budgets", "us-east-1")
    recursos = []
    for pagina in budgets.get_paginator("describe_budgets").paginate(AccountId=account_id):
        for b in pagina.get("Budgets", []):
            gasto = b.get("CalculatedSpend") or {}
            recursos.append(recurso(
                account_id, "global", NAME, "budget", b["BudgetName"],
                arn=f"arn:aws:budgets::{account_id}:budget/{b['BudgetName']}", name=b["BudgetName"],
                attrs={
                    "budget_type": b.get("BudgetType"),
                    "time_unit": b.get("TimeUnit"),
                    "limit_usd": _monto(b.get("BudgetLimit")) if (b.get("BudgetLimit") or {}).get("Unit") == "USD" else None,
                    "actual_usd": _monto(gasto.get("ActualSpend")),
                    "forecast_usd": _monto(gasto.get("ForecastedSpend")),
                    "linked_accounts": _cuentas_filtradas(b),
                    # Con cualquier filtro (servicio, tag...) no vigila el total aunque no filtre cuentas.
                    "filtrado": bool(b.get("CostFilters") or b.get("FilterExpression")),
                },
            ))
    return recursos
