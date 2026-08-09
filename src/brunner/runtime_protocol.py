from __future__ import annotations

import json
from pathlib import Path

from brunner import BRUNNER_RUNTIME_PROTOCOL, __version__
from brunner.errors import IntegrityError


def runtime_identity() -> dict[str, str]:
    return {
        "protocol": BRUNNER_RUNTIME_PROTOCOL,
        "version": __version__,
    }


def validate_trial_runtime(trial: Path) -> None:
    paths = (
        trial / "metadata/manifest.json",
        trial / "metadata/agent-run.json",
    )
    for path in paths:
        try:
            value = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as error:
            raise IntegrityError(
                f"cannot read staged Brunner runtime identity: {path}: {error}"
            ) from error
        protocol = value.get("brunner_runtime_protocol")
        if protocol != BRUNNER_RUNTIME_PROTOCOL:
            raise IntegrityError(
                "staged Brunner runtime protocol is incompatible with the "
                f"installed runtime: {protocol!r} != "
                f"{BRUNNER_RUNTIME_PROTOCOL!r}"
            )
