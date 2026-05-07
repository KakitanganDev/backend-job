import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.orm import relationship

from src.database import Base


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
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    manager = relationship("Employee", remote_side="Employee.id")
    leave_requests = relationship(
        "LeaveRequest", back_populates="employee", foreign_keys="LeaveRequest.employee_id"
    )
    leave_balances = relationship("LeaveBalance", back_populates="employee")


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(SqlEnum(LeaveType), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    # Half-day flags. start_half_day=True means the start date counts as 0.5,
    # end_half_day=True means the end date counts as 0.5. For single-day
    # leaves, both flags refer to the same day; setting either yields 0.5.
    start_half_day = Column(Boolean, nullable=False, default=False, server_default="0")
    end_half_day = Column(Boolean, nullable=False, default=False, server_default="0")
    # Snapshot of the working-day count computed at submission time.
    # Stored so that retroactive changes to the holiday calendar do not
    # silently change historical balance accounting.
    working_days = Column(Float, nullable=False, default=0)
    reason = Column(String, nullable=True)
    status = Column(SqlEnum(LeaveStatus), default=LeaveStatus.PENDING, nullable=False, index=True)
    approved_by = Column(Integer, ForeignKey("employees.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    employee = relationship("Employee", back_populates="leave_requests", foreign_keys=[employee_id])
    approver = relationship("Employee", foreign_keys=[approved_by])

    __table_args__ = (
        CheckConstraint("start_date <= end_date", name="ck_leave_dates_order"),
        CheckConstraint("working_days >= 0", name="ck_leave_working_days_nonneg"),
        Index("ix_leave_requests_emp_dates", "employee_id", "start_date", "end_date"),
    )


class LeaveBalance(Base):
    __tablename__ = "leave_balances"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(SqlEnum(LeaveType), nullable=False)
    year = Column(Integer, nullable=False)
    total_days = Column(Float, nullable=False, default=0)
    used_days = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    employee = relationship("Employee", back_populates="leave_balances")

    __table_args__ = (
        UniqueConstraint("employee_id", "leave_type", "year", name="uq_balance_emp_type_year"),
        CheckConstraint("used_days >= 0", name="ck_balance_used_nonneg"),
        CheckConstraint("total_days >= 0", name="ck_balance_total_nonneg"),
    )

    @property
    def remaining_days(self) -> float:
        return self.total_days - self.used_days
