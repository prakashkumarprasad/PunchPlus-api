"""Request bodies (Pydantic models) with every constraint the contract lists."""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.config import (
    DEFAULT_SHIFT_END, DEFAULT_SHIFT_START, MAX_EPOCH_MS, MIN_EPOCH_MS,
)
from app.timeutils import is_valid_date_string


class EmployeeCreate(BaseModel):
    """Body of POST /employees."""

    # Unknown fields are dropped, so a client can never set a computed value.
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default=DEFAULT_SHIFT_START, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default=DEFAULT_SHIFT_END, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_a_real_date(cls, value: str) -> str:
        """joined_on has to be YYYY-MM-DD and a day that exists."""
        if not is_valid_date_string(value):
            raise ValueError("joined_on must be a real date in YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def shift_times_must_differ(self):
        """The contract rejects a shift that starts and ends at the same time."""
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInBody(BaseModel):
    """Body of POST /attendance/punch-in."""

    model_config = ConfigDict(extra="ignore")

    emp_code: str
    # StrictInt refuses floats, strings and bools (bool is a subclass of int).
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS
    )
    # Only the three presence statuses may be created by a punch-in.
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutBody(BaseModel):
    """Body of POST /attendance/punch-out."""

    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS
    )


class RegularizeBody(BaseModel):
    """Body of PATCH /attendance/{emp_code}/{date}.

    `reason` and `regularized_by` are always required (they go in the audit
    trail). The three corrected fields are optional: omitted means "leave alone".
    """

    model_config = ConfigDict(extra="ignore")

    # A correction may create ABSENT and LEAVE, so all five statuses are allowed.
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
