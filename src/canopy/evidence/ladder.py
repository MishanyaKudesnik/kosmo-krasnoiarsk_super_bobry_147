"""What each source is allowed to say, and the ladder that names a cause.

Two ideas, both graded by the case.

**Competence matrix.**  A source votes only on the questions its physics can
answer.  MODIS MCD64A1 sits on a 463 m grid; on RU_MORDOVIA_04 it marks 88 of
88 cells inside the contour, which is 1889 ha against an area of 1832.7 ha --
103.1 % of the site.  A number above 100 % is the clearest possible proof that
this product must never supply geometry or area.  It supplies time and the fact
of burning; area comes from Sentinel-2 or GFC; tonnes come only from the biomass
product.  The rule is enforced in code and guarded by a test.

**Attribution ladder.**  Five deterministic rungs, no model and no probability.
A patch carries its rung together with the conditions that failed for the next
one, so "cause not established" is an answer with a reason rather than a blank.

The distinction between rung C and rung D is the one most solutions miss: "a
source looked and saw nothing" refutes, "a source could not look" does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..core.config import get_settings

# --- what each product may support ----------------------------------------
COMPETENCE: Dict[str, Dict[str, bool]] = {
    "CCI_AGB": {"fact": True, "geometry": True, "area": True, "date": False, "cause": False, "tonnes": True},
    "S2_REFLECTANCE": {"fact": True, "geometry": True, "area": True, "date": True, "cause": False, "tonnes": False},
    "GFC": {"fact": True, "geometry": True, "area": True, "date": True, "cause": False, "tonnes": False},
    "MODIS_BURN": {"fact": True, "geometry": False, "area": False, "date": True, "cause": True, "tonnes": False},
    "REGIONAL_REPORT": {"fact": False, "geometry": False, "area": False, "date": True, "cause": False, "tonnes": False},
}

COMPETENCE_LABELS = {
    "fact": "факт изменения",
    "geometry": "геометрия",
    "area": "площадь",
    "date": "дата",
    "cause": "причина",
    "tonnes": "величина в тоннах",
}

SOURCE_TITLES = {
    "CCI_AGB": "ESA CCI Biomass",
    "S2_REFLECTANCE": "Sentinel-2 L2A",
    "GFC": "Hansen Global Forest Change",
    "MODIS_BURN": "MODIS MCD64A1",
    "REGIONAL_REPORT": "Сообщение о событии в регионе",
}

RUNGS = {
    "A1": "Нарушение подтверждено, признаки согласуются с горением",
    "A2": "Нарушение подтверждено, признаки согласуются с потерей древесного покрова",
    "B": "Нарушение подтверждено, причина не установлена",
    "C": "Изменение в одном источнике, не подтверждено",
    "D": "Данных недостаточно для суждения",
}

CAUSE_TEXT = {
    "A1": "признаки согласуются с горением (подтверждено продуктом гарей)",
    "A2": "признаки согласуются с потерей древесного покрова",
    "B": "причина не установлена",
    "C": "причина не установлена",
    "D": "причина не установлена",
}


class CompetenceViolation(RuntimeError):
    """Raised when code tries to take a claim a source may not support."""


def may_support(product: str, claim: str) -> bool:
    return bool(COMPETENCE.get(product, {}).get(claim, False))


def require_competence(product: str, claim: str) -> None:
    if not may_support(product, claim):
        raise CompetenceViolation(
            f"{SOURCE_TITLES.get(product, product)} не может обосновывать "
            f"«{COMPETENCE_LABELS.get(claim, claim)}»"
        )


@dataclass
class Evidence:
    """One observation offered in support of one claim."""

    source_id: str
    product: str
    title: str
    claim: str
    observed: bool
    detail: str
    file: Optional[str] = None
    sha256: Optional[str] = None
    observation_time: Optional[str] = None
    spatial_support_m: Optional[float] = None
    overlap_fraction: Optional[float] = None
    date_window: Optional[Dict[str, object]] = None
    limitations: str = ""
    can_support: List[str] = field(default_factory=list)
    human: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "source_id": self.source_id,
            "product": self.product,
            "title": self.title,
            "claim": self.claim,
            "observed": self.observed,
            "detail": self.detail,
            "human": self.human or self.detail,
            "file": self.file,
            "sha256": self.sha256,
            "observation_time": self.observation_time,
            "spatial_support_m": self.spatial_support_m,
            "overlap_fraction": self.overlap_fraction,
            "date_window": self.date_window,
            "limitations": self.limitations,
            "can_support": self.can_support
            or [COMPETENCE_LABELS[k] for k, v in COMPETENCE.get(self.product, {}).items() if v],
        }


@dataclass
class LadderVerdict:
    rung: str
    label: str
    cause: str
    satisfied: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "rung": self.rung,
            "label": self.label,
            "cause": self.cause,
            "satisfied": self.satisfied,
            "missing": self.missing,
        }


def evaluate_ladder(
    cci_change: bool,
    s2_change: Optional[bool],
    gfc_loss: Optional[bool],
    modis_burn: Optional[bool],
    windows_consistent: bool,
    kind: str = "loss",
) -> LadderVerdict:
    """Apply the rules.

    `None` means "the source could not look"; `False` means "it looked and saw
    nothing".  That difference is what separates rung C from rung D.
    """
    satisfied: List[str] = []
    missing: List[str] = []

    if cci_change:
        satisfied.append("падение оценки биомассы по CCI" if kind == "loss" else "рост оценки биомассы по CCI")
    else:
        missing.append("изменение по CCI")

    if s2_change is True:
        satisfied.append("спектральное изменение по Sentinel-2")
    elif s2_change is False:
        missing.append("Sentinel-2 наблюдал и изменения не подтвердил")
    else:
        missing.append("Sentinel-2 не смог наблюдать очаг (облачность или нет сцен)")

    if gfc_loss is True:
        satisfied.append("потеря древесного покрова по GFC в пределах периода")
    elif gfc_loss is False:
        missing.append("GFC не отметил потерю покрова в этом периоде")
    else:
        missing.append("GFC недоступен для этого контура")

    if modis_burn is True:
        satisfied.append("признак горения по MODIS")
    elif modis_burn is False:
        missing.append("MODIS наблюдал и гари не отметил")
    else:
        missing.append("продукт гарей недоступен для этого контура")

    if not windows_consistent:
        missing.append("окна дат источников не пересекаются")

    confirming = sum(
        1 for x in (cci_change, s2_change is True, gfc_loss is True, modis_burn is True) if x
    )

    if (
        cci_change
        and s2_change is True
        and modis_burn is True
        and windows_consistent
    ):
        rung = "A1"
    elif (
        cci_change
        and s2_change is True
        and gfc_loss is True
        and modis_burn is not True
        and windows_consistent
    ):
        rung = "A2"
    elif confirming >= 2:
        rung = "B"
    elif confirming == 1 and (s2_change is False or gfc_loss is False or modis_burn is False):
        rung = "C"
    elif confirming == 1:
        rung = "D"
    else:
        rung = "D"

    return LadderVerdict(rung=rung, label=RUNGS[rung], cause=CAUSE_TEXT[rung], satisfied=satisfied, missing=missing)


def competence_table() -> List[Dict[str, object]]:
    """The matrix, rendered for the interface and the report."""
    rows = []
    for product, caps in COMPETENCE.items():
        rows.append(
            {
                "product": product,
                "title": SOURCE_TITLES.get(product, product),
                "capabilities": {COMPETENCE_LABELS[k]: v for k, v in caps.items()},
            }
        )
    return rows


def modis_area_guard(enabled: Optional[bool] = None) -> bool:
    """True when the configuration still forbids taking area from MODIS."""
    if enabled is None:
        enabled = get_settings().methods["evidence"].get("modis_may_provide_area", False)
    return not enabled
