import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from src.database import Base


class LeaveDuration(str, enum.Enum):
    FULL = "full"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"


class LeaveStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class LeaveType(str, enum.Enum):
    ANNUAL = "annual"
    SICK = "sick"
    PERSONAL = "personal"
    MATERNITY = "maternity"
    PATERNITY = "paternity"
    UNPAID = "unpaid"


class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    department = Column(String, nullable=False)
    manager_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    joined_at = Column(Date, default=date.today)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    manager = relationship("Employee", remote_side="Employee.id")
    leave_requests = relationship(
        "LeaveRequest",
        back_populates="employee",
        foreign_keys="LeaveRequest.employee_id",
    )
    leave_balances = relationship("LeaveBalance", back_populates="employee")


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    __table_args__ = (
        Index(
            "ix_leave_requests_employee_status_dates",
            "employee_id", "status", "start_date", "end_date",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(
        Integer, ForeignKey("employees.id"), nullable=False, index=True
    )
    leave_type = Column(String, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    duration = Column(String, nullable=False, default=LeaveDuration.FULL)
    reason = Column(String, nullable=True)
    status = Column(String, nullable=False, default=LeaveStatus.PENDING)
    reviewed_by = Column(Integer, ForeignKey("employees.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    employee = relationship(
        "Employee", back_populates="leave_requests", foreign_keys=[employee_id]
    )
    reviewer = relationship("Employee", foreign_keys=[reviewed_by])


class LeaveBalance(Base):
    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint("employee_id", "leave_type", "year", name="uq_balance"),
    )

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(
        Integer, ForeignKey("employees.id"), nullable=False, index=True
    )
    leave_type = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    total_days = Column(Float, nullable=False, default=0)
    used_days = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    employee = relationship("Employee", back_populates="leave_balances")

    @property
    def remaining_days(self) -> float:
        return self.total_days - self.used_days


class PublicHoliday(Base):
    __tablename__ = "public_holidays"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
