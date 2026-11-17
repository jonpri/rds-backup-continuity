# Bruno: RDS Backup Audit API

Coleccion Bruno para probar la API de auditoria del proyecto RDS Backup.

## Uso

1. Abrir Bruno.
2. Seleccionar `Open Collection`.
3. Abrir la carpeta:

```text
bruno/rds-backup-audit-api
```

4. Copiar `environments/example.bru` a `environments/qa.bru` (ignorado por git).
5. Seleccionar el environment `qa` y editar variables:

```text
baseUrl=https://<api-id>.execute-api.us-east-1.amazonaws.com/qa
apiKey=<api-key>
accountName=app-a
accountId=<account-id>
region=us-east-1
dbIdentifier=<db-rds>
operationId=<operation-id>
fromUtc=2026-06-17T00:00:00Z
toUtc=2026-06-18T23:59:59Z
```

## Obtener API key

```bash
aws apigateway get-api-keys \
  --region us-east-1 \
  --name-query rds-backup-audit-api-qa \
  --include-values
```

## Requests incluidos

- Diagnostico general de salud.
- Diagnostico por cuenta o proyecto.
- Diagnostico por base de datos.
- Diagnostico de retention.
- Candidatos de restauracion.
- Inventario de bases por proyecto.
- Diagnostico por `operationId`.
- Summary crudo.
- State crudo por cuenta.
- Events crudo general.
- Events crudo por cuenta.

## Diagnosticos recomendados

### Validar metodos compuestos del agente

Estos requests llaman endpoints compuestos reales de la API. Sirven para que el agente consuma respuestas ya priorizadas sin orquestar varios endpoints simples.

| Endpoint compuesto | Ejecutar en Bruno |
| --- | --- |
| `GET /audit/diagnostics/health` | `01 Diagnostics Health` |
| `GET /audit/diagnostics/account/{account_name}` | `02 Diagnostics Account` |
| `GET /audit/diagnostics/database/{account_name}/{region}/{db_identifier}` | `03 Diagnostics Database` |
| `GET /audit/diagnostics/retention` | `04 Diagnostics Retention` |
| `GET /audit/diagnostics/restore-candidates/{account_name}/{region}/{db_identifier}` | `05 Diagnostics Restore Candidates` |
| `GET /audit/diagnostics/databases/{account_name}` | `06 Diagnostics Databases` |
| `GET /audit/diagnostics/operation/{operation_id}` | `07 Diagnostics Operation` |

Flujo rapido:

1. Ejecutar `01 Diagnostics Health`.
2. Para una cuenta puntual, cambiar `accountName` y ejecutar `02 Diagnostics Account`.
3. Para una DB puntual, configurar `dbIdentifier` y ejecutar `03 Diagnostics Database`.
4. Para explicar omisiones de retention, ejecutar `04 Diagnostics Retention`.
5. Para candidatos de restauracion, ejecutar `05 Diagnostics Restore Candidates`.
6. Para descubrir DBs de un proyecto, ejecutar `06 Diagnostics Databases`.
7. Para revisar una ejecucion especifica, configurar `operationId` y ejecutar `07 Diagnostics Operation`.

### Ver salud general

Usar:

```text
01 Diagnostics Health
10 Summary
```

Campos a revisar:

```text
accounts
accounts_with_backup_state
databases
databases_with_backup_state
databases_retention_only
backups.error
retention.skipped_unmanaged
```

### Saber que cuenta configurada no genero estado

Usar `11 State` cambiando `accountName` por cada cuenta configurada.

Si una cuenta responde sin items, todavia no genero estado de auditoria.

### Diagnosticar errores y eventos especificos

Usar `12 Events` o `13 Events Account` y agregar temporalmente el filtro que necesites en Bruno:

```text
status=ERROR
event_type=ASSUME_ROLE_FAILED
event_type=LIST_DBS_FAILED
event_type=BACKUP_FAILED
event_type=SNAPSHOT_SKIPPED
skip_reason=unmanaged
skip_reason=not_expired
skip_reason=keep_tag
```
