from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def require_project_subpath(value: str, allowed_directory: str, label: str) -> Path:
    """Reject connector paths that escape the intended local secret/data directory."""
    candidate = Path(value).expanduser().resolve()
    allowed = (PROJECT_ROOT / allowed_directory).resolve()
    try:
        candidate.relative_to(allowed)
    except ValueError as exc:
        raise ValueError(f"{label} must be stored under {allowed_directory}/.") from exc
    return candidate
