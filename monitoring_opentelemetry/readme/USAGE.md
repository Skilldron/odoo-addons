Once Odoo restarts with an endpoint set, it logs `OpenTelemetry is enabled` (or
`is enabled in each worker` with `workers` > 0) and exports:

**Spans**

| Span | Kind | Attributes |
| --- | --- | --- |
| `{METHOD} {route}`, e.g. `POST /web/dataset/call_kw/<path:path>`; `{METHOD}` alone when no route matches | `SERVER` | `http.request.method` (`http.request.method_original` for an unknown method), `http.route`, `http.response.status_code`, `url.path` (only for a route without arguments), `url.scheme`, `server.address`, `server.port`, `network.protocol.version`, `client.address`, `user_agent.original`, `odoo.db`, `odoo.uid`, `odoo.route_type`, `odoo.sql.count`, `odoo.sql.duration_ms` |
| `{model}.{method}`, e.g. `res.partner.web_search_read` | `INTERNAL` | `odoo.model`, `odoo.method` (`_OTHER` for an unknown or private method) |
| `cron {name}` | `INTERNAL`, root of its trace (child of the request span when run manually) | `odoo.cron.id`, `odoo.cron.name`, `odoo.cron.status`, `odoo.cron.timed_out`, `odoo.db`, `odoo.sql.*` |
| `{OPERATION} {table}`, e.g. `SELECT res_partner` | `CLIENT` | `db.system.name`, `db.namespace`, `db.operation.name`, `db.collection.name`, `db.query.text` |

An error sets the span status to `ERROR` with `error.type` and an `exception`
event: the first line of the message, truncated to 300 characters and as raised (it
may hold a value), and a stack trace without the local variables. A
user error (`UserError`, `AccessError`, `ValidationError`, a 4xx HTTP error...) is
not a failure of Odoo: it only sets `odoo.exception.type`. In a cron job, the exception
of a run, user error included, is an event of its span (Odoo only logs it); the span is
in error only when Odoo counts the job as `failed`.

An incoming `traceparent` header continues the trace of the caller (a reverse
proxy, another service).

**Metrics**, exported by each process every `OTEL_METRIC_EXPORT_INTERVAL`, with or
without the traces:

| Metric | Type | Attributes |
| --- | --- | --- |
| `http.server.request.duration` (s) | histogram | `http.request.method`, `http.route`, `http.response.status_code`, `url.scheme`, `network.protocol.version`, `error.type` |
| `odoo.http.sql.count` | histogram | `http.request.method`, `http.route`: the SQL queries of a request |
| `odoo.http.retries` | counter | `http.route`: the attempts of a request or an RPC call that Odoo runs again after a concurrency error |
| `odoo.rpc.duration` (s) | histogram | `odoo.model`, `odoo.method` (`_OTHER` for a failed call), `error.type`: each attempt of an ORM call over RPC |
| `db.client.operation.duration` (s) | histogram | `db.system.name`, `db.namespace`, `db.operation.name`: each SQL query of a request or a cron job |
| `db.client.connection.count`, `db.client.connection.max` | up-down counter | `db.client.connection.pool.name` (`read_write`, `read_only`), `db.client.connection.state` (`idle`, `used`) |
| `odoo.db.pool.full` | counter | `db.client.connection.pool.name`: connections refused, the pool being full |
| `odoo.cron.duration` (s) | histogram | `odoo.cron.name`, `odoo.cron.status` (`failed` when the run raised) |
| `odoo.cron.lag` (s) | histogram | `odoo.cron.name`: delay between the next call of a job and its run by the scheduler (not for a manual run, a run by a trigger, nor a job run again after `partially done`) |
| `odoo.worker.recycles` | counter | `odoo.worker.recycle.reason` (`request_limit`, `memory_soft`, `max_age`, `parent_changed`) |
| `odoo.worker.memory.limit` | up-down counter | `odoo.limit.type` (`soft`, `hard`): compare with `process.memory.virtual` |
| `process.cpu.time`, `process.memory.usage`, `process.memory.virtual`, `process.thread.count`, `process.unix.file_descriptor.count`, `process.context_switches`, `process.uptime`, `cpython.gc.*` | semantic conventions | |

A duration measured in a sampled trace has an exemplar linking to it. An error of
the user (as for the spans) has no `error.type`; a JSON-RPC error, a 200 response,
has the type of its exception.

**Logs**: each record has its level, its logger (the instrumentation scope), its
source (`code.file.path`, `code.function.name`, `code.line.number`), the trace and
span ids of the current span, and the database (`odoo.db`).

**Resource**: `service.name`, `odoo.version` (the Odoo version), `service.version`
(the Odoo version too, unless set by `OTEL_RESOURCE_ATTRIBUTES`: set it to the
version of your deployment), `service.instance.id` (the host name, the pod name in
Kubernetes), `odoo.process.type` (`http`, `cron` or `threaded`), `process.pid`. The
metrics have `service.instance.id` followed by the PID (`odoo-7d4b9-x2k8p-17`): each
worker exports its own series, which a recycled worker ends.

**Never in the spans**: the query string, the cookies, the headers other than the
user agent, the request and response bodies, the SQL parameters and the literal
values of the queries. The logs never include the HTTP access logs nor the queries
that Odoo logs with their parameters (failed queries, `debug_sql`); the other log records are exported as
Odoo writes them, message and exception included.

**Personal data**: the spans hold the IP address of the client (`client.address`,
the proxy's one unless `proxy_mode` is set), its user agent, the id of the user
(`odoo.uid`) and the URL path of the routes without arguments (never the path of an unknown URL). They reach the
collector and its storage: set its retention accordingly.
