"""Findings de los datos FinOps de la fase 1 (discovery["finops"]): recomendaciones de Cost Optimization Hub,
compromisos (Savings Plans y reservas) y guardarraíles (Cost Anomaly Detection).

Dos fuentes: "cost-optimization-hub" (sus recomendaciones, con price_source cost_optimization_hub, que gana
el dedup) y "finops" (lo que se deduce de compromisos y anomalías, con montos de Cost Explorer).

Uso: from hallazgos_finops import hallazgos, ajustar_por_compromisos
"""

from datetime import datetime, timezone

from hallazgos import hallazgo, parsear_fecha, severidad_por_costo

FUENTE_COH = "cost-optimization-hub"
FUENTE = "finops"

ACCIONES = {"Delete": "Borrar", "Stop": "Detener", "Rightsize": "Redimensionar", "MigrateToGraviton": "Migrar a Graviton",
            "Upgrade": "Pasar a una generación nueva", "PurchaseSavingsPlans": "Comprar Savings Plans",
            "PurchaseReservedInstances": "Comprar reservas", "ScaleIn": "Reducir capacidad"}
DESPERDICIO = {"Delete", "Stop"}
# Tipos de cómputo que cubren los Savings Plans: su ahorro no se cobra mientras el compromiso los absorbe.
COMPUTO = {"Ec2Instance", "Ec2AutoScalingGroup", "LambdaFunction", "EcsService"}
UTILIZACION_OK = 99.5      # % de uso de un compromiso que no vale la pena señalar
DIAS_AVISO_VENCIMIENTO = 60
COBERTURA_MINIMA = 70.0    # % de cómputo cubierto por compromisos
ON_DEMAND_RELEVANTE = 100  # USD de cómputo on-demand en 30 días para que la cobertura baja importe


def _cuenta_management(discovery):
    return (discovery.get("run") or {}).get("management_account_id")


def _primer_vencimiento(sp):
    fechas = sorted(a["fin"] for a in (sp or {}).get("activos") or [] if a.get("fin"))
    return fechas[0] if fechas else None


def _compromiso_absorbe(fin):
    """¿El ahorro de cómputo no llega a la factura? Con Savings Plans que ya sobran (uso < 100%) y casi
    nada de cómputo on-demand, apagar o achicar solo agranda el compromiso sin usar hasta que vence."""
    sp = fin.get("savings_plans") or {}
    u, c = sp.get("utilizacion") or {}, sp.get("cobertura") or {}
    # Mismo umbral que el finding de compromiso sin usar; sin dato de cobertura no se afirma nada.
    return (u.get("porcentaje") is not None and u["porcentaje"] < UTILIZACION_OK
            and c.get("on_demand_usd") is not None and c["on_demand_usd"] < ON_DEMAND_RELEVANTE)


def _orden(item):
    """Orden estable para claves con None (una región puede faltar): sin TypeError al comparar."""
    return tuple("" if x is None else str(x) for x in item[0])


# ---------- Cost Optimization Hub ----------

def _rule_id_coh(r, disponibles):
    # Un volumen que COH manda borrar es "sin adjuntar" solo si el inventario lo ve available: Compute
    # Optimizer también recomienda borrar volúmenes adjuntos sin I/O, que es otro problema.
    if r["accion"] == "Delete" and r["tipo"] == "EbsVolume" and (r["cuenta"], r["region"], r["resource_id"]) in disponibles:
        return f"{FUENTE_COH}.ebs_sin_adjuntar"
    return f"{FUENTE_COH}.{r['accion']}.{r['tipo']}".lower()


def _coh(fin, inventory):
    coh = fin.get("hub_optimizacion") or {}
    recs = coh.get("recomendaciones") or []
    if not recs:
        return []
    disponibles = {(r["account_id"], r["region"], r["id"]) for r in inventory.get("resources", [])
                   if r.get("type") == "volume" and r.get("state") == "available"}
    vence = _primer_vencimiento(fin.get("savings_plans"))
    absorbe = _compromiso_absorbe(fin)
    despues = coh.get("modo_estimacion") == "AfterDiscounts"
    out, sin_ahorro, compras = [], {}, {}
    for r in recs:
        ahorro = r.get("ahorro_usd") or 0
        accion = ACCIONES.get(r.get("accion"), r.get("accion"))
        if str(r.get("accion") or "").startswith("Purchase"):
            # compra de compromisos: por cuenta × región × tipo (su resource_id, si viene, no es estable)
            compras.setdefault((r["cuenta"], r["region"], r["tipo"], r["accion"]), []).append(r)
            continue
        if not r.get("resource_id"):  # una acción sobre un recurso sin id: no hay a qué apuntarla
            continue
        if ahorro <= 0:
            sin_ahorro.setdefault((r["cuenta"], r["region"], r["accion"], r["tipo"]), []).append(r)
            continue
        extra = ", requiere reinicio" if r.get("reinicio") else ""
        extra += "" if r.get("reversible") else ", no reversible"
        detalle = (f"{r.get('actual') or r['resource_id']} → {r.get('recomendado') or accion}. Esfuerzo "
                   f"{(r.get('esfuerzo') or '?').lower()}{extra}; {r.get('dias_analizados') or '?'} días analizados "
                   f"({r.get('origen') or 'COH'}). Ahorro estimado por Cost Optimization Hub"
                   f"{' ya descontando compromisos' if despues else ''}.")
        f = hallazgo("desperdicio" if r["accion"] in DESPERDICIO else "costo", severidad_por_costo(ahorro, "baja"),
                     r["cuenta"], r["region"], r["resource_id"], f"{accion} ({r['tipo']})", detalle, ahorro, FUENTE_COH,
                     rule_id=_rule_id_coh(r, disponibles), price_source="cost_optimization_hub")
        if r["tipo"] in COMPUTO and absorbe and not despues:
            # Antes de descuentos y con el compromiso sobrando: el ahorro no llega a la factura todavía.
            f["confidence"] = "baja"
            f["is_saving"] = False
            f["detail"] += (" No suma al ahorro: con los Savings Plans actuales se materializa al vencer el "
                            f"compromiso{f' ({vence})' if vence else ''}.")
        out.append(f)
    for (cuenta, region, tipo, acc), grupo in sorted(compras.items(), key=_orden):
        ahorro = round(sum(r.get("ahorro_usd") or 0 for r in grupo), 2)
        detalle = "; ".join(r.get("recomendado") or "" for r in grupo)[:400]
        out.append(hallazgo("costo", severidad_por_costo(ahorro, "baja"), cuenta, region, None,
                            f"{ACCIONES.get(acc, acc)} ({tipo})",
                            f"{detalle}. Compromiso nuevo: el ahorro llega solo si el uso se mantiene todo el plazo.",
                            ahorro or None, FUENTE_COH, rule_id=f"{FUENTE_COH}.{acc}.{tipo}".lower(),
                            discriminador=tipo, price_source="cost_optimization_hub" if ahorro else None))
    # Un recurso cuyo ahorro pasa de $0 a >$0 (cambia la holgura de los Savings Plans) cambia de fingerprint:
    # deja el grupo "sin ahorro hoy" y aparece solo. Es intencional: el agregado evita decenas de filas en $0.
    for (cuenta, region, acc, tipo), grupo in sorted(sin_ahorro.items(), key=_orden):
        ejemplos = ", ".join(f"{r['resource_id']} ({r.get('actual')} → {r.get('recomendado')})" for r in grupo[:5])
        mas = f" (+{len(grupo) - 5} más)" if len(grupo) > 5 else ""
        motivo = (f"El cómputo está cubierto por Savings Plans: el ahorro aparece al vencer el compromiso ({vence}); "
                  "conviene hacerlo antes de renovarlo, para comprometer menos." if absorbe and vence and tipo in COMPUTO else
                  "Cost Optimization Hub no estima ahorro hoy (sin descuento aplicable o ya optimizado).")
        out.append(hallazgo("costo", "baja", cuenta, region, None, f"{ACCIONES.get(acc, acc)} ({tipo}): {len(grupo)} sin ahorro hoy",
                            f"{ejemplos}{mas}. {motivo}", None, FUENTE_COH,
                            rule_id=f"{FUENTE_COH}.{acc}.{tipo}.sin_ahorro".lower(), discriminador=f"{acc}:{tipo}"))
    return out


# ---------- compromisos y guardarraíles ----------

def _renovado(plan, en_cola):
    """¿Hay en cola un plan del mismo tipo y región que empieza cuando este vence (±7 días)? Uno en cola que no
    empalma es otra compra, no la renovación."""
    fin = parsear_fecha(plan.get("fin"))
    for q in en_cola:
        inicio = parsear_fecha(q.get("inicio"))
        if (q.get("tipo") == plan.get("tipo") and q.get("region") == plan.get("region") and fin and inicio
                and abs((inicio - fin).days) <= 7):
            return True
    return False


def _compromisos(fin, mgmt, hoy):
    out = []
    sp = fin.get("savings_plans")
    if sp:
        u = sp.get("utilizacion") or {}
        if u.get("porcentaje") is not None and u["porcentaje"] < UTILIZACION_OK and (u.get("no_usado_usd") or 0) > 0:
            usd = u["no_usado_usd"]
            out.append(hallazgo("costo", severidad_por_costo(usd, "media"), mgmt, None, None,
                                f"Savings Plans: compromiso sin usar ({u['porcentaje']:.1f}% de uso)",
                                f"En los últimos 30 días se pagaron {usd:,.2f} USD de compromiso que nada usó. No se "
                                "recupera borrando: se aprovecha moviendo cómputo on-demand al plan (o comprometiendo "
                                "menos al renovar). Es gasto, no ahorro.", usd, FUENTE,
                                rule_id=f"{FUENTE}.savings_plans_sin_usar", price_source="ce_actual", ahorro=False))
        for a in sp.get("activos") or []:
            fin_sp = parsear_fecha(a.get("fin"))
            dias = (fin_sp.date() - hoy).days if fin_sp else None
            if dias is not None and 0 <= dias <= DIAS_AVISO_VENCIMIENTO and not _renovado(a, sp.get("en_cola") or []):
                out.append(hallazgo("costo", "media", mgmt, None, a["id"], f"Savings Plan vence en {dias} días",
                                    f"{a.get('tipo')} de {a.get('compromiso_usd_hora')} USD/hora vence el {a['fin']}. Sin "
                                    "renovarlo, el cómputo que cubre pasa a on-demand. Revisar antes las recomendaciones "
                                    "de redimensionar y Graviton, para comprometer solo lo necesario.", None, FUENTE,
                                    rule_id=f"{FUENTE}.savings_plan_vence"))
        c = sp.get("cobertura") or {}
        if (c.get("porcentaje") is not None and c["porcentaje"] < COBERTURA_MINIMA
                and (c.get("on_demand_usd") or 0) >= ON_DEMAND_RELEVANTE):
            out.append(hallazgo("costo", "baja", mgmt, None, None,
                                f"Cómputo con poca cobertura de Savings Plans ({c['porcentaje']:.0f}%)",
                                f"{c['on_demand_usd']:,.2f} USD de cómputo on-demand en 30 días. Ver las recomendaciones de "
                                "compra de Cost Optimization Hub para el monto.", None, FUENTE,
                                rule_id=f"{FUENTE}.cobertura_baja"))
    ri = (fin.get("reservas") or {}).get("utilizacion") or {}
    if ri.get("porcentaje") is not None and ri["porcentaje"] < UTILIZACION_OK and (ri.get("costo_horas_sin_usar_usd") or 0) > 0:
        usd = ri["costo_horas_sin_usar_usd"]
        out.append(hallazgo("costo", severidad_por_costo(usd, "media"), mgmt, None, None,
                            f"Reservas sin usar ({ri['porcentaje']:.1f}% de uso)",
                            f"{ri.get('horas_sin_usar')} horas reservadas sin usar en 30 días ({usd:,.2f} USD). Se "
                            "aprovechan moviendo carga del mismo tipo o vendiéndolas (EC2 estándar, Marketplace).",
                            usd, FUENTE, rule_id=f"{FUENTE}.reservas_sin_usar", price_source="ce_actual", ahorro=False))
    return out


def _guardarrailes(fin, mgmt):
    out = []
    an = fin.get("anomalias")
    if an is not None:
        if not an.get("monitores"):
            out.append(hallazgo("riesgo", "media", mgmt, None, None, "Sin Cost Anomaly Detection",
                                "Ningún monitor de anomalías de costo: un pico (clave filtrada, bucle, recurso olvidado) "
                                "se ve recién en la factura. Es gratis: un monitor por servicio y una suscripción por email.",
                                None, FUENTE, rule_id=f"{FUENTE}.sin_anomalias"))
        elif not an.get("suscripciones"):
            out.append(hallazgo("riesgo", "baja", mgmt, None, None, "Anomalías de costo sin suscripción",
                                f"{len(an['monitores'])} monitores pero ninguna suscripción: nadie recibe las alertas.",
                                None, FUENTE, rule_id=f"{FUENTE}.anomalias_sin_suscripcion"))
    coh = fin.get("hub_optimizacion")
    if coh is not None and coh.get("estado") not in ("Active", None):
        out.append(hallazgo("inventario", "baja", mgmt, None, None, "Cost Optimization Hub no habilitado",
                            "Es gratis y reúne en un lugar las recomendaciones de Compute Optimizer, de compra de "
                            "compromisos y de borrar recursos sin uso, con el ahorro ya descontando compromisos.",
                            None, FUENTE, rule_id=f"{FUENTE}.coh_inactivo"))
    return out


def hallazgos(discovery, inventory, hoy=None):
    """Findings de discovery["finops"]. Los de Cost Optimization Hub van primero: así, en el dedup, su monto
    (ya neto de compromisos) es el que cuenta para el recurso."""
    fin = discovery.get("finops") or {}
    if not fin:
        return []
    hoy = hoy or (parsear_fecha(discovery.get("generated_at")) or datetime.now(timezone.utc)).date()
    mgmt = _cuenta_management(discovery)
    return _coh(fin, inventory) + _compromisos(fin, mgmt, hoy) + _guardarrailes(fin, mgmt)


def ajustar_por_compromisos(findings, discovery):
    """Findings de cómputo de aws-doctor (instancias ociosas, a precio on-demand) cuando los Savings Plans ya
    sobran: apagarlas solo agranda el compromiso sin usar, así que no suman al ahorro (is_saving False),
    confianza baja y la nota de cuándo se cobraría. Modifica y devuelve la lista."""
    fin = discovery.get("finops") or {}
    if not _compromiso_absorbe(fin):
        return findings
    vence = _primer_vencimiento(fin.get("savings_plans"))
    for f in findings:
        if f.get("rule_id") in ("aws-doctor.idle_ec2_instances",) and f.get("monthly_usd"):
            f["confidence"] = "baja"
            f["is_saving"] = False
            f["detail"] = (f"{f['detail']} No suma al ahorro: precio on-demand, y con los Savings Plans sobrando "
                           f"apagarla no baja la factura hasta que vence el compromiso{f' ({vence})' if vence else ''}.")
    return findings
