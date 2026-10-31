# Skilldron Odoo addons

[![Pre-commit Status](https://github.com/Skilldron/odoo-addons/actions/workflows/pre-commit.yml/badge.svg?branch=20.0)](https://github.com/Skilldron/odoo-addons/actions/workflows/pre-commit.yml?query=branch%3A20.0)
[![Build Status](https://github.com/Skilldron/odoo-addons/actions/workflows/test.yml/badge.svg?branch=20.0)](https://github.com/Skilldron/odoo-addons/actions/workflows/test.yml?query=branch%3A20.0)

<!-- /!\ do not modify above this line -->

Odoo modules of Skilldron. One branch per Odoo version (`20.0`,
…), structured like an OCA repository: each module is installed with pip
(`odoo-addon-<module>`).

| Module                     | Role                                                                  |
| -------------------------- | --------------------------------------------------------------------- |
| `monitoring_opentelemetry` | OpenTelemetry traces and logs over OTLP, server-wide module           |

<!-- /!\ do not modify below this line -->

<!-- prettier-ignore-start -->

[//]: # (addons)

This part will be replaced when running the oca-gen-addons-table script from OCA/maintainer-tools.

[//]: # (end addons)

<!-- prettier-ignore-end -->

## Licenses

This repository is licensed under [LGPL-3.0](LICENSE).

However, each module can have a different license. Consult each module's
`__manifest__.py` file, which contains a `license` key that explains its license.
