Install the Python package of the module, with its pinned OpenTelemetry
dependencies:

``` shell
pip install "odoo-addon-monitoring_opentelemetry @ git+https://github.com/Skilldron/odoo-addons@20.0#subdirectory=monitoring_opentelemetry"
```

The package depends on the `odoo` distribution, which the official Odoo image
provides (`odoo 20.0.postYYYYMMDD`). When Odoo runs from a source checkout that pip
does not know, install it with `--no-deps` and install the packages listed in
`external_dependencies` of the manifest.

The module is loaded server-wide (see Configuration); installing it in a database
is not needed.
