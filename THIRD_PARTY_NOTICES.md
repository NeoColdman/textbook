# Third-party notices

## tchMaterial-parser

This project adapts the SmartEdu catalog, resource parsing, MAC request signing,
download handling and PDF bookmark algorithms from:

- Source: https://github.com/happycola233/tchMaterial-parser
- Baseline: `244170e17bf8bc97401f65e04d261533d252572a` (v4.4)
- Copyright (c) 2026 肥宅水水呀
- License: MIT; the complete original text is in
  [licenses/tchMaterial-parser-MIT.txt](licenses/tchMaterial-parser-MIT.txt).

The adapted implementation separates platform access from a new terminal UI,
adds typed models, credential backends, caching, file validation, output locking
and a persistent completed-file manifest. No upstream desktop images, fonts,
Microsoft Fluent Emoji assets or textbook content are included.

## Runtime dependencies

The package uses Typer (MIT), prompt_toolkit (BSD-3-Clause), Rich (MIT), Requests
(Apache-2.0), platformdirs (MIT), keyring (MIT), filelock (Unlicense), and pypdf
(BSD-3-Clause). Their distributions retain their own license texts. Exact
resolved versions are recorded in `uv.lock`; inspect those distributions when
redistributing bundled standalone applications. Python's standard library and
its SQLite runtime retain their respective upstream licenses.

Software licenses do not grant rights to redistribute textbooks downloaded
from the platform.
