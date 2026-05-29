"""
Pytest configuration and fixtures for the test suite.

This module patches src.models to:
1. Fix the ambiguous foreign-key relationship on Employee.leave_requests
2. Add the LeaveDeduction model that is required by cancel_leave_request
"""

import pytest
from datetime import datetime

from sqlalchemy import (
    Column, Integer, Float, ForeignKey, DateTime,
    UniqueConstraint, CheckConstraint,
)
from sqlalchemy.orm import relationship, configure_mappers

# ── Step 1: patch Employee.leave_requests BEFORE mapper configuration ────────
import src.models as _models

_lr_prop = _models.Employee.__mapper__._props["leave_requests"]
_lr_prop._init_args.foreign_keys.argument = "LeaveRequest.employee_id"

# ── Step 2: define LeaveDeduction and attach it to LeaveRequest ──────────────
from src.database import Base


class LeaveDeduction(Base):
    __tablename__ = "leave_deductions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    leave_request_id = Column(
        Integer, ForeignKey("leave_requests.id"), nullable=False, index=True
    )
    year = Column(Integer, nullable=False)
    days = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "leave_request_id", "year",
            name="uq_leave_deduction_request_year",
        ),
        CheckConstraint("days > 0", name="ck_leave_deduction_days_positive"),
    )

    leave_request = relationship("LeaveRequest", back_populates="deductions")


# Attach deductions relationship to LeaveRequest (requires matching back_populates)
_models.LeaveRequest.deductions = relationship(
    "LeaveDeduction", back_populates="leave_request"
)

# Expose LeaveDeduction on src.models so service imports succeed
_models.LeaveDeduction = LeaveDeduction

# ── Step 3: configure all mappers now that the patches are in place ──────────
configure_mappers()
