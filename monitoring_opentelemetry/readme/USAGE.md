Once Odoo restarts with an endpoint set, it logs `OpenTelemetry is enabled` (or
`is enabled in each worker` with `workers` > 0) and exports:

**Spans**

| Span | Kind | Attributes |
| --- | --- | --- |
| `{METHOD} {route}`, e.g. `POST /web/dataset/call_kw/<path:path>`; `{METHOD}` alone when no route matches | `SERVER` | `http.request.method` (`http.request.method_original` for an unknown method), `http.route`, `http.response.status_code`, `url.path` (not for a route with arguments), `url.scheme`, `server.address`, `server.port`, `network.protocol.version`, `client.address`, `user_agent.original`, `odoo.db`, `odoo.uid`, `odoo.route_type`, `odoo.sql.count`, `odoo.sql.duration_ms` |
| `{model}.{method}`, e.g. `res.partner.web_search_read` | `INTERNAL` | `odoo.model`, `odoo.method` (`_OTHER` for an unknown or private method) |
| `cron {name}` | `INTERNAL`, root of its trace (child of the request span when run manually) | `odoo.cron.id`, `odoo.cron.name`, `odoo.cron.status`, `odoo.cron.timed_out`, `odoo.db`, `odoo.sql.*` |
| `{OPERATION} {table}`, e.g. `SELECT res_partner` | `CLIENT` | `db.system.name`, `db.namespace`, `db.operation.name`, `db.collection.name`, `db.query.text` |

An error sets the span status to `ERROR` with `error.type` and an `exception`
event: the first line of the message, truncated to 300 characters and as raised (it
may hold a value), and a stack trace without the local variables. A
user error (`UserError`, `AccessError`, `ValidationError`, a 4xx HTTP error...) is
not a failure of Odoo: it only sets `odoo.exception.type`.

An incoming `traceparent` header continues the trace of the caller (a reverse
proxy, another service).

**Logs**: each record has its level, its logger (the instrumentation scope), its
source (`code.file.path`, `code.function.name`, `code.line.number`), the trace and
span ids of the current span, and the database (`odoo.db`).

**Resource**: `service.name`, `service.version` and `odoo.version` (the Odoo
version), `service.instance.id` (the host name, the pod name in Kubernetes),
`odoo.process.type` (`http`, `cron` or `threaded`), `process.pid`.

**Never in the spans**: the query string, the cookies, the headers other than the
user agent, the request and response bodies, the SQL parameters and the literal
values of the queries. The logs never include the HTTP access logs nor the failed
queries that Odoo logs with their parameters; the other log records are exported as
Odoo writes them, message and exception included.

**Personal data**: the spans hold the IP address of the client (`client.address`,
the proxy's one unless `proxy_mode` is set), its user agent, the id of the user
(`odoo.uid`) and the URL path of the routes without arguments. They reach the
collector and its storage: set its retention accordingly.
