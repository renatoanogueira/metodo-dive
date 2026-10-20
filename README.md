<div align="right">
  <a href="#es">🇪🇸 Español</a> &nbsp;|&nbsp;
  <a href="#en">🇺🇸 English</a> &nbsp;|&nbsp;
  <a href="#pt">🇧🇷 Português</a>
</div>

---

<a id="es"></a>
## Español

### Método DIVE

Inventario de una AWS Organization con muchas cuentas, en una tarde, con IA como copiloto y sin darle a nadie (ni a la IA) permisos de escritura.

| Fase | Pregunta | Fuente de verdad | Entregable |
|---|---|---|---|
| **0 · Preparar** | ¿Cómo entro a todas las cuentas sin riesgo? | IAM Identity Center | permission set `DIVE_ReadOnly` + perfiles `dive-*` |
| **D · Descubrir** | ¿Dónde hay algo? ¿Cuánto pago? | Billing (Cost Explorer, 6 meses) + Savings Plans, Cost Optimization Hub, anomalías y moneda de la factura | `discovery.json` |
| **I · Inventariar** | ¿Qué hay exactamente? | APIs de AWS, solo donde el billing dice que hay costo | `inventory.json` |
| **V · Validar** | ¿Está sano? ¿Cuadra con lo que pago? | AWS Doctor + Cost Optimization Hub + cruce billing ↔ inventario + reglas FinOps | `findings.json` / `findings.md` |
| **E · Esquematizar** | ¿Cómo se ve? | `inventory.json` | diagramas `.drawio` + `reporte.html` |
| **5 · Dashboard** | ¿Qué cambió entre corridas? ¿Qué ya revisé? | todas las corridas | dashboard local en 127.0.0.1 |
| Anonimizar | ¿Lo puedo mostrar? | todas las salidas | carpeta `-anon` verificada |

#### Principios

1. **La factura es el primer mapa.** En vez de escanear 30+ servicios × todas las regiones × todas las cuentas, solo se escanea donde hubo uso facturado. Menos llamadas, menos throttling, ninguna llamada de más a producción.
2. **Contratos JSON entre fases.** Cada fase lee un archivo y escribe otro (ver [CONTRATO.md](CONTRATO.md)). Nada se parsea desde texto de consola, y cualquier fase se puede repetir sola.
3. **La IA escribe código y analiza, nunca inventa datos.** Todo lo que está en los JSON viene de AWS. Los prompts de cada fase (`*/prompt.md`) están versionados y exigen citar IDs.
4. **El agente usa tu identidad, no tus permisos.** Aunque seas admin, el escaneo corre con un rol de solo lectura de metadatos: puede listar un secreto, nunca leer su valor.
5. **Sin secretos en la salida.** Los colectores descartan variables de entorno, UserData, task definitions, PSKs, policies y endpoints de suscripción.
6. **Checkpoint humano entre fases.** Se revisa cada entregable antes de seguir.

#### Requisitos

- Python 3.11+ (el anonimizador usa cuantificadores posesivos de `re`) y [uv](https://github.com/astral-sh/uv) (o pip)
- AWS CLI v2 con acceso a la cuenta management
- [aws-doctor](https://github.com/elC0mpa/aws-doctor) (opcional, fase V)

#### Instalación

```bash
uv venv .venv && uv pip install -p .venv --require-hashes -r requirements.lock   # versiones exactas con hash
# (sin uv: python3.11 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock)
cp dive.config.example.yaml dive.config.yaml   # y ajusta management_profile, org_alias y replace_terms
```

#### Configuración

| Clave de `dive.config.yaml` | Para qué |
|---|---|
| `management_profile` | Perfil de la cuenta management (billing y lista de cuentas) |
| `access_mode` / `scan_role_name` | `sso` (perfiles `dive-*`) o `assume_role`; nombre del rol de lectura |
| `billing_months` / `threshold_usd` | Ventana de Cost Explorer y umbral por servicio |
| `include_accounts` / `exclude_accounts` | IDs **entre comillas** (`["012345678901"]`): sin comillas YAML los lee como número (y con un 0 adelante, en octal) |
| `output_dir` | Dónde quedan las corridas. Para un cliente, fuera del repo |
| `org_alias` | Nombre corto de la organización: identifica sus corridas y una fase aborta si la corrida es de otra |
| `anonymize.replace_terms` | Marcas del cliente a reemplazar. **Obligatorio** para anonimizar (o `--sin-terminos`) |
| `anonymize.costs` | `exact`, `rounded` o `percent` en la salida anonimizada |

#### Fase 0 · Preparar el acceso

El rol de escaneo es `ViewOnlyAccess` + [una policy mínima](setup/readonly-policy.json) con exactamente lo que piden los colectores y aws-doctor. `ViewOnlyAccess` sola se queda corta (no trae Cost Explorer ni Transit Gateway), y `ReadOnlyAccess` es demasiado (incluye `s3:GetObject`: leer datos). Los datos FinOps de la fase 1 usan `savingsplans:DescribeSavingsPlans`, `cost-optimization-hub:List*/GetPreferences`, `budgets:ViewBudget` e `invoicing:ListInvoiceSummaries` (la factura: de ahí sale la moneda de pago; DIVE guarda solo la tasa de cambio, no los totales). Si creaste el permission set antes, actualízalo con la policy nueva; sin esos permisos la fase 1 sigue y lo avisa.

La policy también **niega explícitamente** lo que sí lee contenido aunque parezca metadato: valores de API keys de API Gateway, variables de entorno de task definitions de ECS y de Lambda (`GetFunctionConfiguration`), UserData (launch templates, launch configurations, spot), secretos, parámetros SSM, objetos S3, eventos de logs y outputs de CloudFormation. Esos `Deny` siguen valiendo aunque AWS amplíe `ViewOnlyAccess` en el futuro. Riesgo residual que no se puede cerrar sin romper el inventario: `lambda:ListFunctions`/`ListVersionsByFunction` devuelven las variables de entorno de Lambda, `glue:GetJobs` los `DefaultArguments` de los jobs y los stages de API Gateway sus stage variables. Los colectores descartan esos campos, pero quien tenga el rol puede verlos: no guardes secretos ahí.

Opcional: el chequeo de memoria de Lambda de aws-doctor usa Logs Insights (`logs:StartQuery`, `logs:GetQueryResults`), que lee el **contenido** de los logs de las funciones. No viene en la policy; agrégalo solo si aceptas ese acceso.

Elige según lo que tenga tu organización:

| Tu org tiene… | Qué hacer |
|---|---|
| **IAM Identity Center (SSO)** | `bash setup/crear_permission_set.sh --profile <management> --user <tu-usuario>` (dry-run), luego con `--apply`. Después `python setup/generar_perfiles.py`. Es re-ejecutable: si cambias la policy, vuelve a correrlo. Reversa: `setup/eliminar_permission_set.sh`. |
| **Organizations sin SSO** | Despliega un rol `DIVE_ReadOnly` con un StackSet en todas las cuentas (misma policy) y usa `access_mode: assume_role`. |
| **Solo `OrganizationAccountAccessRole` / `AWSControlTowerExecution`** | Esos roles son **admin**: úsalos para *desplegar* el rol de lectura, nunca para escanear. |

Comprobación de que el rol no puede escribir (no modifica nada, `--dry-run` solo evalúa el permiso):

```bash
AWS_CONFIG_FILE=~/.aws/dive-config aws ec2 create-tags --dry-run --resources <vpc-id> \
  --tags Key=dive,Value=test --profile dive-<account_id>
# → UnauthorizedOperation
```

#### Uso: todo en un comando

```bash
bash dive.sh --config ~/clientes/acme/dive.config.yaml            # 5 fases + dashboard
bash dive.sh --config ~/clientes/acme/dive.config.yaml --sin-doctor --sin-dashboard
bash dive.sh                                   # sin --config: busca dive.config.yaml en tu carpeta personal y pregunta
```

Opciones: `--sin-doctor`, `--sin-precios` (precios estimados de us-east-1 en vez de la Price List API), `--sin-finops` (sin Savings Plans, Cost Optimization Hub ni moneda de la factura), `--sin-dashboard`, `--sin-navegador` (abre el dashboard sin abrir el navegador). Comprueba antes el entorno (venv, config, perfiles `dive-*`, sesión de AWS) y, si falta algo, dice el comando exacto para arreglarlo (si la sesión de AWS expiró, ofrece hacer el `aws sso login` ahí mismo). Corre las fases sobre una corrida nueva, se para en la primera que falle (indicando cómo retomar solo esa fase) y al final abre el dashboard.

#### Uso: correr las fases

```bash
export DIVE_CONFIG=dive.config.yaml            # o la ruta a la config de cada organización
.venv/bin/python 1-descubrir/descubrir.py       # ~15 s
.venv/bin/python 2-inventariar/inventariar.py   # ~1-2 min para ~5-20 cuentas
.venv/bin/python 3-validar/validar.py           # ~2 min con aws-doctor (--sin-doctor si no lo tienes)
.venv/bin/python 4-esquematizar/esquematizar.py
.venv/bin/python 4-esquematizar/reporte.py
.venv/bin/python anonimizar/anonimizar.py salida/<corrida>   # sale ≠ 0 si detecta cualquier fuga
```

Las salidas quedan en `salida/<corrida>/` (ignorada por git), una carpeta por corrida (`2026-10-18T101500Z`, UTC). La fase 1 la crea y la anota en `salida/ULTIMA`; las demás fases usan esa, así que dos corridas el mismo día no se pisan. Cada JSON lleva un bloque `run` (organización, moneda, versión, opciones de cada fase) y cada hallazgo un `fingerprint` estable entre corridas: ver [CONTRATO.md](CONTRATO.md).

Además del uso por servicio, el descubrimiento trae los **tipos de uso** (`USAGE_TYPE`: NAT, transferencia, almacenamiento, métricas) y los **cargos que no son uso** (impuestos, cuotas de Savings Plans y RIs, su descuento, créditos): con ellos se arma la factura real del mes.

Para ver el resultado sin correr nada, abre `ejemplos/`: una organización sintética de 4 cuentas con su `reporte.html` (funciona offline) y los diagramas `.drawio`.

#### Uso: dashboard (fase 5)

```bash
.venv/bin/python 5-dashboard/dashboard.py                 # corridas del output_dir de DIVE_CONFIG
.venv/bin/python 5-dashboard/dashboard.py --runs ~/clientes/a/runs --runs ~/clientes/b/runs   # varias organizaciones
.venv/bin/python 5-dashboard/dashboard.py --sin-navegador # solo imprime la URL
```

Abre el navegador solo, en `http://127.0.0.1:8765/#t=<token>` (si el puerto está ocupado, usa otro y lo imprime). Las corridas nuevas aparecen al recargar la página, sin reiniciar.

| Pestaña | Qué muestra (todo con filtro por cuenta) |
|---|---|
| Resumen | Cuentas, regiones y recursos; factura, uso e impuestos de la ventana y del último mes; factura por concepto y mes a mes; uso mensual por cuenta; servicios y tipos de uso más caros; evolución entre corridas. Con insights: la tarjeta "Ahorro seguro ya", lo primero que concluye la skill y lo que anota aparte (lagunas, avisos sin recurso) |
| Recursos | Inventario completo con filtros y detalle (tags, atributos, relaciones, hallazgos) |
| Hallazgos | Contador por severidad; historia entre corridas (nuevo, resuelto, no verificado); estado editable (abierto, aceptado, ignorado, resuelto) que se guarda por fingerprint. Cada hallazgo muestra una línea con lo que concluyó la skill y "ver detalle" (texto, qué no se pudo determinar, pregunta al dueño, evidencia y acción) |
| Corridas | Cada corrida con su tipo, errores y su `reporte.html` |
| Informe (.md) | `informe-finops.md` como texto, con un aviso si ya no corresponde al `insights.json` actual |

Los filtros de Recursos y Hallazgos y la pestaña abierta se recuerdan al recargar (solo en tu navegador); un aviso dice cuántos filtros hay activos.

#### Uso: skill para Claude Code

`skill/` (`metodo-dive`) es la capa de análisis: DIVE junta los datos y la skill los lee con criterio. El flujo:

1. `dive.sh` (solo lectura) hace la corrida, o se usa la última.
2. `skill/scripts/resumen.py` resume la corrida: candidatos, grupos por regla con sus fingerprints, diferidos, riesgos, lagunas.
3. La skill analiza. Si le falta un dato que cambia la decisión (¿ese disco tiene un backup programado? ¿ese balanceador tuvo tráfico en 30 días?), lo pide con `skill/scripts/aws_leer.py`, el único camino a AWS: lista blanca de lecturas de metadatos, siempre con el perfil de solo lectura `dive-<cuenta>`, parámetros JSON validados, sin shell, salida cruda guardada en `aws/` con su hash y con los campos sensibles redactados.
4. Escribe `insights.json` (ver [CONTRATO.md](CONTRATO.md)): datos estructurados, no texto libre. El dinero y los porcentajes van como `{{c1}}` con la fuente de la cifra (un finding, `discovery.json`, una evidencia); un recurso solo puede estar en insights con estados compatibles; las acciones son comandos `aws` acotados para que los ejecute una persona.
5. `skill/scripts/insights.py` valida el archivo con las mismas reglas que usa el dashboard, calcula los totales ("ahorro seguro ya" y "máximo teórico") y exporta `informe-finops.md`.

La skill nunca cambia nada en AWS. Para instalarla:

```bash
mkdir -p ~/.claude/skills && ln -s "$PWD/skill" ~/.claude/skills/metodo-dive
```

Después, en Claude Code: `/metodo-dive` o "audita los costos de la organización con DIVE". Al terminar, abre el dashboard para ver el resultado.

#### Qué no se cuenta como ahorro

DIVE separa lo que parece desperdicio de lo que no lo es:
- **AMIs sin uso** de aws-doctor: se consulta su último lanzamiento. Las de AWS Backup (`AwsBackup_…`) y las lanzadas hace menos de 30 días no suman; las que no se lanzan hace más de 30 días son candidatas.
- **Snapshots creados por AWS Backup** (por su descripción o sus tags): son puntos de recuperación que retira la retención del vault, no un `delete-snapshot`.
- **IPv4 públicas:** las propias (instancias, EIPs, NAT) van aparte de las que gestiona un servicio (ALB, RDS: no se pueden liberar, se reducen cambiando el servicio). Las dos son gasto informativo.
- **Savings Plans:** el ahorro de cómputo se difiere mientras sobre compromiso; y los montos se muestran en la moneda de pago de la factura, convertidos desde USD.

#### Cobertura: qué tan completo es el inventario

`3-validar/cobertura.py` compara el inventario contra la Tagging API, un oráculo independiente de los colectores, y genera `tareas_extra` para volver a correr la fase 2 sobre lo que el billing no mostró (típicamente lo que está en free tier).

> **Salvedad:** ningún inventario automático es 100% exhaustivo. El oráculo solo ve recursos que tienen o tuvieron tags. Algunos subrecursos se resumen a propósito (recovery points, revisiones de task definitions, ENIs). Y siempre se puede traspapelar algún recurso de un servicio poco común o recién lanzado. El método no promete "todo": **mide su cobertura, muestra qué falta y dice qué colector escribir después**.

#### Agregar un colector

Cada colector es un archivo en `2-inventariar/colectores/` con `NAME`, `TRIGGERS` (nombres exactos de servicio de Cost Explorer), `ALWAYS`, `GLOBAL`, `TAGS` (`False` si no lee tags) y `collect(session, region, account_id)`. Mira [ec2.py](2-inventariar/colectores/ec2.py) como referencia y la interfaz en [CONTRATO.md](CONTRATO.md). Si un servicio aparece en `findings.md` como "servicio sin colector", ese es el siguiente a escribir.

#### Seguridad

- El escaneo usa un rol de solo lectura de metadatos con `Deny` explícitos (ver Fase 0); nunca tus credenciales de admin.
- Las salidas son el reconocimiento completo de la organización: se crean con permisos solo para tu usuario (umask 077) y los errores no guardan tu sesión ni tu email.
- El dashboard solo escucha en 127.0.0.1, valida el `Host` y pide un token por header en cada llamada (sin cookies: una cookie de 127.0.0.1 viaja a cualquier otro puerto local). El `reporte.html` de cada corrida se abre en un iframe aislado.
- La skill solo consulta AWS con `aws_leer.py` (lista blanca de pares servicio-operación, nunca Lambda, user-data ni variables de entorno; campos sensibles redactados) y sus acciones son comandos `aws` validados para una persona: nunca se ejecutan solos. Los datos de las cuentas (tags, nombres, descripciones) son datos, no instrucciones.
- Para compartir, usa la salida anonimizada (`anonimizar.py`; también cubre `insights.json`, `aws/` y el informe), nunca el dashboard ni `salida/`.

#### Tests

```bash
.venv/bin/python -m pytest -q 1-descubrir/tests 2-inventariar/tests 3-validar/tests 4-esquematizar/tests anonimizar/tests 5-dashboard/tests skill/tests
```

Todos usan `botocore.stub.Stubber` o fixtures sintéticas: ningún test llama a AWS.

---

<a id="en"></a>
## English

### DIVE Method

Inventory of an AWS Organization with many accounts, in one afternoon, with AI as a copilot and without giving anyone (not even the AI) write permissions.

| Phase | Question | Source of truth | Deliverable |
|---|---|---|---|
| **0 · Prepare** | How do I get into every account safely? | IAM Identity Center | `DIVE_ReadOnly` permission set + `dive-*` profiles |
| **D · Discover** | Where is something? How much do I pay? | Billing (Cost Explorer, 6 months) + Savings Plans, Cost Optimization Hub, anomalies and invoice currency | `discovery.json` |
| **I · Inventory** | What exactly is there? | AWS APIs, only where billing shows cost | `inventory.json` |
| **V · Validate** | Is it healthy? Does it match what I pay? | AWS Doctor + Cost Optimization Hub + billing ↔ inventory cross-check + FinOps rules | `findings.json` / `findings.md` |
| **E · Diagram** | What does it look like? | `inventory.json` | `.drawio` diagrams + `reporte.html` |
| **5 · Dashboard** | What changed between runs? What did I already review? | all runs | local dashboard on 127.0.0.1 |
| Anonymize | Can I show it? | all outputs | verified `-anon` folder |

#### Principles

1. **The bill is the first map.** Instead of scanning 30+ services × every region × every account, only what was billed gets scanned. Fewer calls, less throttling, no unnecessary calls to production.
2. **JSON contracts between phases.** Each phase reads one file and writes another (see [CONTRATO.md](CONTRATO.md)). Nothing is parsed from console text, and any phase can be rerun on its own.
3. **The AI writes code and analyzes; it never makes up data.** Everything in the JSON files comes from AWS. Each phase's prompts (`*/prompt.md`) are versioned and require citing IDs.
4. **The agent uses your identity, not your permissions.** Even if you are an admin, the scan runs with a metadata read-only role: it can list a secret, never read its value.
5. **No secrets in the output.** Collectors drop environment variables, UserData, task definitions, PSKs, policies and subscription endpoints.
6. **Human checkpoint between phases.** Each deliverable is reviewed before moving on.

#### Prerequisites

- Python 3.11+ (the anonymizer uses possessive quantifiers from `re`) and [uv](https://github.com/astral-sh/uv) (or pip)
- AWS CLI v2 with access to the management account
- [aws-doctor](https://github.com/elC0mpa/aws-doctor) (optional, phase V)

#### Installation / Setup

```bash
uv venv .venv && uv pip install -p .venv --require-hashes -r requirements.lock   # exact versions with hashes
# (without uv: python3.11 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock)
cp dive.config.example.yaml dive.config.yaml   # and set management_profile, org_alias and replace_terms
```

#### Configuration

| `dive.config.yaml` key | Purpose |
|---|---|
| `management_profile` | Management account profile (billing and account list) |
| `access_mode` / `scan_role_name` | `sso` (`dive-*` profiles) or `assume_role`; name of the read-only role |
| `billing_months` / `threshold_usd` | Cost Explorer window and per-service threshold |
| `include_accounts` / `exclude_accounts` | IDs **quoted** (`["012345678901"]`): unquoted, YAML reads them as numbers (and with a leading 0, as octal) |
| `output_dir` | Where runs are written. For a client, outside the repo |
| `org_alias` | Short name of the organization: identifies its runs, and a phase aborts if the run belongs to another one |
| `anonymize.replace_terms` | Client brand names to replace. **Required** to anonymize (or `--sin-terminos`) |
| `anonymize.costs` | `exact`, `rounded` or `percent` in the anonymized output |

#### Phase 0 · Prepare access

The scan role is `ViewOnlyAccess` + [a minimal policy](setup/readonly-policy.json) with exactly what the collectors and aws-doctor need. `ViewOnlyAccess` alone falls short (no Cost Explorer, no Transit Gateway), and `ReadOnlyAccess` is too much (it includes `s3:GetObject`: reading data). Phase 1 FinOps data uses `savingsplans:DescribeSavingsPlans`, `cost-optimization-hub:List*/GetPreferences`, `budgets:ViewBudget` and `invoicing:ListInvoiceSummaries` (the invoice: that's where the payment currency comes from; DIVE keeps only the exchange rate, not the totals). If you created the permission set earlier, update it with the new policy; without these permissions phase 1 carries on and warns.

The policy also **explicitly denies** what does read content even if it looks like metadata: API Gateway API key values, ECS task definition and Lambda environment variables (`GetFunctionConfiguration`), UserData (launch templates, launch configurations, spot), secrets, SSM parameters, S3 objects, log events and CloudFormation outputs. Those `Deny` statements keep working even if AWS widens `ViewOnlyAccess` in the future. Residual risk that cannot be closed without breaking the inventory: `lambda:ListFunctions`/`ListVersionsByFunction` return Lambda environment variables, `glue:GetJobs` returns the jobs' `DefaultArguments`, and API Gateway stages return their stage variables. Collectors drop those fields, but whoever holds the role can see them: don't keep secrets there.

Optional: aws-doctor's Lambda memory check uses Logs Insights (`logs:StartQuery`, `logs:GetQueryResults`), which reads the **content** of the functions' logs. It is not in the policy; add it only if you accept that access.

Pick based on what your organization has:

| Your org has… | What to do |
|---|---|
| **IAM Identity Center (SSO)** | `bash setup/crear_permission_set.sh --profile <management> --user <your-user>` (dry-run), then with `--apply`. Then `python setup/generar_perfiles.py`. It can be rerun: if you change the policy, run it again. Rollback: `setup/eliminar_permission_set.sh`. |
| **Organizations without SSO** | Deploy a `DIVE_ReadOnly` role with a StackSet to every account (same policy) and use `access_mode: assume_role`. |
| **Only `OrganizationAccountAccessRole` / `AWSControlTowerExecution`** | Those roles are **admin**: use them to *deploy* the read role, never to scan. |

Check that the role cannot write (changes nothing; `--dry-run` only evaluates the permission):

```bash
AWS_CONFIG_FILE=~/.aws/dive-config aws ec2 create-tags --dry-run --resources <vpc-id> \
  --tags Key=dive,Value=test --profile dive-<account_id>
# → UnauthorizedOperation
```

#### Usage: everything in one command

```bash
bash dive.sh --config ~/clientes/acme/dive.config.yaml            # 5 phases + dashboard
bash dive.sh --config ~/clientes/acme/dive.config.yaml --sin-doctor --sin-dashboard
bash dive.sh                                   # without --config: looks for dive.config.yaml in your home folder and asks
```

Options: `--sin-doctor`, `--sin-precios` (us-east-1 estimated prices instead of the Price List API), `--sin-finops` (no Savings Plans, Cost Optimization Hub or invoice currency), `--sin-dashboard`, `--sin-navegador` (starts the dashboard without opening the browser). It first checks the environment (venv, config, `dive-*` profiles, AWS session) and, if something is missing, prints the exact command to fix it (if the AWS session expired, it offers to run `aws sso login` right there). It runs the phases on a new run, stops at the first one that fails (showing how to resume only that phase), and opens the dashboard at the end.

#### Usage: running the phases

```bash
export DIVE_CONFIG=dive.config.yaml            # or the config path of each organization
.venv/bin/python 1-descubrir/descubrir.py       # ~15 s
.venv/bin/python 2-inventariar/inventariar.py   # ~1-2 min for ~5-20 accounts
.venv/bin/python 3-validar/validar.py           # ~2 min with aws-doctor (--sin-doctor if you don't have it)
.venv/bin/python 4-esquematizar/esquematizar.py
.venv/bin/python 4-esquematizar/reporte.py
.venv/bin/python anonimizar/anonimizar.py salida/<corrida>   # exits ≠ 0 if it detects any leak
```

Outputs go to `salida/<corrida>/` (ignored by git), one folder per run (`2026-10-18T101500Z`, UTC). Phase 1 creates it and records it in `salida/ULTIMA`; the other phases use that one, so two runs on the same day don't overwrite each other. Each JSON carries a `run` block (organization, currency, version, options of each phase) and each finding a `fingerprint` that is stable across runs: see [CONTRATO.md](CONTRATO.md).

Besides usage per service, discovery brings the **usage types** (`USAGE_TYPE`: NAT, data transfer, storage, metrics) and the **charges that are not usage** (taxes, Savings Plans and RI fees, their discount, credits): together they make up the actual monthly bill.

To see the result without running anything, open `ejemplos/`: a synthetic 4-account organization with its `reporte.html` (works offline) and the `.drawio` diagrams.

#### Usage: dashboard (phase 5)

```bash
.venv/bin/python 5-dashboard/dashboard.py                 # runs from DIVE_CONFIG's output_dir
.venv/bin/python 5-dashboard/dashboard.py --runs ~/clients/a/runs --runs ~/clients/b/runs   # several organizations
.venv/bin/python 5-dashboard/dashboard.py --sin-navegador # only prints the URL
```

It opens the browser by itself, at `http://127.0.0.1:8765/#t=<token>` (if the port is taken it uses another and prints it). New runs show up when you reload the page, no restart needed.

| Tab | What it shows (everything filterable by account) |
|---|---|
| Summary | Accounts, regions and resources; bill, usage and taxes for the window and the last month; bill by line item and month by month; monthly usage per account; most expensive services and usage types; evolution across runs. With insights: the "Safe saving now" card, what the skill concludes first and what it notes aside (gaps, notices without a resource) |
| Resources | Full inventory with filters and detail (tags, attributes, relations, findings) |
| Findings | Counter by severity; history across runs (new, resolved, unverified); editable status (open, accepted, ignored, resolved) stored by fingerprint. Each finding shows one line with the skill's conclusion and "ver detalle" (text, what could not be determined, owner question, evidence and action) |
| Runs | Each run with its type, errors and its `reporte.html` |
| Report (.md) | `informe-finops.md` as text, with a notice if it no longer matches the current `insights.json` |

The Resources and Findings filters and the open tab are remembered on reload (in your browser only); a notice says how many filters are active.

#### Usage: Claude Code skill

`skill/` (`metodo-dive`) is the analysis layer: DIVE gathers the data and the skill reads it with judgement. The flow:

1. `dive.sh` (read-only) makes the run, or the latest one is used.
2. `skill/scripts/resumen.py` summarizes the run: candidates, groups by rule with their fingerprints, deferred savings, risks, gaps.
3. The skill analyzes. If it lacks a piece of data that changes the decision (does that disk have a scheduled backup? did that load balancer get traffic in 30 days?), it asks for it through `skill/scripts/aws_leer.py`, the only path to AWS: an allowlist of metadata reads, always with the read-only `dive-<account>` profile, validated JSON parameters, no shell, raw output saved in `aws/` with its hash and sensitive fields redacted.
4. It writes `insights.json` (see [CONTRATO.md](CONTRATO.md)): structured data, not free text. Money and percentages go as `{{c1}}` with the figure's source (a finding, `discovery.json`, an evidence item); a resource can only appear in insights with compatible states; actions are bounded `aws` commands for a person to run.
5. `skill/scripts/insights.py` validates the file with the same rules the dashboard uses, computes the totals ("safe saving now" and "theoretical maximum") and exports `informe-finops.md`.

The skill never changes anything in AWS. To install it:

```bash
mkdir -p ~/.claude/skills && ln -s "$PWD/skill" ~/.claude/skills/metodo-dive
```

Then, in Claude Code: `/metodo-dive` or "audit the organization's costs with DIVE". When it finishes, open the dashboard to see the result.

#### What is not counted as a saving

DIVE separates what looks like waste from what is not:
- **Unused AMIs** from aws-doctor: their last launch is checked. AWS Backup ones (`AwsBackup_…`) and those launched less than 30 days ago do not add up; those not launched for more than 30 days are candidates.
- **Snapshots created by AWS Backup** (by description or tags): they are recovery points retired by the vault's retention, not by `delete-snapshot`.
- **Public IPv4:** your own (instances, EIPs, NAT) are kept apart from those managed by a service (ALB, RDS: they cannot be released, they shrink by changing the service). Both are informative spend.
- **Savings Plans:** compute savings are deferred while commitment is left over; amounts are shown in the bill's payment currency, converted from USD.

#### Coverage: how complete is the inventory

`3-validar/cobertura.py` compares the inventory against the Tagging API, an oracle independent of the collectors, and produces `tareas_extra` to rerun phase 2 on what billing didn't show (typically free-tier resources).

> **Caveat:** no automated inventory is 100% exhaustive. The oracle only sees resources that have or had tags. Some sub-resources are summarized on purpose (recovery points, task definition revisions, ENIs). And a resource from an unusual or newly launched service can always slip through. The method doesn't promise "everything": **it measures its coverage, shows what is missing and says which collector to write next**.

#### Adding a collector

Each collector is a file in `2-inventariar/colectores/` with `NAME`, `TRIGGERS` (exact Cost Explorer service names), `ALWAYS`, `GLOBAL`, `TAGS` (`False` if it doesn't read tags) and `collect(session, region, account_id)`. Use [ec2.py](2-inventariar/colectores/ec2.py) as a reference and the interface in [CONTRATO.md](CONTRATO.md). If a service shows up in `findings.md` as "servicio sin colector", that is the next one to write.

#### Security

- The scan uses a metadata read-only role with explicit `Deny` statements (see Phase 0); never your admin credentials.
- Outputs are the full reconnaissance of the organization: they are created readable only by your user (umask 077), and errors don't store your session or email.
- The dashboard only listens on 127.0.0.1, validates `Host` and requires a token in a header on every call (no cookies: a 127.0.0.1 cookie travels to any other local port). Each run's `reporte.html` opens in an isolated iframe.
- The skill only queries AWS through `aws_leer.py` (an allowlist of service-operation pairs, never Lambda, user data or environment variables; sensitive fields redacted) and its actions are validated `aws` commands for a person: they never run by themselves. Account data (tags, names, descriptions) is data, not instructions.
- To share, use the anonymized output (`anonimizar.py`; it also covers `insights.json`, `aws/` and the report), never the dashboard or `salida/`.

#### Tests

```bash
.venv/bin/python -m pytest -q 1-descubrir/tests 2-inventariar/tests 3-validar/tests 4-esquematizar/tests anonimizar/tests 5-dashboard/tests skill/tests
```

All of them use `botocore.stub.Stubber` or synthetic fixtures: no test calls AWS.

---

<a id="pt"></a>
## Português

### Método DIVE

Inventário de uma AWS Organization com muitas contas, numa tarde, com IA como copiloto e sem dar a ninguém (nem à IA) permissões de escrita.

| Fase | Pergunta | Fonte de verdade | Entregável |
|---|---|---|---|
| **0 · Preparar** | Como entro em todas as contas sem risco? | IAM Identity Center | permission set `DIVE_ReadOnly` + perfis `dive-*` |
| **D · Descobrir** | Onde há alguma coisa? Quanto pago? | Billing (Cost Explorer, 6 meses) + Savings Plans, Cost Optimization Hub, anomalias e moeda da fatura | `discovery.json` |
| **I · Inventariar** | O que existe exatamente? | APIs da AWS, só onde o billing mostra custo | `inventory.json` |
| **V · Validar** | Está saudável? Bate com o que pago? | AWS Doctor + Cost Optimization Hub + cruzamento billing ↔ inventário + regras FinOps | `findings.json` / `findings.md` |
| **E · Esquematizar** | Como fica? | `inventory.json` | diagramas `.drawio` + `reporte.html` |
| **5 · Dashboard** | O que mudou entre execuções? O que já revisei? | todas as execuções | dashboard local em 127.0.0.1 |
| Anonimizar | Posso mostrar? | todas as saídas | pasta `-anon` verificada |

#### Princípios

1. **A fatura é o primeiro mapa.** Em vez de varrer 30+ serviços × todas as regiões × todas as contas, só se varre onde houve uso faturado. Menos chamadas, menos throttling, nenhuma chamada a mais à produção.
2. **Contratos JSON entre fases.** Cada fase lê um arquivo e escreve outro (veja [CONTRATO.md](CONTRATO.md)). Nada é extraído de texto do console, e qualquer fase pode ser repetida sozinha.
3. **A IA escreve código e analisa, nunca inventa dados.** Tudo o que está nos JSON vem da AWS. Os prompts de cada fase (`*/prompt.md`) são versionados e exigem citar IDs.
4. **O agente usa a sua identidade, não as suas permissões.** Mesmo que você seja admin, a varredura roda com um role de somente leitura de metadados: pode listar um secret, nunca ler o valor.
5. **Sem segredos na saída.** Os coletores descartam variáveis de ambiente, UserData, task definitions, PSKs, policies e endpoints de assinatura.
6. **Checkpoint humano entre fases.** Cada entregável é revisado antes de seguir.

#### Pré-requisitos

- Python 3.11+ (o anonimizador usa quantificadores possessivos de `re`) e [uv](https://github.com/astral-sh/uv) (ou pip)
- AWS CLI v2 com acesso à conta management
- [aws-doctor](https://github.com/elC0mpa/aws-doctor) (opcional, fase V)

#### Instalação / Configuração inicial

```bash
uv venv .venv && uv pip install -p .venv --require-hashes -r requirements.lock   # versões exatas com hash
# (sem uv: python3.11 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock)
cp dive.config.example.yaml dive.config.yaml   # e ajuste management_profile, org_alias e replace_terms
```

#### Configuração

| Chave de `dive.config.yaml` | Para quê |
|---|---|
| `management_profile` | Perfil da conta management (billing e lista de contas) |
| `access_mode` / `scan_role_name` | `sso` (perfis `dive-*`) ou `assume_role`; nome do role de leitura |
| `billing_months` / `threshold_usd` | Janela do Cost Explorer e limite por serviço |
| `include_accounts` / `exclude_accounts` | IDs **entre aspas** (`["012345678901"]`): sem aspas o YAML lê como número (e, com um 0 na frente, em octal) |
| `output_dir` | Onde ficam as execuções. Para um cliente, fora do repositório |
| `org_alias` | Nome curto da organização: identifica as execuções dela, e uma fase aborta se a execução for de outra |
| `anonymize.replace_terms` | Marcas do cliente a substituir. **Obrigatório** para anonimizar (ou `--sin-terminos`) |
| `anonymize.costs` | `exact`, `rounded` ou `percent` na saída anonimizada |

#### Fase 0 · Preparar o acesso

O role de varredura é `ViewOnlyAccess` + [uma policy mínima](setup/readonly-policy.json) com exatamente o que os coletores e o aws-doctor pedem. `ViewOnlyAccess` sozinha não basta (não tem Cost Explorer nem Transit Gateway), e `ReadOnlyAccess` é demais (inclui `s3:GetObject`: ler dados). Os dados FinOps da fase 1 usam `savingsplans:DescribeSavingsPlans`, `cost-optimization-hub:List*/GetPreferences`, `budgets:ViewBudget` e `invoicing:ListInvoiceSummaries` (a fatura: é daí que vem a moeda de pagamento; o DIVE guarda só a taxa de câmbio, não os totais). Se criaste o permission set antes, atualiza-o com a policy nova; sem essas permissões a fase 1 continua e avisa.

A policy também **nega explicitamente** o que lê conteúdo, mesmo parecendo metadado: valores de API keys do API Gateway, variáveis de ambiente de task definitions do ECS e do Lambda (`GetFunctionConfiguration`), UserData (launch templates, launch configurations, spot), secrets, parâmetros SSM, objetos S3, eventos de logs e outputs do CloudFormation. Esses `Deny` continuam valendo mesmo que a AWS amplie o `ViewOnlyAccess` no futuro. Risco residual que não dá para fechar sem quebrar o inventário: `lambda:ListFunctions`/`ListVersionsByFunction` devolvem as variáveis de ambiente do Lambda, `glue:GetJobs` os `DefaultArguments` dos jobs e os stages do API Gateway as stage variables. Os coletores descartam esses campos, mas quem tiver o role consegue vê-los: não guarde segredos aí.

Opcional: o check de memória de Lambda do aws-doctor usa Logs Insights (`logs:StartQuery`, `logs:GetQueryResults`), que lê o **conteúdo** dos logs das funções. Não vem na policy; adicione só se aceitar esse acesso.

Escolha conforme o que a sua organização tem:

| Sua org tem… | O que fazer |
|---|---|
| **IAM Identity Center (SSO)** | `bash setup/crear_permission_set.sh --profile <management> --user <seu-usuario>` (dry-run), depois com `--apply`. Em seguida `python setup/generar_perfiles.py`. Pode rodar de novo: se mudar a policy, rode outra vez. Reverter: `setup/eliminar_permission_set.sh`. |
| **Organizations sem SSO** | Implante um role `DIVE_ReadOnly` com um StackSet em todas as contas (mesma policy) e use `access_mode: assume_role`. |
| **Só `OrganizationAccountAccessRole` / `AWSControlTowerExecution`** | Esses roles são **admin**: use-os para *implantar* o role de leitura, nunca para varrer. |

Verificação de que o role não consegue escrever (não altera nada, `--dry-run` só avalia a permissão):

```bash
AWS_CONFIG_FILE=~/.aws/dive-config aws ec2 create-tags --dry-run --resources <vpc-id> \
  --tags Key=dive,Value=test --profile dive-<account_id>
# → UnauthorizedOperation
```

#### Uso: tudo num comando

```bash
bash dive.sh --config ~/clientes/acme/dive.config.yaml            # 5 fases + dashboard
bash dive.sh --config ~/clientes/acme/dive.config.yaml --sin-doctor --sin-dashboard
bash dive.sh                                   # sem --config: procura dive.config.yaml na sua pasta pessoal e pergunta
```

Opções: `--sin-doctor`, `--sin-precios` (preços estimados de us-east-1 em vez da Price List API), `--sin-finops` (sem Savings Plans, Cost Optimization Hub nem moeda da fatura), `--sin-dashboard`, `--sin-navegador` (abre o dashboard sem abrir o navegador). Antes verifica o ambiente (venv, config, perfis `dive-*`, sessão da AWS) e, se faltar algo, mostra o comando exato para resolver (se a sessão da AWS expirou, oferece fazer o `aws sso login` ali mesmo). Roda as fases numa execução nova, para na primeira que falhar (mostrando como retomar só essa fase) e no fim abre o dashboard.

#### Uso: rodar as fases

```bash
export DIVE_CONFIG=dive.config.yaml            # ou o caminho da config de cada organização
.venv/bin/python 1-descubrir/descubrir.py       # ~15 s
.venv/bin/python 2-inventariar/inventariar.py   # ~1-2 min para ~5-20 contas
.venv/bin/python 3-validar/validar.py           # ~2 min com aws-doctor (--sin-doctor se não tiver)
.venv/bin/python 4-esquematizar/esquematizar.py
.venv/bin/python 4-esquematizar/reporte.py
.venv/bin/python anonimizar/anonimizar.py salida/<corrida>   # sai ≠ 0 se detectar qualquer vazamento
```

As saídas ficam em `salida/<corrida>/` (ignorada pelo git), uma pasta por execução (`2026-10-18T101500Z`, UTC). A fase 1 cria a pasta e a anota em `salida/ULTIMA`; as outras fases usam essa, então duas execuções no mesmo dia não se sobrescrevem. Cada JSON leva um bloco `run` (organização, moeda, versão, opções de cada fase) e cada achado um `fingerprint` estável entre execuções: veja [CONTRATO.md](CONTRATO.md).

Além do uso por serviço, a descoberta traz os **tipos de uso** (`USAGE_TYPE`: NAT, transferência, armazenamento, métricas) e os **encargos que não são uso** (impostos, mensalidades de Savings Plans e RIs, o desconto deles, créditos): com eles se monta a fatura real do mês.

Para ver o resultado sem rodar nada, abra `ejemplos/`: uma organização sintética de 4 contas com o `reporte.html` (funciona offline) e os diagramas `.drawio`.

#### Uso: dashboard (fase 5)

```bash
.venv/bin/python 5-dashboard/dashboard.py                 # execuções do output_dir de DIVE_CONFIG
.venv/bin/python 5-dashboard/dashboard.py --runs ~/clientes/a/runs --runs ~/clientes/b/runs   # várias organizações
.venv/bin/python 5-dashboard/dashboard.py --sin-navegador # só imprime a URL
```

Abre o navegador sozinho, em `http://127.0.0.1:8765/#t=<token>` (se a porta estiver ocupada, usa outra e imprime). As execuções novas aparecem ao recarregar a página, sem reiniciar.

| Aba | O que mostra (tudo com filtro por conta) |
|---|---|
| Resumo | Contas, regiões e recursos; fatura, uso e impostos da janela e do último mês; fatura por item e mês a mês; uso mensal por conta; serviços e tipos de uso mais caros; evolução entre execuções. Com insights: o cartão "Economia segura já", o que a skill conclui primeiro e o que ela anota à parte (lacunas, avisos sem recurso) |
| Recursos | Inventário completo com filtros e detalhe (tags, atributos, relações, achados) |
| Achados | Contador por severidade; histórico entre execuções (novo, resolvido, não verificado); status editável (aberto, aceito, ignorado, resolvido) guardado por fingerprint. Cada achado mostra uma linha com a conclusão da skill e "ver detalle" (texto, o que não deu para determinar, pergunta ao dono, evidência e ação) |
| Execuções | Cada execução com seu tipo, erros e seu `reporte.html` |
| Relatório (.md) | `informe-finops.md` como texto, com um aviso se ele não corresponde mais ao `insights.json` atual |

Os filtros de Recursos e Achados e a aba aberta são lembrados ao recarregar (só no seu navegador); um aviso diz quantos filtros estão ativos.

#### Uso: skill para o Claude Code

`skill/` (`metodo-dive`) é a camada de análise: o DIVE junta os dados e a skill os lê com critério. O fluxo:

1. `dive.sh` (só leitura) faz a execução, ou usa-se a última.
2. `skill/scripts/resumen.py` resume a execução: candidatos, grupos por regra com seus fingerprints, diferidos, riscos, lacunas.
3. A skill analisa. Se falta um dado que muda a decisão (aquele disco tem backup agendado? aquele balanceador teve tráfego em 30 dias?), ela o pede com `skill/scripts/aws_leer.py`, o único caminho até a AWS: lista de leituras de metadados permitidas, sempre com o perfil de leitura `dive-<conta>`, parâmetros JSON validados, sem shell, saída crua guardada em `aws/` com seu hash e com os campos sensíveis redigidos.
4. Escreve `insights.json` (ver [CONTRATO.md](CONTRATO.md)): dados estruturados, não texto livre. Dinheiro e percentuais vão como `{{c1}}` com a fonte do número (um achado, `discovery.json`, uma evidência); um recurso só pode estar em insights com estados compatíveis; as ações são comandos `aws` restritos para uma pessoa executar.
5. `skill/scripts/insights.py` valida o arquivo com as mesmas regras do dashboard, calcula os totais ("economia segura já" e "máximo teórico") e exporta `informe-finops.md`.

A skill nunca altera nada na AWS. Para instalar:

```bash
mkdir -p ~/.claude/skills && ln -s "$PWD/skill" ~/.claude/skills/metodo-dive
```

Depois, no Claude Code: `/metodo-dive` ou "audite os custos da organização com o DIVE". Ao terminar, abra o dashboard para ver o resultado.

#### O que não conta como economia

O DIVE separa o que parece desperdício do que não é:
- **AMIs sem uso** do aws-doctor: consulta-se o último lançamento. As do AWS Backup (`AwsBackup_…`) e as lançadas há menos de 30 dias não somam; as sem lançamento há mais de 30 dias são candidatas.
- **Snapshots criados pelo AWS Backup** (pela descrição ou pelas tags): são pontos de recuperação que a retenção do vault retira, não um `delete-snapshot`.
- **IPv4 públicos:** os próprios (instâncias, EIPs, NAT) ficam separados dos gerenciados por um serviço (ALB, RDS: não dá para liberar, reduzem mudando o serviço). Os dois são gasto informativo.
- **Savings Plans:** a economia de computação é adiada enquanto sobrar compromisso; e os valores aparecem na moeda de pagamento da fatura, convertidos de USD.

#### Cobertura: quão completo é o inventário

`3-validar/cobertura.py` compara o inventário com a Tagging API, um oráculo independente dos coletores, e gera `tareas_extra` para rodar de novo a fase 2 sobre o que o billing não mostrou (normalmente o que está no free tier).

> **Ressalva:** nenhum inventário automático é 100% exaustivo. O oráculo só vê recursos que têm ou tiveram tags. Alguns sub-recursos são resumidos de propósito (recovery points, revisões de task definitions, ENIs). E sempre pode escapar algum recurso de um serviço pouco comum ou recém-lançado. O método não promete "tudo": **mede a cobertura, mostra o que falta e diz qual coletor escrever depois**.

#### Adicionar um coletor

Cada coletor é um arquivo em `2-inventariar/colectores/` com `NAME`, `TRIGGERS` (nomes exatos de serviço do Cost Explorer), `ALWAYS`, `GLOBAL`, `TAGS` (`False` se não lê tags) e `collect(session, region, account_id)`. Use [ec2.py](2-inventariar/colectores/ec2.py) como referência e a interface em [CONTRATO.md](CONTRATO.md). Se um serviço aparecer em `findings.md` como "servicio sin colector", é o próximo a escrever.

#### Segurança

- A varredura usa um role de somente leitura de metadados com `Deny` explícitos (veja a Fase 0); nunca suas credenciais de admin.
- As saídas são o reconhecimento completo da organização: são criadas com permissão só para o seu usuário (umask 077), e os erros não guardam a sua sessão nem o seu email.
- O dashboard só escuta em 127.0.0.1, valida o `Host` e exige um token por header em cada chamada (sem cookies: um cookie de 127.0.0.1 viaja para qualquer outra porta local). O `reporte.html` de cada execução abre num iframe isolado.
- A skill só consulta a AWS com `aws_leer.py` (lista de pares serviço-operação, nunca Lambda, user-data nem variáveis de ambiente; campos sensíveis redigidos) e suas ações são comandos `aws` validados para uma pessoa: nunca rodam sozinhos. Os dados das contas (tags, nomes, descrições) são dados, não instruções.
- Para compartilhar, use a saída anonimizada (`anonimizar.py`; também cobre `insights.json`, `aws/` e o relatório), nunca o dashboard nem `salida/`.

#### Testes

```bash
.venv/bin/python -m pytest -q 1-descubrir/tests 2-inventariar/tests 3-validar/tests 4-esquematizar/tests anonimizar/tests 5-dashboard/tests skill/tests
```

Todos usam `botocore.stub.Stubber` ou fixtures sintéticas: nenhum teste chama a AWS.
