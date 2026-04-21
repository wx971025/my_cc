from pathlib import Path

from configs import WORKDIR

def safe_path(p: Path | str) -> Path:
    if isinstance(p, str):
        p = Path(p)
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def safe_path_at(p: Path | str, base: Path | None = None) -> Path:
    """
    基于指定 base 目录解析相对路径，再做 WORKDIR 沙箱校验。

    - base=None 时等价于 safe_path（兼容老调用方）。
    - base 必须位于 WORKDIR 之下（例如 .worktrees/t7-alice）。
    - 绝对路径按原值解析；相对路径相对 base 解析。
    - 最终必须落在 WORKDIR 之内，否则抛 ValueError。
    """
    if isinstance(p, str):
        p = Path(p)

    root = base if base is not None else WORKDIR
    if p.is_absolute():
        path = p.resolve()
    else:
        path = (root / p).resolve()

    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
