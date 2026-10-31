Load the module server-wide, in the Odoo configuration file (or with
`--load base,web,monitoring_opentelemetry`):

``` ini
server_wide_modules = base,web,monitoring_opentelemetry
```

The configuration is read from environment variables only. Without an OTLP
endpoint, the module stays inactive and logs it at startup.

Standard OpenTelemetry variables, read by the SDK:

| Variable | Default | Description |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | none | Base URL of the collector, OTLP HTTP (`http://collector:4318`); enables both signals. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | none | Full URL for one signal (`.../v1/traces`); enables this signal alone. |
| `OTEL_EXPORTER_OTLP_HEADERS` | none | Headers of the export requests, values URL-encoded: `Authorization=Bearer%20<token>` (URL-encode a token with `%`, `,`, `=` or `&`). |
| `OTEL_SERVICE_NAME` | `odoo` | Service name. |
| `OTEL_RESOURCE_ATTRIBUTES` | none | Extra resource attributes, which override those of the module: `deployment.environment.name=production`. |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | Sampling of the traces. |
| `OTEL_BSP_*`, `OTEL_BLRP_*` | SDK defaults | Batch processors of the spans and of the logs (queue size, delay). |
| `OTEL_SDK_DISABLED` | `false` | `true` disables the module entirely. |

Variables of the module:

| Variable | Default | Description |
| --- | --- | --- |
| `ODOO_OTEL_TRACES` | `true` | Export the traces (when an endpoint is set). |
| `ODOO_OTEL_LOGS` | `true` | Export the logs (when an endpoint is set). |
| `ODOO_OTEL_LOG_LEVEL` | `INFO` | Minimum level of the exported logs, name or number. It filters on top of the Odoo log levels (`--log-level`, `--log-handler`): a record that Odoo does not log is never exported. |
| `ODOO_OTEL_EXCLUDED_LOGGERS` | none | More loggers not exported, with their children. Always excluded: `opentelemetry`, `urllib3` and `requests` (the exporter), `odoo.http.server` and `werkzeug` (the HTTP access logs, which hold the query string and the session id; the traces replace them). |
| `ODOO_OTEL_EXCLUDED_PATHS` | `/websocket` | URL path prefixes without span. Static files never have one. |
| `ODOO_OTEL_SQL` | `slow` | Spans of the SQL queries: `off`, `slow` (queries lasting at least `ODOO_OTEL_SQL_SLOW_MS`) or `all`. The count and the total duration of the queries are on the request and cron spans in every mode. |
| `ODOO_OTEL_SQL_SLOW_MS` | `100` | Threshold of the `slow` mode, in milliseconds. |

Example, in a Kubernetes deployment, with the token in a Secret:

``` yaml
env:
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: http://otel-gateway.observability.svc:4318
  - name: OTLP_TOKEN
    valueFrom:
      secretKeyRef: {name: otlp, key: token}
  - name: OTEL_EXPORTER_OTLP_HEADERS
    value: Authorization=Bearer%20$(OTLP_TOKEN)
  - name: OTEL_RESOURCE_ATTRIBUTES
    value: deployment.environment.name=production
```
