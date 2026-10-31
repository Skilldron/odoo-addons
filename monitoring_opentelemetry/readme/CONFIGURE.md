Load the module server-wide, in the Odoo configuration file:

``` ini
server_wide_modules = base,web,monitoring_opentelemetry
```

The configuration is read from environment variables only (`OTEL_*`); they are
documented with the instrumentation.
