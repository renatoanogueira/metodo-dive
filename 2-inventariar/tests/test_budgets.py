"""Colector de AWS Budgets con Stubber. Sin llamadas a AWS."""

import sys
from pathlib import Path

import boto3
from botocore.stub import Stubber

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

from colectores import budgets  # noqa: E402

CUENTA = "111111111111"


class Sesion:
    def __init__(self):
        self.cliente = boto3.client("budgets", region_name="us-east-1", aws_access_key_id="x",
                                    aws_secret_access_key="x")
        self.stub = Stubber(self.cliente)

    def client(self, servicio, region_name=None, config=None):
        return self.cliente


def test_budgets_con_filtros_viejos_y_nuevos():
    s = Sesion()
    s.stub.add_response("describe_budgets", {"Budgets": [
        {"BudgetName": "mensual", "BudgetType": "COST", "TimeUnit": "MONTHLY",
         "BudgetLimit": {"Amount": "1000.0", "Unit": "USD"},
         "CalculatedSpend": {"ActualSpend": {"Amount": "812.345", "Unit": "USD"},
                             "ForecastedSpend": {"Amount": "990", "Unit": "USD"}},
         "CostFilters": {"LinkedAccount": ["222222222222"]}},
        {"BudgetName": "expr", "BudgetType": "COST", "TimeUnit": "MONTHLY",
         "FilterExpression": {"And": [{"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": ["333333333333"]}},
                                      {"Not": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": ["444444444444"]}}},
                                      {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon EC2"]}}]}},
        {"BudgetName": "total", "BudgetType": "COST", "TimeUnit": "MONTHLY"},
    ]}, {"AccountId": CUENTA})
    with s.stub:
        r = budgets.collect(s, "us-east-1", CUENTA)
    a = {x["id"]: x["attrs"] for x in r}
    assert a["mensual"]["limit_usd"] == 1000.0 and a["mensual"]["actual_usd"] == 812.35
    assert a["mensual"]["linked_accounts"] == ["222222222222"]
    assert a["expr"]["linked_accounts"] == ["333333333333"] and a["expr"]["limit_usd"] is None  # el Not excluye, no cubre
    assert a["mensual"]["filtrado"] and a["expr"]["filtrado"] and not a["total"]["filtrado"]
    assert all(x["region"] == "global" and x["type"] == "budget" for x in r)
