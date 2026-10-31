Server-wide module that exports the traces and the logs of Odoo with OpenTelemetry,
over OTLP HTTP, to an OpenTelemetry collector (an Elastic EDOT collector, the
OpenTelemetry Collector...).

- **Traces**: one span per HTTP request, ORM call over RPC, cron job and slow SQL
  query, with the database, the user, the route, the model and the method.
- **Logs**: the log records of Odoo, with the trace and span ids of the current
  request or cron job.

It works with the multi-process server (`workers` > 0, one SDK per worker) and the
threaded server (`workers` = 0). It never breaks Odoo: when the configuration is
missing or the collector is down, Odoo runs as usual (the telemetry is lost).
