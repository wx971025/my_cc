from pathlib import Path

from configs import WORKDIR

def safe_path(p: Path | str) -> Path:
    if isinstance(p, str):
        p = Path(p)
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
