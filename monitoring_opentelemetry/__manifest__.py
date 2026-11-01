{
    "name": "Monitoring OpenTelemetry",
    "summary": "Export Odoo traces, metrics and logs with OpenTelemetry (OTLP)",
    "version": "20.0.2.0.0",
    "category": "Extra Tools",
    "website": "https://github.com/Skilldron/odoo-addons",
    "author": "Skilldron",
    "license": "LGPL-3",
    "development_status": "Alpha",
    "depends": ["base"],
    "external_dependencies": {
        "python": [
            "opentelemetry-api==1.45.2",
            "opentelemetry-sdk==1.45.2",
            "opentelemetry-exporter-otlp-proto-http==1.45.2",
            "opentelemetry-instrumentation-logging==0.66b2",
        ],
    },
    "post_load": "post_load",
    "installable": True,
}
