# Contrato para agente: RDS Backup Continuity

Este documento consolida las caracteristicas de la API y las reglas de interpretacion pensadas para un agente que consulte el estado de backups y retencion.

La API es de solo lectura y esta orientada a responder preguntas operativas como:

- Que cuentas tienen backups exitosos recientes.
- Que bases fueron omitidas.
- Donde existen errores de backup o retention.
- Que registros tienen solo informacion de retention y no de backup.
- Cual fue la ultima actividad conocida por base de datos.

## Capacidades esperadas del agente

El agente debe poder responder en lenguaje natural preguntas de salud, diagnostico, auditoria y explicacion. La API entrega datos; el agente agrega contexto operacional y priorizacion.

## Metodos compuestos del agente

La API expone endpoints compuestos bajo `/audit/diagnostics/*` para que el agente no tenga que orquestar varias llamadas simples. Estos endpoints devuelven estructuras listas para razonar, priorizar y explicar.

Metodos recomendados:

| Endpoint | Proposito |
| --- | --- |
| `GET /audit/diagnostics/health` | Salud general o por cuenta, con severidad, cuentas afectadas y acciones recomendadas. |
| `GET /audit/diagnostics/account/{account_name}` | Diagnostico por proyecto/cuenta con cobertura por DB y errores agrupados. |
| `GET /audit/diagnostics/databases/{account_name}` | Descubre las DBs conocidas de un proyecto y resume salud de backup/retention por DB. |
| `GET /audit/diagnostics/database/{account_name}/{region}/{db_identifier}` | Estado y eventos de una DB especifica. |
| `GET /audit/diagnostics/retention` | Explica omisiones de retention por `unmanaged`, `not_expired` y `keep_tag`. |
| `GET /audit/diagnostics/restore-candidates/{account_name}/{region}/{db_identifier}` | Lista snapshots exitosos candidatos para restauracion, ordenados del mas reciente al mas antiguo. |
| `GET /audit/diagnostics/operation/{operation_id}` | Entrega el detalle de una ejecucion especifica de backup o retention, con eventos, cuentas afectadas y errores. |

Todos aceptan `from` y `to` como filtros ISO-8601 UTC cuando aplica. `health` y `retention` tambien aceptan `account_name`.

El campo `database_health` de `/audit/diagnostics/health` resume salud de continuidad por DB: backup, retention, cobertura y ultimos timestamps. No representa salud de performance RDS como CPU, conexiones, latencia o storage; esas senales deben venir de CloudWatch/RDS en un servicio complementario.

El campo `error_health` separa errores activos de errores historicos resueltos. Un error se considera resuelto si existe actividad exitosa posterior en la misma cuenta, por ejemplo un backup `SUCCESS` despues de un `ASSUME_ROLE_FAILED`. El agente debe priorizar `error_health.active`; `summary.backups.error` puede conservar conteo historico dentro de la ventana consultada.

## Flujo recomendado de consulta

Para validar si el proceso opera correctamente, el agente debe consultar en este orden:

| Paso | Pregunta | Endpoint |
| --- | --- | --- |
| 1 | Esta saludable la plataforma? | `GET /audit/diagnostics/health` |
| 2 | Como esta un proyecto o cuenta especifica? | `GET /audit/diagnostics/databases/{account_name}` |
| 3 | Hay snapshots restaurables recientes para una DB? | `GET /audit/diagnostics/restore-candidates/{account_name}/{region}/{db_identifier}` |

Campos clave por endpoint:

| Endpoint | Campos a mirar |
| --- | --- |
| `/audit/diagnostics/health` | `status`, `summary.accounts`, `summary.accounts_with_backup_state`, `summary.retention.error`, `error_health.active`, `database_health.healthy`, `database_health.degraded`, `database_health.retention_only` |
| `/audit/diagnostics/databases/{account_name}` | `databases`, `healthy`, `degraded`, `items[].db_identifier`, `items[].status`, `items[].backup.status`, `items[].backup.last_at`, `items[].backup.snapshot_id`, `items[].retention.status` |
| `/audit/diagnostics/restore-candidates/{account_name}/{region}/{db_identifier}` | `items[0].created_at`, `items[0].snapshot_id`, `items[0].db_identifier` |

Reglas practicas:

| Intencion del usuario | Endpoint recomendado |
| --- | --- |
| "Esta funcionando la plataforma?" | `/audit/diagnostics/health` |
| "Como esta el proyecto app-b?" | `/audit/diagnostics/databases/app-b` |
| "Hay backups para db-app-b-prod?" | `/audit/diagnostics/restore-candidates/app-b/us-east-1/db-app-b-prod` |
| "Que paso en esta ejecucion?" | `/audit/diagnostics/operation/{operation_id}` |
| "Por que retention omitio snapshots?" | `/audit/diagnostics/retention` |
| "Necesito investigar un evento puntual" | `/audit/events` o `/audit/events/{account_name}` |

`/audit/summary` debe tratarse como resumen crudo de auditoria. Sirve para conteos, pero no reemplaza `/audit/diagnostics/health` para decidir salud operacional.

## Operation ID

Cada ejecucion de `rds-backup-runner` y `rds-backup-retention` genera un `operation_id` UUID. Todos los eventos producidos por esa ejecucion incluyen ese valor, y el estado de cada DB conserva el ultimo `last_backup_operation_id` y `last_retention_operation_id`.

Uso del agente:

- Si el usuario pregunta por una ejecucion especifica, consultar `GET /audit/diagnostics/operation/{operation_id}`.
- Si el usuario pregunta si un error pertenece a la ultima ejecucion, comparar el `operation_id` de los eventos con `last_backup_operation_id` o `last_retention_operation_id` del estado.
- Si el usuario pregunta por una DB, el endpoint de DB puede indicar el ultimo `operation_id` asociado al backup y retention de esa DB.

Ejemplo:

```text
GET /audit/diagnostics/operation/987b1570-dcca-4a74-bcf9-2b835a698d3f
```

Respuesta esperada:

```json
{
  "operation_id": "987b1570-dcca-4a74-bcf9-2b835a698d3f",
  "status": "completed",
  "source": "RdsBackup",
  "started_at": "2026-06-18T12:00:01Z",
  "finished_at": "2026-06-18T12:00:08Z",
  "accounts": 8,
  "databases": 8,
  "event_counts": {
    "BACKUP_CREATED": {
      "success": 8
    }
  },
  "errors": [],
  "events": []
}
```

Estados de salud:

| Estado | Significado |
| --- | --- |
| `healthy` | No hay errores observados y la cobertura actual no muestra alertas. |
| `observing-retention-only` | Hay DBs vistas por retention, pero aun no existe estado de backup. Normal despues de desplegar antes de que corra el runner. |
| `degraded` | Hay errores de backup/retention o fallas puntuales de DB. |
| `critical` | Hay fallas que afectan una cuenta completa, por ejemplo `ASSUME_ROLE_FAILED` o `LIST_DBS_FAILED`. |

Ejemplo de salida de `GET /audit/diagnostics/health`:

```json
{
  "status": "critical",
  "summary": {},
  "priorities": [
    "Validar trust policy, ExternalId y despliegue del rol RdsBackupCrossAccountRole."
  ],
  "affected_accounts": [
    {
      "account": "app-d",
      "count": 1,
      "event_types": {
        "ASSUME_ROLE_FAILED": 1
      }
    }
  ],
  "affected_databases": [],
  "recommended_actions": [
    {
      "priority": "high",
      "action": "Validar que el rol RdsBackupCrossAccountRole exista en la cuenta hija y permita AssumeRole desde los roles centrales con el ExternalId esperado."
    }
  ]
}
```

### Salud general

Preguntas ejemplo:

- Esta funcionando el sistema de backups?
- Como esta la salud general de RDS Backup?
- Hubo errores en las ultimas 24 horas?
- Cuando fue la ultima ejecucion registrada?
- Cuantas cuentas y bases estan siendo auditadas?

Endpoint recomendado:

```text
GET /audit/diagnostics/health?from={from}&to={to}
```

Senales:

- `status`
- `summary.last_event_at`
- `summary.backups.success`
- `summary.retention.error`
- `database_health`
- `error_health.active`
- `error_health.resolved`
- `priorities`
- `recommended_actions`

Respuesta esperada:

```text
El sistema esta saludable. Hay 8 cuentas observadas, 8 cuentas con estado de backup, 0 errores activos y retention sin errores. Existe 1 error historico resuelto, ya superado por actividad exitosa posterior.
```

### Backups

Preguntas ejemplo:

- Que bases tuvieron backup exitoso?
- Que bases fueron omitidas?
- Que cuenta tiene errores de backup?
- Cual fue el ultimo snapshot de una DB?
- La DB `{db_identifier}` tiene backup reciente?

Endpoints recomendados:

```text
GET /audit/diagnostics/databases/{account_name}
GET /audit/diagnostics/database/{account_name}/{region}/{db_identifier}
GET /audit/diagnostics/restore-candidates/{account_name}/{region}/{db_identifier}
```

Senales:

- `items[].db_identifier`
- `items[].backup.health`
- `items[].backup.status`
- `items[].backup.last_at`
- `items[].backup.snapshot_id`
- `items[].backup.error`
- `coverage`
- eventos `BACKUP_CREATED`
- eventos `BACKUP_SKIPPED`
- eventos `BACKUP_FAILED`

Respuesta esperada:

```text
El proyecto app-a tiene una DB conocida, db-app-a-prod, con salud healthy para continuidad. El ultimo backup fue SUCCESS y genero el snapshot db-app-a-prod-1781726452.
```

### Retention

Preguntas ejemplo:

- La retencion esta funcionando?
- Por que hay tantos snapshots omitidos?
- Cuantos snapshots fueron omitidos por no estar vencidos?
- Cuantos snapshots manuales no administrados existen?
- Hay snapshots protegidos con keep=true?
- Que snapshots elimino retention?

Endpoints recomendados:

```text
GET /audit/diagnostics/retention?from={from}&to={to}
```

Senales:

- `retention.deleted`
- `retention.skipped`
- `retention.skipped_unmanaged`
- `retention.skipped_not_expired`
- `retention.skipped_keep_tag`
- `retention.skipped_unknown`
- `retention.error`
- `samples.unmanaged`
- `samples.not_expired`
- `samples.keep_tag`
- `interpretation`

Respuesta esperada:

```text
Retention esta funcionando sin errores. Omitio 125 snapshots: 98 aun no vencen, 27 son manuales no administrados, 0 tienen keep=true y 0 quedaron sin clasificar. Tambien elimino 19 snapshots administrados vencidos.
```

### Estado por cuenta o proyecto

Preguntas ejemplo:

- Como esta `app-a`?
- Que paso en `app-b` durante la ultima ejecucion?
- Que bases de `app-c` tienen backup exitoso?
- Que proyecto tiene mas errores?

Endpoints recomendados:

```text
GET /audit/diagnostics/account/{account_name}?from={from}&to={to}
GET /audit/diagnostics/databases/{account_name}
```

Senales:

- `account_name`
- `last_backup_status`
- `last_retention_status`
- `coverage`
- `last_activity_source`
- `last_backup_operation_id`
- `last_retention_operation_id`
- eventos con `status=ERROR`

Respuesta esperada:

```text
En app-b hay 1 DB con backup saludable y retention sin errores. No hay errores activos para el proyecto.
```

### Diagnostico de errores

Preguntas ejemplo:

- Que debo revisar primero?
- Cuales son los errores actuales?
- Hay problemas de permisos?
- Hay cuentas sin rol?
- Que evento explica el ultimo fallo?

Endpoints recomendados:

```text
GET /audit/diagnostics/health?from={from}&to={to}
GET /audit/diagnostics/account/{account_name}?from={from}&to={to}
GET /audit/events?status=ERROR&from={from}&to={to}
```

Senales:

- `event_type`
- `status`
- `message`
- `error`
- `error_code`
- `account_name`
- `db_identifier`
- `operation_id`
- `error_health.active`
- `error_health.resolved`

Respuesta esperada:

```text
No hay errores activos. Existe 1 error historico resuelto en app-d; el agente no debe marcar el sistema como degradado si `error_health.active=0`.
```

### Auditoria historica

Preguntas ejemplo:

- Que snapshots se crearon ayer?
- Que snapshots se eliminaron entre dos fechas?
- Que paso con el snapshot `{snapshot_id}`?
- Dame el historial de una DB especifica.

Endpoints recomendados:

```text
GET /audit/events?from={from}&to={to}
GET /audit/events/{account_name}?snapshot_id={snapshot_id}
GET /audit/events/{account_name}?db_identifier={db_identifier}&from={from}&to={to}
```

Senales:

- `event_type`
- `timestamp`
- `snapshot_id`
- `db_identifier`
- `message`
- `source`

Respuesta esperada:

```text
Entre las fechas consultadas se crearon 6 snapshots y se eliminaron 3 por politica de retention. No se registraron errores en retention.
```

## Endpoints recomendados

### Vista agregada

```text
GET /audit/diagnostics/health
GET /audit/diagnostics/account/{account_name}
```

Uso del agente:

- Obtener salud global.
- Detectar si existen errores activos.
- Separar errores historicos resueltos de errores activos.
- Revisar `summary.last_event_at` contra la ventana esperada de ejecucion.
- Responder preguntas de alto nivel sin recorrer todos los eventos.

`GET /audit/summary` queda disponible como resumen crudo de auditoria, pero no debe ser usado como fuente unica para decidir salud operacional.

### Estado actual

```text
GET /audit/state
GET /audit/state/{account_name}
GET /audit/state/{account_name}/{region}/{db_identifier}
```

Uso del agente:

- Identificar estado actual por base.
- Determinar si una DB tiene backup reciente.
- Detectar bases omitidas por estado invalido.
- Diferenciar registros observados por retention de registros observados por backup.

### Eventos historicos

```text
GET /audit/events
GET /audit/events/{account_name}
```

Uso del agente:

- Explicar por que un estado esta en error.
- Buscar eventos por `event_type`, `status`, `db_identifier` o `snapshot_id`.
- Reconstruir actividad dentro de una ventana temporal.

## Campos clave de `/audit/summary`

`/audit/summary` es un endpoint crudo de auditoria. Cuenta eventos dentro de la ventana consultada, pero no interpreta si un error ya fue resuelto por una ejecucion posterior. Para salud operacional, usar `/audit/diagnostics/health`.

| Campo | Semantica para el agente |
| --- | --- |
| `accounts` | Cantidad de cuentas con registros en estado. |
| `accounts_with_backup_state` | Cantidad de cuentas con al menos una DB vista por el backup runner. |
| `databases` | Cantidad de DBs o identificadores observados en estado. Puede mezclar DBs respaldadas y referencias historicas vistas por retention. |
| `databases_with_backup_state` | DBs o identificadores con informacion de backup. |
| `databases_retention_only` | DBs o identificadores observados solo por retention. |
| `databases_without_observed_state` | Registros sin backup ni retention observables; normalmente anomalia historica. |
| `backups.success` | Cantidad de eventos `BACKUP_CREATED`. |
| `backups.skipped` | Cantidad de eventos `BACKUP_SKIPPED`. |
| `backups.error` | Cantidad de errores de backup, assume role o listado de DBs. |
| `retention.deleted` | Cantidad de snapshots eliminados por retention. |
| `retention.skipped` | Cantidad de snapshots evaluados pero no eliminados. |
| `retention.skipped_unmanaged` | Snapshots manuales sin tags administrados por la solucion. |
| `retention.skipped_not_expired` | Snapshots administrados que aun estan dentro de `retention_days`. |
| `retention.skipped_keep_tag` | Snapshots protegidos por `keep=true`. |
| `retention.skipped_unknown` | Snapshots omitidos sin motivo clasificable. |
| `retention.error` | Cantidad de errores de retention. |
| `last_event_at` | Ultimo evento auditado en formato UTC ISO-8601. |

Interpretacion:

- `retention.deleted = 0` no es necesariamente un problema. Puede significar que no hay snapshots administrados vencidos.
- `retention.skipped > 0` es normal si hay snapshots manuales no administrados, snapshots recientes o snapshots con `keep=true`.
- Para explicar `retention.skipped`, el agente debe preferir los buckets `skipped_unmanaged`, `skipped_not_expired`, `skipped_keep_tag` y `skipped_unknown`.
- Para explicar `databases`, el agente debe distinguir `databases_with_backup_state` de `databases_retention_only`.
- `backups.error > 0` en summary no implica por si solo degradacion activa. El agente debe revisar `/audit/diagnostics/health.error_health.active`.
- `backups.skipped > 0` suele indicar instancias no disponibles para snapshot.

## Campos clave de `/audit/state`

| Campo | Semantica para el agente |
| --- | --- |
| `account_id` | ID AWS de la cuenta administrada. |
| `account_name` | Alias humano del proyecto/cuenta, por ejemplo `app-a`, `app-b`, `app-c`. |
| `region` | Region AWS donde se observo la DB/snapshot. |
| `db_identifier` | Identificador RDS asociado al estado. |
| `last_backup_status` | Ultimo estado de backup conocido: `SUCCESS`, `SKIPPED` o `ERROR`. |
| `last_backup_at` | Timestamp UTC del ultimo estado de backup. |
| `last_snapshot_id` | Snapshot creado por el ultimo backup exitoso. |
| `last_error` | Error de backup u omision, si existe. |
| `last_retention_status` | Ultimo estado de retention conocido: `DELETED`, `SKIPPED` o `ERROR`. |
| `last_retention_at` | Timestamp UTC del ultimo estado de retention. |
| `last_deleted_snapshot_id` | Snapshot eliminado por retention, si aplica. |
| `last_retention_error` | Error de retention, si existe. |
| `last_backup_operation_id` | Ultimo `operation_id` asociado al backup de esa DB. |
| `last_retention_operation_id` | Ultimo `operation_id` asociado a retention para esa DB. |
| `updated_at` | Ultima actualizacion del registro de estado. |

## Campos derivados para el agente

Estos campos no viven en DynamoDB. La API los calcula al responder `/audit/state`.

### `coverage`

```json
{
  "coverage": {
    "has_backup_state": true,
    "has_retention_state": true
  }
}
```

Semantica:

- `has_backup_state=true`: el registro tiene informacion de backup (`last_backup_at` o `last_backup_status`).
- `has_retention_state=true`: el registro tiene informacion de retention (`last_retention_at` o `last_retention_status`).

Casos utiles:

| Caso | Interpretacion |
| --- | --- |
| backup=true, retention=true | La DB fue observada por ambos procesos. |
| backup=true, retention=false | Hay estado de backup, pero retention aun no ha actualizado ese registro. |
| backup=false, retention=true | Retention observo snapshots asociados, pero no hay estado de backup para esa DB. |
| backup=false, retention=false | Registro incompleto o historico anomalo. |

### `last_activity_source`

Valores posibles:

```text
RdsBackup
RdsRetention
null
```

Regla:

- Si existen `last_backup_at` y `last_retention_at`, gana el timestamp mas reciente.
- Si solo existe `last_backup_at`, retorna `RdsBackup`.
- Si solo existe `last_retention_at`, retorna `RdsRetention`.
- Si no existe ninguno, retorna `null`.

Uso del agente:

- Explicar por que una fila fue actualizada recientemente.
- Priorizar analisis cuando la ultima actividad fue retention.
- Diferenciar DBs con snapshots historicos de DBs respaldadas por el runner.

## Eventos relevantes

| Evento | Fuente | Significado |
| --- | --- | --- |
| `BACKUP_CREATED` | `RdsBackup` | Snapshot manual creado correctamente. |
| `BACKUP_SKIPPED` | `RdsBackup` | DB omitida, normalmente por estado invalido. |
| `BACKUP_FAILED` | `RdsBackup` | Error creando snapshot. |
| `ASSUME_ROLE_FAILED` | `RdsBackup` | No se pudo asumir rol en cuenta administrada. |
| `LIST_DBS_FAILED` | `RdsBackup` | No se pudieron listar instancias RDS. |
| `SNAPSHOT_DELETED` | `RdsRetention` | Snapshot administrado eliminado por retention. |
| `SNAPSHOT_SKIPPED` | `RdsRetention` | Snapshot evaluado pero no eliminado. |
| `RETENTION_FAILED` | `RdsRetention` | Error durante evaluacion/eliminacion. |
| `LIST_SNAPSHOTS_FAILED` | `RdsRetention` | No se pudieron listar snapshots. |

## Reglas de severidad sugeridas

| Condicion | Severidad | Accion sugerida |
| --- | --- | --- |
| `/audit/diagnostics/health.status=critical` | Alta | Revisar `error_health.active_accounts` y eventos por `operation_id`. |
| `/audit/diagnostics/health.error_health.active > 0` | Alta | Consultar `/audit/events?status=ERROR` y, si existe, `/audit/diagnostics/operation/{operation_id}`. |
| `/audit/summary.backups.error > 0` con `error_health.active=0` | Informativa | Reportar como error historico resuelto, no como incidente activo. |
| `last_backup_status=ERROR` | Alta | Revisar `last_error` y eventos de la DB. |
| `last_backup_status=SKIPPED` | Media | Revisar si la instancia estaba disponible. |
| `coverage.has_backup_state=false` y `coverage.has_retention_state=true` | Informativa | Indicar que retention observo snapshots, pero no hay backup registrado. |
| `retention.error > 0` | Alta | Consultar eventos `RETENTION_FAILED` o `LIST_SNAPSHOTS_FAILED`. |
| `retention.deleted=0` | Informativa | No alertar por si solo. |
| `retention.skipped_unknown > 0` | Baja | Revisar eventos `SNAPSHOT_SKIPPED` para clasificar nuevos motivos. |

## Ventanas temporales

El agente debe preferir consultas con `from` y `to` cuando la pregunta mencione tiempo reciente o salud operacional.

Reglas sugeridas:

| Pregunta | Ventana recomendada |
| --- | --- |
| "hoy" | Desde `00:00:00Z` del dia UTC o desde inicio del dia local convertido a UTC, segun configuracion del agente. |
| "ultimas 24 horas" | `now - 24h` hasta `now`. |
| "ayer" | Rango completo del dia anterior. |
| "ultima ejecucion" | Usar el `operation_id` mas reciente desde eventos/state y consultar `/audit/diagnostics/operation/{operation_id}`. |
| Sin ventana explicita | Usar ultimas 24 horas para salud operativa; indicar que se uso esa ventana. |

Notas:

- La API acepta fechas ISO-8601 con timezone UTC.
- Si el agente usa zona local `America/Santiago`, debe convertir a UTC antes de llamar la API.
- Si el usuario solicita "todo", el agente debe advertir que la API pagina y puede requerir multiples llamadas con `next_token`.
- Para evitar conclusiones incompletas, el agente debe mencionar la ventana consultada en respuestas de salud.

Ejemplo:

```text
Revise la ventana 2026-06-15T00:00:00Z a 2026-06-16T23:59:59Z. En ese rango retention elimino 19 snapshots y no tuvo errores.
```

## Consultas utiles

Errores:

```text
GET /audit/diagnostics/health
GET /audit/events?status=ERROR
```

Backups exitosos por cuenta:

```text
GET /audit/events/{account_name}?event_type=BACKUP_CREATED&status=SUCCESS
```

Snapshots eliminados por retention:

```text
GET /audit/events/{account_name}?event_type=SNAPSHOT_DELETED&status=SUCCESS
```

Snapshots omitidos por motivo:

```text
GET /audit/events/{account_name}?event_type=SNAPSHOT_SKIPPED&skip_reason=unmanaged
GET /audit/events/{account_name}?event_type=SNAPSHOT_SKIPPED&skip_reason=not_expired
GET /audit/events/{account_name}?event_type=SNAPSHOT_SKIPPED&skip_reason=keep_tag
```

Detalle de una ejecucion:

```text
GET /audit/diagnostics/operation/{operation_id}
GET /audit/events?operation_id={operation_id}
```

Estado por DB:

```text
GET /audit/state/{account_name}/{region}/{db_identifier}
```

## Consideraciones

- Todas las fechas se exponen en UTC ISO-8601.
- El agente no debe interpretar `next_token`; solo debe reenviarlo para paginar.
- El agente no debe asumir que `retention.skipped` es un error.
- El agente debe explicar `retention.skipped` usando el breakdown de motivos cuando este disponible.
- El agente debe mencionar `account_name` en respuestas humanas cuando este disponible.
- La API oculta claves internas de DynamoDB (`pk`, `sk`, `gsi1pk`, `gsi1sk`).
