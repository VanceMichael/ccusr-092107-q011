"""从 fixtures 目录加载全部编排资料。"""

from __future__ import annotations

import json
from pathlib import Path

from .catalog import ServiceCatalog
from .members import MemberRegistry
from .money import FX


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_workspace(fixtures_dir: str | Path) -> dict:
    root = Path(fixtures_dir)
    team = _read(root / "team.json")
    credentials = _read(root / "credentials.json")
    catalog = ServiceCatalog(_read(root / "city_services.json"))
    fx_data = _read(root / "fx_rates.json")
    fx = FX(fx_data["rates"], base=fx_data["base_currency"], as_of=fx_data.get("as_of", ""))
    policies = _read(root / "policies.json")
    registry = MemberRegistry(team, credentials)
    return {
        "team": team,
        "registry": registry,
        "catalog": catalog,
        "fx": fx,
        "policies": policies,
    }
