# Vendored Muteki source

This backend contains an isolated copy of the upstream Muteki Python package
from:

<https://github.com/FishCodeTech/muteki>

- Upstream version: `0.2.5`
- Source commit: `a585a2943a15efc4de78b00a385335fc9b1f23f1`
- Local package: `backend/muteki/`
- License: GNU Affero General Public License v3.0, preserved at
  `backend/muteki/LICENSE`

The vendored package is not yet the production Solver entry point.  The
current project adapters remain under `backend/app/solver/muteki/` while the
upstream graph, reasoner, gate, and worker contracts are migrated in isolated
steps.
