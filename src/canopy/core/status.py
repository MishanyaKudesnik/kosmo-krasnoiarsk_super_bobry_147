"""Machine-readable status codes.

The case statement is explicit: when data are insufficient the service reports
the reason instead of inventing a value.  That rule only holds if "no value" is
a first-class result, so every quantity the service can fail to produce carries
one of these codes together with a sentence a verifier can read.

Codes are part of the public API contract and are also what the UI keys its
warning banners off.  Nothing in the code path is allowed to return a bare
`None` for a number the user asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- request validation ----------------------------------------------------
OK = "OK"
INVALID_GEOMETRY = "INVALID_GEOMETRY"
AOI_TOO_LARGE = "AOI_TOO_LARGE"
AOI_TOO_SMALL = "AOI_TOO_SMALL"
INVALID_PERIOD = "INVALID_PERIOD"

# --- data availability -----------------------------------------------------
NO_BIOMASS_DATA = "NO_BIOMASS_DATA"
PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
NO_VALID_OBSERVATIONS = "NO_VALID_OBSERVATIONS"
SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"
OUT_OF_BASELINE_COVERAGE = "OUT_OF_BASELINE_COVERAGE"

# --- interpretation --------------------------------------------------------
CAUSE_UNDETERMINED = "CAUSE_UNDETERMINED"
SOURCES_CONFLICT = "SOURCES_CONFLICT"
NO_CHANGE_DETECTED = "NO_CHANGE_DETECTED"

# --- units -----------------------------------------------------------------
UNITS_UNAVAILABLE_PARTIAL_COVERAGE = "UNITS_UNAVAILABLE_PARTIAL_COVERAGE"
UNITS_UNAVAILABLE_NO_BASELINE = "UNITS_UNAVAILABLE_NO_BASELINE"
RESULT_NOT_ABOVE_BASELINE = "RESULT_NOT_ABOVE_BASELINE"
UNCERTAINTY_EXCEEDS_RESULT = "UNCERTAINTY_EXCEEDS_RESULT"

#: Human-readable explanation shown next to the code.  Russian, because the
#: service is presented to a Russian-speaking jury and the wording has to match
#: the case statement's own vocabulary.
MESSAGES: Dict[str, str] = {
    OK: "Расчёт выполнен",
    INVALID_GEOMETRY: "Геометрия запроса некорректна",
    AOI_TOO_LARGE: "Площадь контура превышает допустимые 20 км²",
    AOI_TOO_SMALL: "Контур слишком мал: он не пересекает ни одного пикселя биомассы",
    INVALID_PERIOD: "Конечный год должен быть больше начального",
    NO_BIOMASS_DATA: "Для этого контура нет данных о биомассе — расчёт запаса недоступен",
    PARTIAL_COVERAGE: "Данные покрывают контур не полностью; показаны рассчитанная площадь и пропуски",
    NO_VALID_OBSERVATIONS: "В этом году нет пригодных спутниковых наблюдений на контуре",
    SOURCE_UNREACHABLE: "Внешний источник недоступен; использован сохранённый набор",
    OUT_OF_BASELINE_COVERAGE: "Контур вне покрытия таблицы базовой линии",
    CAUSE_UNDETERMINED: "Причина не установлена: подтверждений недостаточно",
    SOURCES_CONFLICT: "Источники дают несовместимые окна дат; вывод не делается",
    NO_CHANGE_DETECTED: "Значимых изменений не обнаружено",
    UNITS_UNAVAILABLE_PARTIAL_COVERAGE: (
        "При неполном покрытии число потенциальных единиц не рассчитывается "
        "(условие кейса)"
    ),
    UNITS_UNAVAILABLE_NO_BASELINE: "Нет строки базовой линии для этого контура и периода",
    RESULT_NOT_ABOVE_BASELINE: "Результат не лучше базовой линии: R ≤ 0, поэтому Q = 0",
    UNCERTAINTY_EXCEEDS_RESULT: "Ширина диапазона не меньше результата: H/R ≥ 1, поэтому Q = 0",
}


@dataclass
class Status:
    """A code plus everything needed to explain it."""

    code: str = OK
    detail: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == OK

    @property
    def message(self) -> str:
        return self.detail or MESSAGES.get(self.code, self.code)

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}


@dataclass
class Quantity:
    """A number the service either produced or explained the absence of.

    `value is None` always comes with a status code that is not OK, so the UI
    can render a reason where a number would have been and never a blank.
    """

    name: str
    value: Optional[float] = None
    unit: str = ""
    status: Status = field(default_factory=Status)
    pool: Optional[str] = None
    node_id: Optional[str] = None
    sign_convention: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.value is not None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "available": self.available,
            "status": self.status.as_dict(),
        }
        if self.pool:
            out["pool"] = self.pool
        if self.node_id:
            out["node_id"] = self.node_id
        if self.sign_convention:
            out["sign_convention"] = self.sign_convention
        if self.notes:
            out["notes"] = self.notes
        return out


def unavailable(name: str, code: str, unit: str = "", **context) -> Quantity:
    return Quantity(name=name, value=None, unit=unit, status=Status(code, context=context))


class RequestError(ValueError):
    """Raised for a request that cannot be accepted at all."""

    def __init__(self, code: str, detail: Optional[str] = None, **context):
        self.status = Status(code, detail, context)
        super().__init__(self.status.message)
