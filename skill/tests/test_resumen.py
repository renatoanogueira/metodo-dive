"""resumen.py de la skill: totales iguales a findings.md, reversibilidad para todo rule_id que DIVE puede emitir,
contexto de tags e IaC, y la moneda solo como tasa. Sin llamadas a AWS.

Uso: .venv/bin/python -m pytest skill/tests -q
"""

import json
import re
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parents[1]
sys.path.insert(0, str(AQUI.parent / "scripts"))
sys.path.insert(0, str(RAIZ / "3-validar"))

import resumen  # noqa: E402
import validar  # noqa: E402  (agrega RAIZ y 2-inventariar al path)
import doctor  # noqa: E402
import reglas  # noqa: E402
import hallazgos_finops  # noqa: E402
from hallazgos import ISSUE_CANONICO  # noqa: E402

CUENTA, REGION = "111111111111", "eu-west-1"


def _rule_ids_posibles():
    """Todos los rule_id que el código puede emitir (por familia)."""
    ids = {f"regla.{r.__name__.removeprefix('regla_')}" for r in reglas.REGLAS} - {"regla.root"}
    ids |= set(re.findall(r'rule_id="(regla\.[a-z_]+)"', (RAIZ / "3-validar" / "reglas.py").read_text()))
    ids |= {f"aws-doctor.{c}" for c in doctor.CATEGORIAS}
    ids |= set(re.findall(r'"(cruce\.[a-z_]+)"', (RAIZ / "3-validar" / "validar.py").read_text()))
    ids |= {f"finops.{n}" for n in re.findall(r"\{FUENTE\}\.([a-z_]+)", (RAIZ / "3-validar" / "hallazgos_finops.py").read_text())}
    return ids


def test_todo_rule_id_tiene_reversibilidad_explicita():
    faltan = []
    for rid in sorted(_rule_ids_posibles()):
        issue = ISSUE_CANONICO.get(rid, rid)
        if issue not in resumen.REVERSIBILIDAD and rid not in resumen.REVERSIBILIDAD:
            faltan.append(rid)
    assert not faltan, f"sin entrada en resumen.REVERSIBILIDAD: {faltan}"
    # Y las claves de la tabla existen (un typo en la tabla no se notaría).
    validas = _rule_ids_posibles() | {ISSUE_CANONICO.get(r, r) for r in _rule_ids_posibles()}
    assert set(resumen.REVERSIBILIDAD) <= validas, set(resumen.REVERSIBILIDAD) - validas


def test_acciones_de_cost_optimization_hub():
    for accion in hallazgos_finops.ACCIONES:
        assert accion.lower() in resumen.REVERSIBILIDAD_COH, accion  # cada acción conocida tiene entrada
    assert resumen.reversibilidad({"rule_id": "cost-optimization-hub.ebs_sin_adjuntar", "issue_id": "ebs.unattached"}) == "reversible"
    assert resumen.reversibilidad({"rule_id": "cost-optimization-hub.purchasesavingsplans.computesavingsplans"}) == "compromiso"
    assert resumen.reversibilidad({"rule_id": "algo.nuevo"}) == "irreversible"  # desconocido nunca es "seguro"


def _r(tipo, rid, state=None, tags=None, **attrs):
    return {"account_id": CUENTA, "region": REGION, "service": "ebs" if tipo in ("volume", "snapshot") else "ec2",
            "type": tipo, "id": rid, "arn": None, "name": rid, "state": state, "attrs": attrs, "relations": {},
            "tags": tags or {}}


def _corrida(tmp_path, moneda=None):
    inv = {"resources": [
        _r("volume", "vol-1", "available", {"owner": "pagos", "Environment": "prod",
                                            "aws:cloudformation:stack-name": "s"}, size_gb=100, volume_type="gp2"),
        _r("elastic_ip", "eipalloc-1", None, {}, associated=False, public_ip="203.0.113.5"),
        {**_r("instance", "i-1", "running", {}, public_ip="198.51.100.1", instance_type="t3.micro")}],
        "generated_at": "2026-10-18T00:00:00Z", "errors": [], "skipped": []}
    inv["resources"][1]["service"] = "eip"
    findings = validar.ordenar_y_numerar(validar.deduplicar(reglas.aplicar(inv)))
    disc = {"accounts": {CUENTA: {"name": "test"}}}
    if moneda:
        disc["finops"] = {"moneda_pago": moneda, "savings_plans": {"activos": [{"fin": "2027-01-09"}]}}
    (tmp_path / "findings.json").write_text(json.dumps({"run": {"run_id": "r1"}, "generated_at": "x",
                                                        "findings": findings, "errors": []}))
    (tmp_path / "inventory.json").write_text(json.dumps(inv))
    (tmp_path / "discovery.json").write_text(json.dumps(disc))
    (tmp_path / "findings.md").write_text(validar.generar_md(findings, disc, []))
    return findings


def test_maximo_teorico_igual_a_findings_md(tmp_path):
    _corrida(tmp_path)
    r = resumen.resumir(tmp_path)
    total_md = re.search(r"\*\*\$([\d,.]+)/mes\*\*", (tmp_path / "findings.md").read_text()).group(1)
    assert r["maximo_teorico_usd"] == float(total_md.replace(",", ""))
    # El gp2→gp3 del mismo volumen no suma (ya cuenta como sin adjuntar): no aparece entre los candidatos.
    assert [c["rule_id"] for c in r["candidatos"]] == ["regla.ebs_sin_adjuntar", "regla.eip_sin_asociar"]
    assert r["ahorro_por_reversibilidad_usd"] == {"reversible": 10.0, "irreversible": 3.65}  # 100 GB gp2 × 0.10
    assert r["gasto_informativo_usd"] == 3.65 and r["gasto_informativo"][0]["rule_id"] == "regla.ipv4_publica"
    assert [s["rule_id"] for s in r["solapados"]] == ["regla.gp2"]  # mismo volumen: ni ahorro ni gasto


def test_contexto_de_tags_e_iac_sin_inventar(tmp_path):
    _corrida(tmp_path)
    c = {x["resource_id"]: x for x in resumen.resumir(tmp_path)["candidatos"]}
    assert (c["vol-1"]["dueno"], c["vol-1"]["entorno"], c["vol-1"]["iac"]) == ("pagos", "prod", "cloudformation")
    # Sin tags de IaC no se sabe si lo gestiona Terraform o Pulumi: "desconocido", nunca "sin IaC".
    assert (c["eipalloc-1"]["dueno"], c["eipalloc-1"]["iac"]) == (None, "desconocido")


def test_moneda_solo_tasa_y_lagunas(tmp_path):
    _corrida(tmp_path, moneda={"codigo": "EUR", "tasa": 0.878, "periodo": "2026-09", "fuente": "invoice",
                               "por_entidad": [{"entidad": "x", "tasa": 0.878, "peso": 1}]})
    r = resumen.resumir(tmp_path)
    assert r["finops"]["moneda_pago"] == {"codigo": "EUR", "tasa": 0.878, "periodo": "2026-09", "fuente": "invoice"}
    assert r["finops"]["savings_plans"]["primer_vencimiento"] == "2027-01-09"
    assert r["lagunas"]["sin_finops"] is False
    _corrida(tmp_path)
    assert resumen.resumir(tmp_path)["lagunas"]["sin_finops"] is True


def test_sin_findings_explica_que_correr(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(sys, "argv", ["resumen.py", str(tmp_path)])
    with pytest.raises(SystemExit, match="bash dive.sh"):
        resumen.main()


def test_el_formato_del_informe_se_anonimiza():
    """Las reglas del informe (montos como $1,234.56, sin montos convertidos) bastan para que el anonimizador
    oculte las cifras exactas al compartir."""
    sys.path.insert(0, str(RAIZ / "anonimizar"))
    import anonimizar as an
    informe = ("- Costo:          $1,234.56/mes → $0.00/mes = $1,234.56/mes   (fuente: pricing_api)\n"
               "Ahorro seguro ya: **$2,345.67/mes**. Se paga en EUR, tasa 0.878 por dólar, factura de 2026-09.\n"
               "- Acción: aws ec2 delete-volume --region eu-west-1 --volume-id vol-0abc1234 --profile acme-admin "
               "(cuenta 123456789012)")
    for modo in ("rounded", "percent"):
        a = an.Anonimizador(an.Mapeo(), ["acme"], modo, total_usd=10000.0, meses=1)
        t = a.texto(informe)
        assert "1,234.56" not in t and "2,345.67" not in t, (modo, t)
        assert "123456789012" not in t and "acme" not in t.lower()



def test_dueno_estable_y_sin_app():
    import subprocess
    f = {"account_id": CUENTA, "resource_id": "i-1"}
    idx = {(CUENTA, "i-1"): {"tags": {"app": "checkout", "team": "core", "owner": "pagos"}}}
    assert resumen._contexto(f, idx)["dueno"] == "pagos"
    idx = {(CUENTA, "i-1"): {"tags": {"app": "checkout"}}}
    assert resumen._contexto(f, idx)["dueno"] is None  # el nombre de la app no es un dueño
    # El mismo resultado con otra semilla de hash (antes dependía del orden de un set).
    codigo = ("import sys; sys.path.insert(0, %r); import resumen; "
              "print(resumen._contexto({'account_id': 'a', 'resource_id': 'r'}, "
              "{('a', 'r'): {'tags': {'team': 'core', 'squad': 'x', 'owner': 'pagos', 'Environment': 'prod', 'env': 'dev'}}}))"
              % str(AQUI.parent / "scripts"))
    salidas = {subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True,
                              env={"PYTHONHASHSEED": str(n)}).stdout for n in range(6)}
    assert len(salidas) == 1 and "pagos" in salidas.pop()


def test_diferidos_no_son_gasto(tmp_path):
    from hallazgos import hallazgo, completar_v2
    doc = completar_v2(hallazgo("desperdicio", "media", CUENTA, REGION, "i-9", "ociosa", "d", 70.0, "aws-doctor",
                                rule_id="aws-doctor.idle_ec2_instances", ahorro=False))
    ipv4 = completar_v2(hallazgo("costo", "baja", CUENTA, REGION, None, "IPv4", "d", 3.65, "regla",
                                 rule_id="regla.ipv4_publica", ahorro=False))
    (tmp_path / "findings.json").write_text(json.dumps({"findings": [doc, ipv4]}))
    r = resumen.resumir(tmp_path)
    assert [x["rule_id"] for x in r["diferidos"]] == ["aws-doctor.idle_ec2_instances"]
    assert [x["rule_id"] for x in r["gasto_informativo"]] == ["regla.ipv4_publica"] and r["gasto_informativo_usd"] == 3.65
    assert r["lagunas"]["sin_inventario"] and r["lagunas"]["sin_discovery"]


def test_archivos_raros_no_rompen(tmp_path):
    (tmp_path / "findings.json").write_text("[]")
    assert resumen.resumir(tmp_path)["maximo_teorico_usd"] == 0
    (tmp_path / "findings.json").write_text('{"findings": [{"is_saving": true, "monthly_usd": NaN}, '
                                            '{"is_saving": true, "monthly_usd": "5"}]}')
    assert resumen.resumir(tmp_path)["maximo_teorico_usd"] == 0  # NaN: archivo rechazado; texto: no es monto
    (tmp_path / "findings.json").write_text('{"findings": [{"is_saving": true, "monthly_usd": "5"}, '
                                            '{"is_saving": true, "monthly_usd": 2.5, "resource_id": null}]}')
    assert resumen.resumir(tmp_path)["maximo_teorico_usd"] == 2.5


def test_coincide_por_arn_y_ids_dinamicos_de_coh():
    f = {"account_id": CUENTA, "resource_id": "arn:aws:elasticloadbalancing:eu-west-1:111111111111:loadbalancer/clb"}
    idx = resumen._indice_recursos({"resources": [{"account_id": CUENTA, "id": "clb", "arn": f["resource_id"],
                                                   "tags": {"owner": "web"}}]})
    assert resumen._contexto(f, idx)["dueno"] == "web"
    for rid, esperado in (("cost-optimization-hub.rightsize.ec2instance.sin_ahorro", "reinicio"),
                          ("cost-optimization-hub.scalein.ec2autoscalinggroup", "reinicio"),
                          ("cost-optimization-hub.delete.ebsvolume", "irreversible"),
                          ("cost-optimization-hub.nuevaaccion.x", "irreversible")):
        assert resumen.reversibilidad({"rule_id": rid}) == esperado, rid


def test_verificacion_json_hace_anonimizar_al_creador(tmp_path):
    """El creador de CloudTrail que la skill escribe en el informe se anonimiza si va también en
    verificacion.json (clave created_by), como pide SKILL.md."""
    sys.path.insert(0, str(RAIZ / "anonimizar"))
    import subprocess
    (tmp_path / "corrida").mkdir()
    c = tmp_path / "corrida"
    (c / "discovery.json").write_text(json.dumps({"run": {"org_alias": "acme"}, "accounts": {}}))
    (c / "verificacion.json").write_text(json.dumps({"items": [{"id": "F001", "created_by": "jperez-admin"}]}))
    (c / "informe-finops.md").write_text("- Creado por:     jperez-admin (CloudTrail)\n")
    r = subprocess.run([sys.executable, "-I", str(RAIZ / "anonimizar" / "anonimizar.py"), "--termino", "acme",
                        str(c)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "jperez-admin" not in (tmp_path / "corrida-anon" / "informe-finops.md").read_text()


def test_desperdicio_sin_monto_hace_solapado_no_diferido(tmp_path):
    from hallazgos import hallazgo
    ociosa = hallazgo("desperdicio", "media", CUENTA, REGION, "i-1", "ociosa", "d", None, "aws-doctor",
                      rule_id="aws-doctor.idle_ec2_instances")
    familia = hallazgo("costo", "baja", CUENTA, REGION, "i-1", "familia", "d", 20.0, "regla",
                       rule_id="regla.ec2_generacion_antigua")
    findings = validar.deduplicar([ociosa, familia])
    assert {f["rule_id"]: f["is_saving"] for f in findings}["regla.ec2_generacion_antigua"] is False
    (tmp_path / "findings.json").write_text(json.dumps({"findings": findings}))
    r = resumen.resumir(tmp_path)
    assert [x["rule_id"] for x in r["solapados"]] == ["regla.ec2_generacion_antigua"] and r["diferidos"] == []


def test_findings_ilegible_corta_en_vez_de_dar_cero(tmp_path, monkeypatch):
    import pytest
    (tmp_path / "findings.json").write_text('{"findings": [{"monthly_usd": NaN}]}')
    monkeypatch.setattr(sys, "argv", ["resumen.py", str(tmp_path)])
    with pytest.raises(SystemExit, match="no se puede leer"):
        resumen.main()
    assert resumen.resumir(tmp_path)["lagunas"]["sin_findings"] is True


def test_valores_anidados_raros_no_rompen(tmp_path):
    (tmp_path / "findings.json").write_text(json.dumps({"run": [], "findings": [{"is_saving": True, "monthly_usd": 1}]}))
    (tmp_path / "discovery.json").write_text(json.dumps({"finops": "x"}))
    (tmp_path / "inventory.json").write_text(json.dumps({"resources": ["x", 3]}))
    assert resumen.resumir(tmp_path)["maximo_teorico_usd"] == 1


def test_prefijos_de_comandos_funcionan_en_zsh_y_bash(tmp_path):
    """El prefijo de lectura que indica acciones.md (escrito entero, con variables escalares) se expande igual en
    zsh (la shell de macOS y de la herramienta Bash) y en bash. Se prueba con un `aws` falso que imprime sus argumentos."""
    import shutil
    import subprocess
    falso = tmp_path / "bin" / "aws"
    falso.parent.mkdir()
    falso.write_text('#!/bin/sh\necho "CFG=$AWS_CONFIG_FILE"; for a in "$@"; do echo "[$a]"; done\n')
    falso.chmod(0o755)
    linea = ("CUENTA='111111111111' R='eu-west-1' ID='vol-0abc1234'; "
             'AWS_CONFIG_FILE="$HOME/.aws/dive-config" aws --profile "dive-$CUENTA" --region "$R" '
             "ec2 describe-volumes --volume-ids \"$ID\"")
    esperado = None
    for shell in ("zsh", "bash"):
        if not shutil.which(shell):
            continue
        r = subprocess.run([shell, "-c", linea], capture_output=True, text=True,
                           env={"PATH": f"{falso.parent}:/usr/bin:/bin", "HOME": "/home/x"})
        assert r.returncode == 0, (shell, r.stderr)
        assert "[--profile]\n[dive-111111111111]\n[--region]\n[eu-west-1]" in r.stdout, (shell, r.stdout)
        assert "CFG=/home/x/.aws/dive-config" in r.stdout
        esperado = esperado or r.stdout
        assert r.stdout == esperado
    # Y acciones.md ya no usa el patrón $LEER / $ESCRIBIR que zsh no separa.
    texto = (AQUI.parent / "referencias" / "acciones.md").read_text()
    assert "$LEER" not in texto and "$ESCRIBIR" not in texto


# ---------- verificacion.json ----------

def _corrida_con_run(tmp_path):
    findings = _corrida(tmp_path)
    doc = json.loads((tmp_path / "findings.json").read_text())
    doc["run"] = {"run_id": "2026-10-18T000000Z"}
    (tmp_path / "findings.json").write_text(json.dumps(doc))
    return {f["rule_id"]: f for f in findings}


def test_verificacion_valida_y_reglas_de_seguro_ya(tmp_path):
    import verificacion
    f = _corrida_con_run(tmp_path)
    vol, eip = f["regla.ebs_sin_adjuntar"], f["regla.eip_sin_asociar"]

    def escribir(items, run_id="2026-10-18T000000Z"):
        (tmp_path / "verificacion.json").write_text(json.dumps({"run_id": run_id, "items": items}))
        return verificacion.validar(tmp_path)

    validos, problemas = escribir([{"fingerprint": vol["fingerprint"], "estado": "seguro_ya", "motivo": "available"},
                                   {"fingerprint": eip["fingerprint"], "estado": "firma", "motivo": "irreversible",
                                    "pregunta_dueno": "¿está en una allowlist?"}])
    assert not problemas and len(validos) == 2
    # Una EIP (irreversible) no puede ser seguro_ya, aunque sume.
    _, problemas = escribir([{"fingerprint": eip["fingerprint"], "estado": "seguro_ya", "motivo": "x"}])
    assert problemas and "no puede ser seguro_ya" in problemas[0]
    # La IPv4 no suma: tampoco.
    _, problemas = escribir([{"fingerprint": f["regla.ipv4_publica"]["fingerprint"], "estado": "seguro_ya", "motivo": "x"}])
    assert "no puede ser seguro_ya" in problemas[0]
    for malo, texto in (({"fingerprint": "zz", "estado": "firma", "motivo": "x"}, "fingerprint inválido"),
                        ({"fingerprint": "0" * 16, "estado": "firma", "motivo": "x"}, "no está en findings.json"),
                        ({"fingerprint": vol["fingerprint"], "estado": "listo", "motivo": "x"}, "estado"),
                        ({"fingerprint": vol["fingerprint"], "estado": "firma"}, "falta motivo"),
                        ({"fingerprint": vol["fingerprint"], "estado": "firma", "motivo": "x" * 600}, "500")):
        _, problemas = escribir([malo])
        assert problemas and texto in problemas[0], (malo, problemas)
    _, problemas = escribir([], run_id="otra")
    assert "run_id" in problemas[0]


def test_verificacion_cli(tmp_path, monkeypatch, capsys):
    import verificacion
    f = _corrida_con_run(tmp_path)
    (tmp_path / "verificacion.json").write_text(json.dumps({"run_id": "2026-10-18T000000Z", "items": [
        {"fingerprint": f["regla.ebs_sin_adjuntar"]["fingerprint"], "estado": "seguro_ya", "motivo": "available"}]}))
    monkeypatch.setattr(sys, "argv", ["verificacion.py", str(tmp_path)])
    assert verificacion.main() == 0
    assert "ahorro seguro ya $10.00/mes" in capsys.readouterr().out


def test_validador_rechaza_symlink(tmp_path):
    import verificacion
    _corrida_con_run(tmp_path)
    fuera = tmp_path.parent / f"{tmp_path.name}-fuera.json"
    fuera.write_text(json.dumps({"run_id": "2026-10-18T000000Z", "items": []}))
    (tmp_path / "verificacion.json").symlink_to(fuera)
    _, problemas = verificacion.validar(tmp_path)
    assert problemas and "symlink" in problemas[0]


def test_validador_con_json_patologico_no_revienta(tmp_path, monkeypatch):
    import verificacion
    _corrida_con_run(tmp_path)
    (tmp_path / "verificacion.json").write_text("[" * 200000)
    monkeypatch.setattr(sys, "argv", ["verificacion.py", str(tmp_path)])
    assert verificacion.main() == 2
