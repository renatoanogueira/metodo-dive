# Contrato entre fases

Cada fase lee el archivo de la anterior y escribe el suyo en `salida/<corrida>/`. Nada se parsea desde texto de consola.

**Corrida.** La fase 1 crea `salida/<run_id>/`, con `run_id` = `YYYY-MM-DDTHHMMSSZ` (UTC), y escribe su `run_id` en `salida/ULTIMA`. Las fases 2-4, sin ruta explícita, trabajan sobre la corrida de `ULTIMA` (si no existe, sobre `salida/<fecha de hoy>/`, el formato anterior). Dos corridas el mismo día no se pisan.

**Bloque `run`** (al inicio de `discovery.json`, `inventory.json` y `findings.json`): identifica la corrida y con qué se hizo. Las fases 2 y 3 lo copian de la anterior y agregan sus opciones en `flags`. Si `org_alias` del config no coincide con el de la corrida, la fase aborta.

```json
"run": {
  "run_id": "2026-10-18T101500Z", "dive_version": "a1b2c3d-dirty",
  "organization_id": "o-a1b2c3d4e5", "management_account_id": "123456789012", "org_alias": "mi-org",
  "currency": "USD",
  "config": {"billing_months": 6, "threshold_usd": 0.01, "include_accounts": [], "exclude_accounts": [], "access_mode": "sso"},
  "flags": {"inventariar": {"solo": null, "sin_cobertura": false},
            "validar": {"sin_doctor": false, "aws_doctor_version": "aws-doctor version 2.21.1",
                        "precios": "pricing_api"}}
}
```

```
setup/  →  1-descubrir → discovery.json
                          ↓
           2-inventariar → inventory.json
                          ↓
           3-validar     → findings.json + findings.md
                          ↓
           4-esquematizar → diagramas/*.drawio + reporte.html
           anonimizar    → cualquier archivo de salida → versión presentable
```

## Reglas para todo el código

1. **Solo lectura.** Solo se llaman operaciones `Describe*`, `List*`, `Get*` de metadatos. Nunca `GetObject`, `GetSecretValue`, `Decrypt` ni nada que lea contenido.
2. **Sin secretos en la salida.** Los colectores nunca guardan variables de entorno, `UserData`, `environment`/`secrets` de task definitions, parámetros de conexión, passwords ni certificados. Si un campo puede traer eso, no se copia.
3. **Nada hardcodeado.** Cuentas, regiones y nombres salen de la API o de `dive.config.yaml`.
4. **Los errores no detienen la corrida.** Un `AccessDenied` o throttling en una cuenta/región se registra en `errors` y se sigue.

## discovery.json (fase 1)

```json
{
  "generated_at": "2026-09-26T15:00:00Z",
  "period": {"start": "2026-03-01", "end": "2026-09-01", "months": 6},
  "threshold_usd": 0.01,
  "accounts": {
    "123456789012": {
      "name": "Produccion",
      "total_usd": 1234.56,
      "regions": {
        "us-east-1": {
          "total_usd": 1000.0,
          "services": {"Amazon Elastic Compute Cloud - Compute": 600.0, "Amazon Relational Database Service": 400.0},
          "monthly_usd": {"2026-03": 160.0, "2026-04": 170.0},
          "services_monthly_usd": {"Amazon Relational Database Service": {"2026-03": 70.0, "2026-04": 70.0}}
        },
        "global": {"total_usd": 5.0, "services": {"Amazon Route 53": 5.0}, "monthly_usd": {}}
      }
    }
  }
}
```

- La región `global` agrupa lo que Cost Explorer reporta como `NoRegion`/`global`, y también los servicios globales que Cost Explorer reparte por región de edge (`Amazon CloudFront`): si no, cada edge parece una región con recursos.
- `errors`: consultas de Cost Explorer que fallaron, `[{"region", "error"}]`. Vacío si todo fue bien.
- `services` usa el nombre exacto de la dimensión `SERVICE` de Cost Explorer.
- Solo cuenta registros de **uso** (`Usage`, `DiscountedUsage`, `SavingsPlanCoveredUsage`). Las cuotas de RIs/Savings Plans no son recursos y se excluyen.
- `services_monthly_usd` permite saber si un servicio se sigue cobrando en el último mes o dejó de cobrarse (recurso ya borrado).
- `usage_types_monthly_usd`: `{usage_type: {mes: usd}}` por región (dimensión `USAGE_TYPE` de Cost Explorer, sin servicio: Cost Explorer admite solo 2 GroupBy y un mismo tipo, ej. `EU-DataTransfer-Out-Bytes`, aparece en varios servicios). Dice qué se paga dentro de cada servicio: NAT, transferencia, almacenamiento, métricas. Los de servicios globales (CloudFront) van a `global`; esa consulta no filtra región, así que en `global` la suma de tipos puede superar a `services` (que solo suma regiones sobre el umbral).
- `charges_monthly_usd` (al nivel de `accounts`): `{cuenta: {record_type: {mes: usd}}}` con lo que NO es uso: `Tax`, `SavingsPlanRecurringFee` / `SavingsPlanUpfrontFee` (cuota del compromiso), `SavingsPlanNegation` (el descuento: anula el `SavingsPlanCoveredUsage` contado en `services`), `Recurring` / `Fee` (RIs), `Credit`, `Refund`... Factura del mes = `usage_total_monthly_usd` (uso sin umbral) + suma de estos cargos. Los `services` solos son el uso a precio de lista con el umbral aplicado (sin impuestos ni descuentos de compromisos). Fuera de la factura quedan los servicios de `SERVICIOS_NO_RECURSOS` cuyo registro sea de uso (ej. AWS Support, si se cobra como `Usage`).
- `usage_total_monthly_usd` (al nivel de `accounts`): `{cuenta: {mes: usd}}`, uso por cuenta sin umbral.
- En `errors`, `"query": "usage_type"` marca que falló solo la consulta de tipos de uso (los `services` siguen completos).
- `finops` (opcional; falta con `--sin-finops`): datos de la organización leídos desde la cuenta management. Cada sección es `null` si no se pudo leer, y el motivo queda en `finops.avisos` (`[{"seccion", "tipo": "sin_permiso" | "error", "error"}]`).
  - `savings_plans`: `activos` (`id`, `tipo`, `pago`, `region`, `compromiso_usd_hora`, `inicio`, `fin`), `utilizacion` de los últimos 30 días (`compromiso_usd`, `usado_usd`, `no_usado_usd`, `porcentaje`, `ahorro_neto_usd`) y `cobertura` (`porcentaje`, `on_demand_usd`, `cubierto_usd`). Sin Savings Plans activos, `utilizacion` y `cobertura` son `null`.
  - `reservas.utilizacion`: igual para las RIs (`null` si no hay horas compradas).
  - `anomalias`: `monitores` (`tipo`, `dimension`) y número de `suscripciones` de Cost Anomaly Detection.
  - `hub_optimizacion` (Cost Optimization Hub; la clave no lleva "cost" para que el anonimizador no trate días y conteos como costos): `estado` (`Active` si está habilitado), `modo_estimacion` (`AfterDiscounts` = los ahorros ya descuentan Savings Plans y RIs) y `recomendaciones` de las cuentas del config (`cuenta`, `region`, `resource_id`, `tipo`, `accion`, `ahorro_usd`, `costo_actual_usd`, `esfuerzo`, `reinicio`, `reversible`, `actual`, `recomendado`, `dias_analizados`, `origen`).
  - `moneda_pago`: moneda en la que se paga la factura y tasa USD → moneda (`codigo`, `tasa`, `periodo`, `fuente`: `invoice` | `config`, `por_entidad`: `[{"entidad", "tasa", "peso"}]`). Sale del último mes con facturas (Invoicing API); con varias entidades de facturación la tasa se pondera por lo facturado en USD por cada una. No guarda totales de la factura, solo tasas y pesos. Sin permiso o sin facturas, usa `moneda: {codigo, tasa}` del config si existe. Los montos de DIVE siguen en USD: la moneda es solo para mostrarlos.

## inventory.json (fase 2)

```json
{
  "generated_at": "2026-09-26T15:20:00Z",
  "accounts": {"123456789012": {"name": "Produccion"}},
  "scanned": [{"account_id": "123456789012", "region": "us-east-1", "collector": "ec2", "count": 4, "seconds": 1.2}],
  "resources": [
    {
      "account_id": "123456789012",
      "region": "us-east-1",
      "service": "ec2",
      "type": "instance",
      "id": "i-0abc...",
      "arn": "arn:aws:ec2:us-east-1:123456789012:instance/i-0abc...",
      "name": "api-prod-1",
      "state": "running",
      "tags": {"Environment": "prod"},
      "attrs": {"instance_type": "t3.large", "private_ip": "10.0.1.10", "public_ip": null},
      "relations": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-1"], "security_group_ids": ["sg-1"]}
    }
  ],
  "errors": [{"account_id": "123456789012", "region": "eu-west-1", "collector": "rds", "error": "AccessDenied: ..."}]
}
```

- `accounts.<id>.enabled_regions`: regiones habilitadas en la cuenta (`ec2:DescribeRegions`). Falta si la consulta falló; en ese caso no se filtra nada.
- `skipped`: `[{"account_id", "region", "reason": "region_not_enabled"}]`, cuenta × región con costo donde no se escaneó porque la región (opt-in) no está habilitada. No son errores.
- `service`: nombre corto del colector (`ec2`, `rds`, `vpc`...). `type`: subtipo (`instance`, `volume`, `security_group`...).
- `iam/account_summary` (uno por cuenta, región `global`): solo indicadores del root (`mfa_enabled`, `access_keys_present`, `password_present`) y el número de usuarios. Nunca credenciales.
- `name`: tag `Name` o el nombre propio del recurso; `null` si no tiene.
- `relations` solo lleva IDs, y solo las claves que apliquen: `vpc_id`, `subnet_ids`, `security_group_ids`, `target_ids`, `attached_to`.

## Interfaz de un colector (`2-inventariar/colectores/<nombre>.py`)

```python
NAME = "rds"                       # nombre corto, igual al del archivo
TRIGGERS = ["Amazon Relational Database Service"]   # servicios de Cost Explorer que lo activan
ALWAYS = False                     # True = corre en toda cuenta/región con costo (ej. vpc, que no factura)
GLOBAL = False                     # True = corre una sola vez por cuenta (en us-east-1), ej. s3, cloudfront, route53
TAGS = True                        # False = el colector no lee tags; la regla "sin tag" ignora sus recursos

def collect(session, region, account_id):
    """Devuelve una lista de dicts con la forma de `resources` de inventory.json."""
```

- Usar paginadores (`client.get_paginator`) siempre que existan.
- Crear clientes con `common.aws.client(session, "<servicio>", region)`: ya trae reintentos adaptativos para throttling.
- Construir cada recurso con `common.recurso(...)` para que todos tengan las mismas claves.

## findings.json (fase 3)

Campos v2 (aditivos) de cada finding:

- `rule_id`: de dónde salió: `regla.<nombre>`, `aws-doctor.<categoría>`, `cruce.<tipo>`, `cost-optimization-hub.<acción>.<tipo>` o `finops.<nombre>` (compromisos, anomalías).
- `issue_id`: el problema canónico. Fuentes distintas que detectan lo mismo comparten `issue_id` (ej. `regla.ebs_sin_adjuntar` y `aws-doctor.unused_ebs_volumes` → `ebs.unattached`).
- `fingerprint`: `sha256(issue_id|account_id|region|resource_id|discriminador)[:16]`. Identidad estable entre corridas: no cambia con el título, el costo, el orden ni con correr sin aws-doctor. Es la clave de deduplicación (un finding por fingerprint) y la que usa un historial. El `id` (F001…) es solo para leer y cambia entre corridas.
- `price_source`: `ce_actual` (Cost Explorer), `cost_optimization_hub` (ahorro estimado por Cost Optimization Hub; con `modo_estimacion` `AfterDiscounts`, ya neto de Savings Plans y RIs), `pricing_api` (precio de la región del recurso, de la AWS Price List API), `aws_doctor` (estimación de aws-doctor), `static_estimate` (precio de respaldo us-east-1 de una regla) o `null` si no hay `monthly_usd`.
- `confidence`: confianza del monto según su fuente: `alta` (`ce_actual`), `media` (`pricing_api`, `aws_doctor`), `baja` (`static_estimate`) o `null` sin monto.
- `is_saving`: `true` para `desperdicio` y `costo`, salvo que la regla marque el monto como gasto informativo (no es un ahorro). Los totales de `findings.md` y `reporte.html` suman solo estos.

**precios.json** (fase 3, junto a `findings.json`): los precios on-demand que usaron las reglas, `{"generated_at", "cobertura", "precios": {región: {clave: {usd, unidad, descripcion, usagetype, fuente, consulta}}}}`. Sale de la Price List API (endpoint us-east-1, filtro `regionCode`), con cache del día en `<output_dir>/.precios/`. Se reescribe en cada validación. Con `--sin-precios` o si la API falla, las reglas usan su precio de respaldo y lo marcan `static_estimate`; el error queda en `errors`. `cobertura` (también en `run.flags.validar.precios`): `pricing_api` (todos los precios pedidos), `parcial`, `static_estimate` (ninguno) o `n/a` (nada que cotizar).

```json
{
  "generated_at": "...",
  "findings": [
    {
      "id": "F001",
      "category": "costo | desperdicio | riesgo | inventario",
      "severity": "alta | media | baja",
      "account_id": "123456789012",
      "region": "us-east-1",
      "resource_id": "vol-0abc",
      "title": "Volumen EBS sin adjuntar",
      "detail": "...",
      "monthly_usd": 12.5,
      "source": "aws-doctor | cruce-billing | regla | cost-optimization-hub | finops"
    }
  ],
  "errors": [{"account_id": "123456789012", "region": "eu-west-1", "collector": "aws-doctor/Lambda", "error": "..."}]
}
```

- `errors`: corridas de aws-doctor que fallaron, checks de aws-doctor que fallaron (`aws-doctor/<check>`) y categorías de aws-doctor que `doctor.py` no conoce (señal de que hay que actualizar `CATEGORIAS`).
- Si un recurso tiene un finding de `desperdicio`, sus findings de `costo` se descartan: el ahorro es borrarlo, no optimizarlo. Los totales de `findings.md` y `reporte.html` suman solo `desperdicio` + `costo`.

## insights.json (skill metodo-dive, opcional)

Lo que la skill concluye al analizar la corrida, estructurado. Lo escribe la skill en la carpeta de la corrida y lo
valida `skill/scripts/insights.py`, que además calcula los totales y exporta `informe-finops.md` (un derivado: lo
que muestra el dashboard sale de `insights.json`, nunca del `.md`). El dashboard lo lee aunque la corrida ya
estuviera ingerida (lo relee si cambia) y lo valida otra vez con las mismas reglas (`common/insights.py`).

```json
{
  "run_id": "2026-10-18T194232Z",
  "generado": "2026-10-18T20:00:00Z",
  "insights": [
    {"id": "I01", "tipo": "hallazgo", "estado": "firma", "prioridad": 2,
     "alcance": {"nivel": "recurso", "cuenta": "123456789012", "region": "eu-west-1", "fingerprints": ["a1b2c3d4e5f60718"]},
     "titulo": "Load balancer sin tráfico",
     "texto": "Cuesta {{c1}} al mes y tuvo {{c2}} requests en 30 días.",
     "cifras": {"c1": {"fuente": "finding:a1b2c3d4e5f60718.monthly_usd", "formato": "usd"},
                "c2": {"fuente": "evidencia:E1.requests", "formato": "num"}},
     "evidencia": [{"id": "E1", "origen": "aws", "descripcion": "RequestCount de 30 días",
                    "salida_ref": "A001.json", "valores": {"requests": 0}}],
     "no_determinado": "Con tráfico cero no se distingue una operación pequeña de un recurso abandonado.",
     "pregunta_dueno": "¿El entorno está apagado a propósito?",
     "accion": {"comando": "aws --profile '<perfil-con-escritura>' elbv2 delete-load-balancer --load-balancer-arn '<arn>'",
                "reversibilidad": "irreversible"}}
  ]
}
```

- `tipo`: `hallazgo`, `causa_costo`, `recomendacion`, `pregunta` o `laguna` (lo que no se pudo ver).
- `estado`: `seguro_ya` (reversible y verificado), `firma` (necesita la decisión del dueño), `contradicho`,
  `no_verificado` o `informativo` (por defecto). `prioridad`: 1 a 99, 1 = lo primero.
- `alcance`: `nivel` (`organizacion`, `cuenta`, `region`, `recurso`), `cuenta`, `region` y `fingerprints` (los del
  finding). Un insight con fingerprints sigue al problema entre corridas; uno de otra corrida se ve atenuado y no
  cuenta en los totales.
- **El dinero y los porcentajes no se escriben en `titulo` ni en `texto`.** Van como `{{c1}}` y `cifras.c1` dice de
  dónde sale (`fuente`) y cómo se muestra (`formato`: `usd`, `pct`, `num`). Se rechaza el dinero escrito con símbolo
  (`$12`, `12€`), con código (`12 USD`), en palabras (`11 dólares`, `once mil dólares`) y los porcentajes (`94%`,
  `94 por ciento`). Fechas, cuentas, contadores y tipos de instancia sí pueden ir en el texto. Fuentes:
  - `finding:<fingerprint>.monthly_usd`;
  - `suma:<fp>,<fp>.monthly_usd` (sin fingerprints repetidos: contarían dos veces);
  - `discovery:<ruta.punteada>` (un número de `discovery.json`);
  - `evidencia:<E1>.<clave>`;
  - `total:maximo_teorico`, `total:seguro_ya` o `total:gasto_informativo`.

  El dashboard formatea el valor en el navegador (el dinero sigue el selector de moneda).
- `evidencia`: `origen` `dive` (datos que ya trae la corrida) o `aws` (una consulta que la skill hizo). Una de
  `aws` apunta con `salida_ref` a la salida cruda que guardó el runner en `aws/<id>.json` (con su hash), y sus
  `valores` tienen que ser un valor (una hoja numérica) de esa salida, no un pedazo de otro número ni de una fecha. `salida_ref` es
  `A###.json`. Con `salida_ref` y `valores` solo vale `origen: aws`. `evidencia[].comando` es descriptivo: sin `; | & $ ` < > \` ni saltos de línea.
- `no_determinado`: lo que la skill no pudo saber y el operador debe comprobar. `pregunta_dueno`: la pregunta
  concreta. `accion.comando`: lo ejecuta una persona, con un perfil con escritura; solo `aws --profile '<perfil-con-escritura>' [--region '…'] <servicio> <operación> --opción 'valor'…`,
  con los valores entre comillas simples, una línea por comando (hasta 6): sin pipes, `;`, `&`, `$()` ni redirecciones. Los textos no llevan imágenes ni enlaces de Markdown.
- `seguro_ya` solo vale para un hallazgo o recomendación cuyos fingerprints son ahorros que suman, con monto y de un
  tipo reversible (`common/verificacion.py`). Los totales (`seguro_ya`, `maximo_teorico`, `por_reversibilidad`,
  `gasto_informativo`) los calcula el script y los recalcula el dashboard; no hay un campo de totales. Un fingerprint no puede estar
  en un insight `seguro_ya` y en otro `firma`, `contradicho` o `no_verificado`. Cada cifra declara un formato coherente con su fuente:
  `finding`, `suma` y `total` son `usd`; `evidencia` nunca es `usd`.
- Un archivo con cualquier problema no se publica, ni a medias: el script sale con error y el dashboard no muestra
  ninguno de sus insights (el Resumen lista los problemas). El informe `informe-finops.md` lleva al final la marca `<!-- insights-sha256: … -->`:
  el dashboard avisa si ya no corresponde al `insights.json` actual. No se leen archivos que sean symlinks o
  hardlinks, ni `insights.json` de más de 2 MB.

### aws/<id>.json (salidas del runner)

`skill/scripts/aws_leer.py` es la única forma en que la skill consulta AWS. Solo ejecuta lecturas de metadatos,
de una lista blanca de pares (servicio, operación) (nunca `describe_*` o `list_*` en general: Lambda, `describe_instance_attribute`,
las versiones de launch templates, las task definitions de ECS o las suscripciones de SNS no están), siempre con el perfil de
solo lectura de DIVE de esa cuenta (en `assume_role`, solo con el rol `DIVE_ReadOnly`), en una cuenta y región de la corrida, con
parámetros JSON validados y sin shell. Antes de guardar, redacta `UserData`, `Environment`, `Variables`, `Input` y similares. Guarda `{id, cuenta, region, servicio, operacion,
parametros, salida, truncada, sha256, ejecutado, username}`; `salida` es la respuesta cruda (hasta 200 KB) y
`username` lista las personas que aparecen en ella (el anonimizador las reemplaza).
