from __future__ import annotations

import os
from pathlib import Path

from cases import write_visible_cases


def main() -> int:
    challenge_root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"]).resolve()
    written = write_visible_cases(challenge_root)
    print(f"generated {len(written)} diffusion challenge files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
