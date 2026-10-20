"""AMIs sin uso de aws-doctor, refinadas con su último lanzamiento. No llaman a AWS.

Uso: .venv/bin/python -m pytest 3-validar/tests/test_amis.py -q
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import amis  # noqa: E402
from hallazgos import completar_v2, hallazgo  # noqa: E402

AHORA = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
CFG = {"access_mode": "sso", "max_workers": 4}


def _ami(ami_id, nombre):
    return hallazgo("desperdicio", "baja", "111111111111", "eu-west-1", ami_id, "AMI sin uso (verificar ASG/launch templates)",
                    f"name={nombre}, creation_date=2023-04-05T14:41:10Z, days_since_create=1293, snapshot_size_gb=152",
                    7.6, "aws-doctor", rule_id=amis.REGLA)


def _lector(fechas):
    def leer(cuenta, region, ami):
        v = fechas[ami]
        if isinstance(v, Exception):
            raise v
        return v
    return leer


def _refinar(findings, fechas):
    errores = []
    amis.refinar(CFG, findings, errores, lector=_lector(fechas), ahora=AHORA)
    return {f["resource_id"]: f for f in findings}, errores


def test_clasifica_por_ultimo_lanzamiento_y_backup():
    fs = [_ami("ami-1", "AwsBackup_i-0abc_uuid"), _ami("ami-2", "gitlab-runner-v4"), _ami("ami-3", "gitlab-runner-v3"),
          _ami("ami-4", "vieja"), _ami("ami-5", "limite")]
    por_id, errores = _refinar(fs, {
        "ami-2": datetime(2026, 10, 19, 10, 52, tzinfo=timezone.utc),    # hoy
        "ami-3": datetime(2026, 8, 1, tzinfo=timezone.utc),              # 79 días
        "ami-4": None,                                                   # nunca
        "ami-5": datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc),      # justo 30 días: ya no es reciente
        "ami-1": AssertionError("las de AWS Backup no se consultan"),
    })
    assert not errores
    assert por_id["ami-1"]["is_saving"] is False and "gestionada=aws_backup" in por_id["ami-1"]["detail"]
    assert por_id["ami-2"]["is_saving"] is False and "last_launched=2026-10-19" in por_id["ami-2"]["detail"]
    assert "hace 0 días" in por_id["ami-2"]["title"]
    assert "is_saving" not in por_id["ami-3"] and "hace 79 días" in por_id["ami-3"]["title"]
    assert "sin lanzamientos registrados" in por_id["ami-4"]["title"] and "last_launched=sin registros" in por_id["ami-4"]["detail"]
    assert "is_saving" not in por_id["ami-5"] and "hace 30 días" in por_id["ami-5"]["title"]


def test_los_que_no_suman_siguen_en_el_contrato_pero_fuera_del_ahorro():
    fs = [_ami("ami-2", "x"), _ami("ami-4", "y")]
    por_id, _ = _refinar(fs, {"ami-2": datetime(2026, 10, 19, tzinfo=timezone.utc), "ami-4": None})
    reciente, nunca = completar_v2(por_id["ami-2"]), completar_v2(por_id["ami-4"])
    assert reciente["is_saving"] is False and reciente["monthly_usd"] == 7.6      # se muestra, no se descarta
    assert nunca["is_saving"] is True
    assert reciente["fingerprint"] != nunca["fingerprint"]


def test_el_fingerprint_no_cambia_al_refinar():
    antes = completar_v2(_ami("ami-2", "x"))["fingerprint"]
    por_id, _ = _refinar([_ami("ami-2", "x")], {"ami-2": datetime(2026, 10, 19, tzinfo=timezone.utc)})
    assert completar_v2(por_id["ami-2"])["fingerprint"] == antes


def test_sin_lectura_se_deja_como_aws_doctor_con_confianza_baja_y_un_aviso():
    por_id, errores = _refinar([_ami("ami-1", "a"), _ami("ami-2", "b")],
                               {"ami-1": PermissionError("AccessDenied"), "ami-2": PermissionError("AccessDenied")})
    for f in por_id.values():
        assert "is_saving" not in f and f["confidence"] == "baja" and "last_launched=no verificado" in f["detail"]
    assert len(errores) == 1 and errores[0]["collector"] == "amis"      # el mismo error no se repite por AMI


def test_sin_amis_no_se_llama_a_aws():
    otros = [hallazgo("desperdicio", "baja", "1", "eu-west-1", "vol-1", "t", "d", 1.0, "aws-doctor", rule_id="aws-doctor.x")]
    errores = []
    assert amis.refinar(CFG, otros, errores, lector=None) is otros and not errores


def test_nombre_se_lee_del_detalle():
    assert amis._nombre("name=AwsBackup_i-0abc_uuid, creation_date=2023") == "AwsBackup_i-0abc_uuid"
    assert amis._nombre("creation_date=2023") == ""


# ---------- snapshots de AWS Backup ----------

def _snap(rule, snap_id, cuenta="111111111111"):
    return hallazgo("desperdicio", "baja", cuenta, "eu-west-1", snap_id, "Snapshot", "d", 6.4, "aws-doctor", rule_id=rule)


def _inv():
    base = {"account_id": "111111111111", "region": "eu-west-1", "type": "snapshot"}
    return {"resources": [
        {**base, "id": "snap-bk", "tags": {}, "attrs": {"description": "This snapshot is created by the AWS Backup service."}},
        {**base, "id": "snap-tag", "tags": {"aws:backup:source-resource": "x"}, "attrs": {"description": "otra"}},
        {**base, "id": "snap-man", "tags": {}, "attrs": {"description": "manual"}},
    ]}


def test_snapshots_de_aws_backup_no_suman_pero_se_muestran():
    fs = [_snap("aws-doctor.orphaned_snapshots", "snap-bk"), _snap("regla.snapshots_viejos", "snap-tag"),
          _snap("aws-doctor.stale_snapshots", "snap-man"), _snap("aws-doctor.orphaned_snapshots", "snap-bk", "999999999999")]
    amis.marcar_aws_backup(fs, _inv())
    assert [f.get("is_saving") for f in fs] == [False, False, None, None]     # otra cuenta con el mismo id: no coincide
    assert "gestionado=aws_backup" in fs[0]["detail"] and "AWS Backup" in fs[1]["title"]
    assert completar_v2(fs[0])["monthly_usd"] == 6.4 and completar_v2(fs[0])["is_saving"] is False


def test_snapshot_rds_de_aws_backup_por_id():
    fs = [_snap("aws-doctor.old_rds_snapshots", "awsbackup:job-123"), _snap("aws-doctor.old_rds_snapshots", "migracao")]
    amis.marcar_aws_backup(fs, {"resources": []})
    assert [f.get("is_saving") for f in fs] == [False, None]


def test_una_fecha_ilegible_es_sin_lectura_y_un_detalle_vacio_no_rompe():
    f = _ami("ami-1", "x")
    f["detail"] = None
    f["detail"] = None
    por_id, errores = _refinar([f], {"ami-1": ValueError("LastLaunchedTime con un formato que no se entiende")})
    assert por_id["ami-1"]["confidence"] == "baja" and por_id["ami-1"]["detail"] == ", last_launched=no verificado"
    assert errores
