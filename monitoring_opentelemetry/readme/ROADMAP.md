Known limitations of this version:

- The gevent process (websocket, longpolling) and the prefork master are not
  instrumented: neither their metrics nor their logs (startup, worker recycling) are
  exported.
- A worker killed by Odoo (`limit_time_real`, `limit_memory_hard`) or by the system
  loses the spans, logs and metrics not exported yet; the span of the request or cron
  job it was running is lost too, and its end is not counted in
  `odoo.worker.recycles`.
- `db.client.operation.duration` measures the queries of the requests and cron jobs
  only, without the table nor the errors (a failed query is measured as the others).
- The span of a request ends when Odoo returns the response: the time spent
  streaming a large body (a file download) is not measured.
- A request retried by Odoo on a concurrency error gives one RPC span per attempt
  under the same request span.
- The log messages and exceptions are exported as Odoo writes them: a message of Odoo
  or of a module that holds personal data or a value of a record is exported too.
  Exclude its logger with `ODOO_OTEL_EXCLUDED_LOGGERS` when needed.
- When the collector is unreachable, stopping a process may wait for the export
  timeout of the pending spans and logs (`OTEL_EXPORTER_OTLP_TIMEOUT`, 10 s).
- The `traceparent` header of a request is trusted: expose Odoo behind a proxy that
  removes or sets it when the trace ids of the clients must not be used.
- `--stop-after-init` (module updates, tests) runs one process: the SDK is set up
  as in the threaded server.
