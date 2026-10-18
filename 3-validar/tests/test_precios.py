"""Precios por región (FinOps bloque A): elección del producto en respuestas reales de la Price List API
(grabadas en fixtures/pricing, datos públicos) y reglas que calculan con ellos. Sin llamadas a AWS.

Uso: .venv/bin/python -m pytest 3-validar/tests -q
"""

import json
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

import validar  # noqa: E402,F401  (agrega RAIZ al path)
import reglas  # noqa: E402
from common.precios import CONSULTAS, elegir, obtener_precios, precio  # noqa: E402
from hallazgos import completar_v2  # noqa: E402

RESPUESTAS = json.loads((AQUI / "fixtures" / "pricing" / "get_products.json").read_text())
CUENTA = "111111111111"


def _elegir(region, clave):
    _, _, sufijo, unidad = CONSULTAS[clave]
    return elegir(RESPUESTAS[region][clave], sufijo, unidad)


def test_elegir_precios_reales_eu_west_1():
    esperado = {"ebs_gb_mes:gp2": 0.11, "ebs_gb_mes:gp3": 0.088, "ebs_gb_mes:io1": 0.138,
                "ebs_gb_mes:io2": 0.138, "ebs_gb_mes:st1": 0.05, "ebs_gb_mes:sc1": 0.0168,
                "ebs_gb_mes:standard": 0.055, "snapshot_gb_mes": 0.05, "ipv4_ociosa_hora": 0.005,
                "logs_gb_mes": 0.03, "rds_gb_mes:gp2": 0.127, "rds_gb_mes:gp3": 0.127, "alb_hora": 0.0252}
    assert {k: _elegir("eu-west-1", k)["usd"] for k in esperado} == esperado


def test_elegir_us_east_1_sin_prefijo_de_region():
    assert _elegir("us-east-1", "ebs_gb_mes:gp3")["usd"] == 0.08
    assert _elegir("us-east-1", "alb_hora")["usd"] == 0.0225


def test_elegir_descarta_variantes_del_producto():
    # EU-TS-LoadBalancerUsage (0.0056) también termina en el sufijo: gana el producto base.
    elegido = _elegir("eu-west-1", "alb_hora")
    assert elegido["usagetype"] == "EU-LoadBalancerUsage"


def test_elegir_unidad_con_alias():
    # io2 publica la unidad como "GB-month", no "GB-Mo".
    assert _elegir("eu-west-1", "ebs_gb_mes:io2") is not None
    assert elegir(RESPUESTAS["eu-west-1"]["ebs_gb_mes:gp3"], "EBS:VolumeUsage.gp3", "Hrs") is None


def test_precio_con_y_sin_api():
    precios = {"eu-west-1": {"snapshot_gb_mes": {"usd": 0.05}}}
    assert precio(precios, "eu-west-1", "snapshot_gb_mes", 0.04) == (0.05, "pricing_api")
    assert precio(precios, "us-east-2", "snapshot_gb_mes", 0.04) == (0.04, "static_estimate")
    assert precio({}, "eu-west-1", "x") == (None, None)


class _PricingFalso:
    """Cliente pricing mínimo: responde con el fixture según el regionCode del filtro."""

    def __init__(self, falla=()):
        self.llamadas, self.falla = 0, set(falla)

    def get_paginator(self, _):
        return self

    def paginate(self, ServiceCode, Filters):  # noqa: N803 (firma de boto3)
        self.llamadas += 1
        # Como la API: todos los productos del servicio que cumplen los filtros (varias claves comparten
        # filtros, ej. las dos de IPv4), y la regla elige por sufijo.
        region = next(f["Value"] for f in Filters if f["Field"] == "regionCode")
        campos, lista = {f["Field"]: f["Value"] for f in Filters}, []
        for clave, (codigo, filtros, _, _) in CONSULTAS.items():
            if codigo == ServiceCode and filtros == {k: v for k, v in campos.items() if k != "regionCode"}:
                if clave in self.falla:
                    raise RuntimeError("Throttling")
                lista += RESPUESTAS[region][clave]
        return [{"PriceList": lista}]


def test_obtener_precios_cache_y_errores(tmp_path, monkeypatch):
    import common.precios as mod
    falso = _PricingFalso(falla={"snapshot_gb_mes"})
    monkeypatch.setattr(mod, "client", lambda *a, **k: falso)
    cache = tmp_path / ".precios" / "cache.json"
    pedidos = {"eu-west-1": {"ebs_gb_mes:gp3", "snapshot_gb_mes", "clave_desconocida"}}

    precios, errores = obtener_precios(None, pedidos, cache)
    assert precios["eu-west-1"]["ebs_gb_mes:gp3"]["usd"] == 0.088
    assert precios["eu-west-1"]["ebs_gb_mes:gp3"]["fuente"] == "pricing_api"
    assert "snapshot_gb_mes" not in precios["eu-west-1"]
    assert [e["collector"] for e in errores] == ["precios/snapshot_gb_mes"]

    # Segunda corrida del mismo día: gp3 sale de la cache; snapshot (que falló) se vuelve a pedir.
    antes = falso.llamadas
    precios2, _ = obtener_precios(None, pedidos, cache)
    assert precios2["eu-west-1"]["ebs_gb_mes:gp3"]["usd"] == 0.088
    assert falso.llamadas - antes == 1


def _inventario():
    def r(tipo, rid, state=None, **attrs):
        return {"account_id": CUENTA, "region": "eu-west-1", "service": "ec2", "type": tipo, "id": rid,
                "state": state, "attrs": attrs, "relations": {}, "tags": {}}
    return {"generated_at": "2026-10-18T00:00:00Z", "resources": [
        r("volume", "vol-suelto", "available", size_gb=100, volume_type="gp3"),
        r("volume", "vol-gp2", "in-use", size_gb=100, volume_type="gp2"),
        r("elastic_ip", "eipalloc-1", None, public_ip="203.0.113.1", associated=False),
        r("snapshot", "snap-viejo", None, size_gb=10, start_time="2025-01-01T00:00:00Z"),
    ]}


def _precios_eu():
    return {"eu-west-1": {k: _elegir("eu-west-1", k) for k in
                          ("ebs_gb_mes:gp2", "ebs_gb_mes:gp3", "ipv4_ociosa_hora", "snapshot_gb_mes")}}


def test_precios_necesarios():
    pedidos = reglas.precios_necesarios(_inventario())
    assert pedidos == {"eu-west-1": {"ebs_gb_mes:gp2", "ebs_gb_mes:gp3", "ipv4_ociosa_hora", "snapshot_gb_mes"}}


def _por_recurso(findings):
    return {f["resource_id"]: completar_v2(f) for f in findings if f["monthly_usd"] is not None}


def test_reglas_con_precio_de_la_region():
    f = _por_recurso(reglas.aplicar(_inventario(), _precios_eu()))
    assert round(f["vol-suelto"]["monthly_usd"], 4) == 8.8          # 100 GB × 0.088
    assert round(f["vol-gp2"]["monthly_usd"], 4) == 2.2             # 100 GB × (0.11 − 0.088)
    assert round(f["eipalloc-1"]["monthly_usd"], 4) == 3.65         # 0.005/h × 730 h
    assert round(f["snap-viejo"]["monthly_usd"], 4) == 0.5          # 10 GB × 0.05
    assert {x["price_source"] for x in f.values()} == {"pricing_api"}
    assert {x["confidence"] for x in f.values()} == {"media"}
    assert "Price List API" in f["vol-suelto"]["detail"]


def test_reglas_sin_precios_usan_respaldo():
    f = _por_recurso(reglas.aplicar(_inventario()))
    assert round(f["vol-suelto"]["monthly_usd"], 4) == 8.0
    assert round(f["vol-gp2"]["monthly_usd"], 4) == 2.0
    assert {x["price_source"] for x in f.values()} == {"static_estimate"}
    assert {x["confidence"] for x in f.values()} == {"baja"}


def test_gp2_no_mezcla_fuentes():
    # Con gp2 de la API pero sin gp3, la diferencia sale toda del respaldo (no 0.11 − 0.08).
    precios = {"eu-west-1": {"ebs_gb_mes:gp2": _elegir("eu-west-1", "ebs_gb_mes:gp2")}}
    f = _por_recurso(reglas.aplicar(_inventario(), precios))
    assert round(f["vol-gp2"]["monthly_usd"], 4) == 2.0
    assert f["vol-gp2"]["price_source"] == "static_estimate"


def test_monto_informativo_no_es_ahorro():
    from hallazgos import hallazgo
    f = completar_v2(hallazgo("costo", "baja", CUENTA, "eu-west-1", "x", "t", "d", 3.65, "regla",
                              rule_id="regla.x", price_source="pricing_api", ahorro=False))
    assert f["is_saving"] is False and f["price_source"] == "pricing_api" and "_no_ahorro" not in f


def test_dedup_prefiere_precio_de_la_api():
    from hallazgos import hallazgo
    doc = hallazgo("desperdicio", "media", CUENTA, "eu-west-1", "eipalloc-1", "EIP", "d", 3.65, "aws-doctor",
                   rule_id="aws-doctor.unused_elastic_ips")
    regla = hallazgo("desperdicio", "media", CUENTA, "eu-west-1", "eipalloc-1", "EIP", "d", 3.65, "regla",
                     rule_id="regla.eip_sin_asociar", price_source="pricing_api")
    [f] = validar.deduplicar([doc, regla])
    assert f["source"] == "regla" and f["price_source"] == "pricing_api"
    [f] = validar.deduplicar([doc, {**regla, "price_source": "static_estimate"}])
    assert f["source"] == "aws-doctor"


def test_dedup_no_depende_de_la_fuente_de_precio():
    # Mismo recurso, dos problemas de ahorro distintos: sobrevive el mismo con o sin precio de la API.
    from hallazgos import hallazgo
    doc = hallazgo("desperdicio", "baja", CUENTA, "eu-west-1", "vol-1", "t", "d", 5.0, "aws-doctor",
                   rule_id="aws-doctor.stopped_instance_volumes")
    for fuente in ("pricing_api", "static_estimate"):
        regla = hallazgo("desperdicio", "media", CUENTA, "eu-west-1", "vol-1", "t", "d", 8.8, "regla",
                         rule_id="regla.ebs_sin_adjuntar", price_source=fuente)
        assert [f["rule_id"] for f in validar.deduplicar([doc, regla])] == ["aws-doctor.stopped_instance_volumes"]


def test_completar_v2_idempotente_con_informativo():
    from hallazgos import hallazgo
    f = hallazgo("costo", "baja", CUENTA, "eu-west-1", None, "t", "d", 3.65, "regla", rule_id="regla.x", ahorro=False)
    assert completar_v2(completar_v2(f))["is_saving"] is False


def test_cache_rara_se_ignora(tmp_path, monkeypatch):
    import common.precios as mod
    falso = _PricingFalso()
    monkeypatch.setattr(mod, "client", lambda *a, **k: falso)
    cache = tmp_path / "cache.json"
    for contenido in ("[]", "{roto", json.dumps({"fecha": "2000-01-01", "precios": {"eu-west-1": {
            "ebs_gb_mes:gp3": {"usd": 9}}}}), json.dumps({"fecha": None, "precios": []})):
        cache.write_text(contenido)
        precios, errores = obtener_precios(None, {"eu-west-1": {"ebs_gb_mes:gp3"}}, cache)
        assert precios["eu-west-1"]["ebs_gb_mes:gp3"]["usd"] == 0.088 and not errores
    # Precio de hoy con forma inválida: se vuelve a pedir en vez de romper la regla.
    hoy = json.loads(cache.read_text())["fecha"]
    cache.write_text(json.dumps({"fecha": hoy, "precios": {"eu-west-1": {"ebs_gb_mes:gp3": {"usd": "x"}}}}))
    precios, _ = obtener_precios(None, {"eu-west-1": {"ebs_gb_mes:gp3"}}, cache)
    assert precios["eu-west-1"]["ebs_gb_mes:gp3"]["usd"] == 0.088
    assert precio({"r": {"k": {"usd": "x"}}}, "r", "k", 0.1) == (0.1, "static_estimate")


def test_acceso_denegado_corta_y_conserva_la_cache(tmp_path, monkeypatch):
    import common.precios as mod

    class Denegado(_PricingFalso):
        def paginate(self, **_):
            self.llamadas += 1
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetProducts")

    falso = Denegado()
    monkeypatch.setattr(mod, "client", lambda *a, **k: falso)
    cache = tmp_path / "cache.json"
    hoy = mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d")
    cache.write_text(json.dumps({"fecha": hoy, "precios": {"us-east-1": {"alb_hora": {"usd": 0.0225}}}}))
    precios, errores = obtener_precios(None, {"eu-west-1": {"ebs_gb_mes:gp2", "ebs_gb_mes:gp3", "alb_hora"}}, cache)
    assert precios == {} and len(errores) == 1 and falso.llamadas == 1
    assert json.loads(cache.read_text())["precios"]["us-east-1"]["alb_hora"]["usd"] == 0.0225


def _run_inventario():
    inv = _inventario()
    inv["run"] = {"run_id": "2026-10-18T000000Z", "organization_id": "o-test", "org_alias": "o-test"}
    inv["accounts"] = {CUENTA: {"name": "test"}}
    return inv


def test_sin_sesion_la_fase_sigue_y_el_error_queda_en_findings(tmp_path, monkeypatch):
    import common.aws as aws

    def sin_perfiles(cfg):
        raise SystemExit("Faltan los perfiles dive-*. Corre: python setup/generar_perfiles.py")

    monkeypatch.setattr(aws, "sesion_management", sin_perfiles)
    findings, errores = validar.validar({"access_mode": "sso"}, {"accounts": {}}, _run_inventario(), tmp_path,
                                        sin_doctor=True, consultar_precios=True)
    datos = json.loads((tmp_path / "findings.json").read_text())
    assert [e["collector"] for e in datos["errors"]] == ["precios"]
    assert "· precios: SystemExit: Faltan los perfiles" in (tmp_path / "findings.md").read_text()
    assert datos["run"]["flags"]["validar"]["precios"] == "static_estimate"
    assert {f["price_source"] for f in findings if f["monthly_usd"] is not None} == {"static_estimate"}
    assert json.loads((tmp_path / "precios.json").read_text())["cobertura"] == "static_estimate"


def test_precios_ok_quedan_en_la_corrida(tmp_path, monkeypatch):
    import common.aws as aws
    import common.precios as mod
    monkeypatch.setattr(aws, "sesion_management", lambda cfg: None)
    monkeypatch.setattr(mod, "client", lambda *a, **k: _PricingFalso())
    (tmp_path / "runs").mkdir()
    corrida = tmp_path / "runs" / "c1"
    corrida.mkdir()
    findings, errores = validar.validar({"access_mode": "sso"}, {"accounts": {}}, _run_inventario(), corrida,
                                        sin_doctor=True, consultar_precios=True)
    datos = json.loads((corrida / "findings.json").read_text())
    assert datos["run"]["flags"]["validar"]["precios"] == "pricing_api" and not errores
    assert set(json.loads((corrida / "precios.json").read_text())["precios"]["eu-west-1"]) == \
        reglas.precios_necesarios(_inventario())["eu-west-1"]
    assert (tmp_path / "runs" / ".precios" / "cache.json").exists()


def test_main_sin_precios_no_toca_aws(tmp_path, monkeypatch):
    import common.aws as aws

    def prohibido(cfg):
        raise AssertionError("no debía crear sesión")

    monkeypatch.setattr(aws, "sesion_management", prohibido)
    (tmp_path / "inventory.json").write_text(json.dumps(_run_inventario()))
    (tmp_path / "discovery.json").write_text(json.dumps({"accounts": {}}))
    monkeypatch.setattr(validar, "cargar_config", lambda ruta: {"access_mode": "sso"})
    monkeypatch.setattr(sys, "argv", ["validar.py", "--inventory", str(tmp_path / "inventory.json"),
                                      "--sin-doctor", "--sin-precios"])
    validar.main()
    assert json.loads((tmp_path / "findings.json").read_text())["run"]["flags"]["validar"]["precios"] == "static_estimate"


def _cortar_con(error):
    """Cliente pricing que falla siempre con `error`; cuenta las llamadas."""
    class Falla(_PricingFalso):
        def paginate(self, **_):
            self.llamadas += 1
            raise error
    return Falla()


def test_errores_globales_cortan_en_la_primera_llamada(tmp_path, monkeypatch):
    import common.precios as mod
    from botocore.exceptions import ClientError, EndpointConnectionError, UnauthorizedSSOTokenError
    pedidos = {r: {"ebs_gb_mes:gp2", "ebs_gb_mes:gp3", "snapshot_gb_mes"} for r in ("eu-west-1", "us-east-1")}
    for error in (UnauthorizedSSOTokenError(),
                  EndpointConnectionError(endpoint_url="https://api.pricing.us-east-1.amazonaws.com"),
                  ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetProducts")):
        falso = _cortar_con(error)
        monkeypatch.setattr(mod, "client", lambda *a, **k: falso)
        precios, errores = obtener_precios(None, pedidos, tmp_path / "cache.json")
        assert (precios, len(errores), falso.llamadas) == ({}, 1, 1), type(error).__name__


def test_error_de_una_clave_no_corta_las_demas(monkeypatch):
    import common.precios as mod
    from botocore.exceptions import ClientError
    falso = _cortar_con(ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "GetProducts"))
    monkeypatch.setattr(mod, "client", lambda *a, **k: falso)
    _, errores = obtener_precios(None, {"eu-west-1": {"ebs_gb_mes:gp2", "ebs_gb_mes:gp3"}})
    assert falso.llamadas == 2 and len(errores) == 2


def test_cliente_pricing_con_pocos_reintentos():
    import common.precios as mod
    assert mod._CONFIG.retries["max_attempts"] <= 3 and mod._CONFIG.connect_timeout <= 5


def test_volume_type_desconocido_no_se_pide_ni_deja_parcial(tmp_path, monkeypatch):
    import common.aws as aws
    import common.precios as mod
    inv = _run_inventario()
    inv["resources"][0]["attrs"]["volume_type"] = "gp9"
    assert all(k in CONSULTAS for c in reglas.precios_necesarios(inv).values() for k in c)
    monkeypatch.setattr(aws, "sesion_management", lambda cfg: None)
    monkeypatch.setattr(mod, "client", lambda *a, **k: _PricingFalso())
    _, _, cobertura = validar.precios_de_la_corrida({"access_mode": "sso"}, inv, tmp_path / "c1")
    assert cobertura == "pricing_api"


def test_precios_json_viejo_se_reemplaza(tmp_path):
    (tmp_path / "precios.json").write_text(json.dumps({"precios": {"eu-west-1": {"x": {"usd": 1}}}}))
    validar.validar({"access_mode": "sso"}, {"accounts": {}}, _run_inventario(), tmp_path, sin_doctor=True)
    assert json.loads((tmp_path / "precios.json").read_text())["precios"] == {}


def test_sin_nada_que_cotizar_es_na(tmp_path):
    inv = {**_run_inventario(), "resources": []}
    validar.validar({"access_mode": "sso"}, {"accounts": {}}, inv, tmp_path, sin_doctor=True, consultar_precios=True)
    assert json.loads((tmp_path / "findings.json").read_text())["run"]["flags"]["validar"]["precios"] == "n/a"


def test_usd_invalido_en_cache():
    from common.precios import _valido
    assert not any(_valido({"usd": v}) for v in (True, float("inf"), float("nan"), 0, -1, "0.1", None))
    assert _valido({"usd": 0.088})


def test_cache_marca_origen_y_filtra_campos(tmp_path, monkeypatch):
    import common.precios as mod
    monkeypatch.setattr(mod, "client", lambda *a, **k: _PricingFalso())
    cache = tmp_path / "cache.json"
    primero, _ = obtener_precios(None, {"eu-west-1": {"ebs_gb_mes:gp3"}}, cache)
    assert "cache" not in primero["eu-west-1"]["ebs_gb_mes:gp3"]
    datos = json.loads(cache.read_text())
    datos["precios"]["eu-west-1"]["ebs_gb_mes:gp3"].update(extra="<script>", descripcion="x" * 500)
    datos["precios"]["eu-west-1"]["ebs_gb_mes:gp2"] = {"usd": 5000}  # fuera de rango: se ignora
    cache.write_text(json.dumps(datos))
    segundo, _ = obtener_precios(None, {"eu-west-1": {"ebs_gb_mes:gp3", "ebs_gb_mes:gp2"}}, cache)
    p = segundo["eu-west-1"]["ebs_gb_mes:gp3"]
    assert p["cache"] and "extra" not in p and len(p["descripcion"]) == 120
    assert segundo["eu-west-1"]["ebs_gb_mes:gp2"]["usd"] == 0.11
    assert "cache" not in json.loads(cache.read_text())["precios"]["eu-west-1"]["ebs_gb_mes:gp3"]
