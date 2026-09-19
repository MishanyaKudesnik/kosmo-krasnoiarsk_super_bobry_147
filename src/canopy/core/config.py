"""Configuration loading.

Every threshold, path, limit and assumption lives in `config/*.json` and is read
at start-up.  The rule the repository enforces is that `src/` contains no area
identifier, no year and no path from a developer's machine: a new contour and a
new period must work without touching code.  `tests/test_no_hardcode.py` greps
for violations.

Carbon coefficients are deliberately *not* duplicated here.  CF, 44/12, the
uncertainty allowance, the buffer and the price scenarios are read from
`data/methodology/parameters.csv`, which is part of the case, so there is only
one source of truth for them.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional


def project_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def _read_json(name: str) -> Dict[str, Any]:
    path = os.path.join(project_root(), "config", name)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class Parameter:
    name: str
    value: str
    unit: str
    kind: str
    source_id: str
    locator: str
    applicability: str

    def as_float(self) -> float:
        v = self.value.strip()
        if "/" in v:
            num, den = v.split("/", 1)
            return float(num) / float(den)
        return float(v.replace(",", "."))


@dataclass
class Settings:
    methods: Dict[str, Any] = field(default_factory=dict)
    limits: Dict[str, Any] = field(default_factory=dict)
    sources: Dict[str, Any] = field(default_factory=dict)
    parameters: Dict[str, Parameter] = field(default_factory=dict)

    # -- case parameters, always from parameters.csv ----------------------
    def param(self, name: str) -> Parameter:
        if name not in self.parameters:
            raise KeyError(
                f"parameter {name!r} is missing from data/methodology/parameters.csv"
            )
        return self.parameters[name]

    def pvalue(self, name: str) -> float:
        return self.param(name).as_float()

    @property
    def cf_agb(self) -> float:
        return self.pvalue("CF_AGB")

    @property
    def co2_per_c(self) -> float:
        return self.pvalue("CO2_per_C")

    @property
    def unc_allowance(self) -> float:
        return self.pvalue("UNC_allowance")

    @property
    def unc_stop_ratio(self) -> float:
        return self.pvalue("UNC_stop_ratio")

    @property
    def buffer_share(self) -> float:
        return self.pvalue("BUF")

    @property
    def leakage(self) -> float:
        return self.pvalue("LK")

    @property
    def prices(self) -> List[Dict[str, Any]]:
        out = []
        for key, label in (
            ("price_low", "низкий"),
            ("price_base", "базовый"),
            ("price_high", "высокий"),
        ):
            if key in self.parameters:
                out.append(
                    {
                        "id": key,
                        "label": label,
                        "value": self.pvalue(key),
                        "unit": self.parameters[key].unit,
                    }
                )
        return out

    # -- paths -------------------------------------------------------------
    def data_root(self) -> str:
        return os.path.join(project_root(), self.sources.get("data_root", "data"))

    def fixture_root(self) -> str:
        return os.path.join(project_root(), self.sources.get("fixture_root", "data_fixtures"))

    def runs_root(self) -> str:
        return os.path.join(project_root(), "runs")

    def methodology_path(self, name: str) -> str:
        return os.path.join(self.data_root(), "methodology", name)

    def data_path(self, name: str) -> str:
        return os.path.join(self.data_root(), name)


def _load_parameters(path: str) -> Dict[str, Parameter]:
    params: Dict[str, Parameter] = {}
    if not os.path.exists(path):
        return params
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("parameter") or "").strip()
            if not name:
                continue
            params[name] = Parameter(
                name=name,
                value=(row.get("value") or "").strip(),
                unit=(row.get("unit") or "").strip(),
                kind=(row.get("kind") or "").strip(),
                source_id=(row.get("source_id") or "").strip(),
                locator=(row.get("locator") or "").strip(),
                applicability=(row.get("applicability") or "").strip(),
            )
    return params


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings(
        methods=_read_json("methods.json"),
        limits=_read_json("limits.json"),
        sources=_read_json("sources.json"),
    )
    settings.parameters = _load_parameters(
        settings.methodology_path("parameters.csv")
    )
    return settings


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
