# SPDX-License-Identifier: Apache-2.0
"""foundry -- launch coding-agent CLIs against a Microsoft Foundry endpoint.

Nothing is imported here on purpose: ``foundry.auth`` shells out to ``az`` and
``foundry.agents`` touches the filesystem, so importing the package must stay
free of side effects and cheap enough for shell completion.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

#: Fallback for a source checkout that was never installed (``python -m foundry``
#: straight out of ``src/``), where the distribution metadata does not exist.
_FALLBACK_VERSION = "0.1.0"

try:
    __version__: str = _version("foundry-cli")
except PackageNotFoundError:  # pragma: no cover - only hit in an uninstalled tree
    __version__ = _FALLBACK_VERSION

__all__ = ["__version__"]
