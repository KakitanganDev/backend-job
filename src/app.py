"""
FastAPI application for Kakitangan Leave Management System.
"""

from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Annotated, Optional, Literal

from fastapi import FastAPI, Depends, HTTPException, Query, APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from src.database import engine, get_db, Base
from src.models import LeaveType, LeaveStatus, LeaveDuration
from src.dependencies import get_current_employee, require_manager
from src import services

Base.metadata.create_all(bind=engine)


# ── Lifespan ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    db = next(get_db())
    try:
        services.seed_demo_data(db)
    finally:
        db.close()
    yield


# ── App / Router ───────────────────────────────────────────────────────────

app = FastAPI(
    title="Kakitangan Leave Management API",
    version="0.1.0",
    lifespan=lifespan,
)

router = APIRouter(prefix="/api/v1")


# ── Schemas ────────────────────────────────────────────────────────────────

class EmployeeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(description="Employee ID")
    name: str = Field(description="Employee name")
    email: str = Field(description="Employee email")
    department: str = Field(description="Employee department")
    manager_id: Optional[int] = Field(None, description="Manager employee ID, null for top-level")


class LeaveBalanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    leave_type: LeaveType = Field(description="Leave type")
    year: int = Field(description="Calendar year")
    total_days: float = Field(description="Total entitlement days")
    used_days: float = Field(description="Days consumed")
    remaining_days: float = Field(description="Days remaining (total - used)")


class EmployeeWithBalancesOut(BaseModel):
    employee: EmployeeOut = Field(description="Employee record")
    leave_balances: list[LeaveBalanceOut] = Field(description="Leave balances for the employee")


class LeaveRequestCreate(BaseModel):
    leave_type: LeaveType = Field(description="Leave type")
    start_date: date = Field(description="Leave start date (inclusive)")
    end_date: date = Field(description="Leave end date (inclusive)")
    duration: LeaveDuration = Field(LeaveDuration.FULL, description="Full day, first half, or second half")
    reason: Optional[str] = Field(None, description="Reason for leave")


class LeaveRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(description="Leave request ID")
    employee_id: int = Field(description="ID of the employee taking leave")
    leave_type: LeaveType = Field(description="Leave type")
    start_date: date = Field(description="Leave start date")
    end_date: date = Field(description="Leave end date")
    duration: str = Field(description="Full, first_half, or second_half")
    reason: Optional[str] = Field(None, description="Reason for leave")
    status: LeaveStatus = Field(description="Request status")
    reviewed_by: Optional[int] = Field(None, description="ID of manager who reviewed")
    reviewed_at: Optional[datetime] = Field(None, description="Timestamp of review")
    rejection_reason: Optional[str] = Field(None, description="Reason provided when rejecting")


class LeaveRequestReview(BaseModel):
    decision: Literal["approved", "rejected"] = Field(description="Review decision")
    rejection_reason: Optional[str] = Field(None, description="Reason for rejection")


class PaginatedLeaveRequests(BaseModel):
    items: list[LeaveRequestOut] = Field(description="List of leave requests")
    total: int = Field(description="Total number of matching records")
    page: int = Field(description="Current page number")
    page_size: int = Field(description="Records per page")


class PaginatedEmployees(BaseModel):
    items: list[EmployeeOut] = Field(description="List of employees")
    total: int = Field(description="Total number of matching records")
    page: int = Field(description="Current page number")
    page_size: int = Field(description="Records per page")


class HolidayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(description="Holiday ID")
    date: Annotated[date, Field(description="Holiday date")]
    name: str = Field(description="Holiday name")


class HolidayCreate(BaseModel):
    date: Annotated[date, Field(description="Holiday date")]
    name: str = Field(description="Holiday name")


class HolidayUpdate(BaseModel):
    date: Annotated[date, Field(description="Holiday date")]
    name: str = Field(description="Holiday name")


class PaginatedHolidays(BaseModel):
    items: list[HolidayOut] = Field(description="List of holidays")
    total: int = Field(description="Total number of matching records")
    page: int = Field(description="Current page number")
    page_size: int = Field(description="Records per page")


# ── Routes: Employees ─────────────────────────────────────────────────────

@router.get(
    "/employees",
    response_model=PaginatedEmployees,
    tags=["employees"],
)
def list_employees(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    caller_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    items, total = services.list_employees(db, caller_id=caller_id, page=page, page_size=page_size)
    return PaginatedEmployees(
        items=[EmployeeOut.model_validate(e) for e in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/employees/{employee_id}",
    response_model=EmployeeWithBalancesOut,
    tags=["employees"],
)
def get_employee(
    employee_id: int,
    caller_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    employee = services.get_employee(db, employee_id=employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    balances = services.get_leave_balances(db, employee_id=employee_id)
    return EmployeeWithBalancesOut(
        employee=EmployeeOut.model_validate(employee),
        leave_balances=[LeaveBalanceOut.model_validate(b) for b in balances],
    )


# ── Routes: Leave Requests ────────────────────────────────────────────────

@router.post(
    "/leave-requests",
    response_model=LeaveRequestOut,
    status_code=201,
    tags=["leave-requests"],
)
def create_leave_request(
    body: LeaveRequestCreate,
    employee_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    try:
        return services.create_leave_request(
            db,
            employee_id=employee_id,
            leave_type=body.leave_type,
            start_date=body.start_date,
            end_date=body.end_date,
            duration=body.duration,
            reason=body.reason,
        )
    except services.LeaveError as e:
        detail = str(e)
        if "Employee not found" in detail:
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=422, detail=detail)


@router.get(
    "/leave-requests",
    response_model=PaginatedLeaveRequests,
    tags=["leave-requests"],
)
def list_leave_requests(
    employee_id: Optional[int] = Query(None, description="Filter by specific direct report"),
    status: Optional[LeaveStatus] = Query(None, description="Filter by status"),
    leave_type: Optional[LeaveType] = Query(None, description="Filter by leave type"),
    from_date: Optional[date] = Query(None, description="Start of interval overlap filter"),
    to_date: Optional[date] = Query(None, description="End of interval overlap filter"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    caller_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    items, total = services.get_leave_requests(
        db,
        caller_id=caller_id,
        employee_id=employee_id,
        status=status,
        leave_type=leave_type,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
    )
    return PaginatedLeaveRequests(
        items=[LeaveRequestOut.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/leave-requests/{leave_request_id}",
    response_model=LeaveRequestOut,
    tags=["leave-requests"],
)
def get_leave_request(
    leave_request_id: int,
    caller_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    try:
        return services.get_leave_request(db, leave_request_id=leave_request_id, caller_id=caller_id)
    except services.LeaveError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post(
    "/leave-requests/{leave_request_id}/review",
    response_model=LeaveRequestOut,
    tags=["leave-requests"],
)
def review_leave_request(
    leave_request_id: int,
    body: LeaveRequestReview,
    reviewer_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    try:
        return services.review_leave_request(
            db,
            leave_request_id=leave_request_id,
            reviewer_id=reviewer_id,
            decision=body.decision,
            rejection_reason=body.rejection_reason,
        )
    except services.LeaveError as e:
        detail = str(e)
        if "not found" in detail.lower():
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=422, detail=detail)


@router.post(
    "/leave-requests/{leave_request_id}/cancel",
    response_model=LeaveRequestOut,
    tags=["leave-requests"],
)
def cancel_leave_request(
    leave_request_id: int,
    employee_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    try:
        return services.cancel_leave_request(
            db,
            leave_request_id=leave_request_id,
            employee_id=employee_id,
        )
    except services.LeaveError as e:
        detail = str(e)
        if "not found" in detail.lower():
            raise HTTPException(status_code=404, detail=detail)
        if "only cancel your own" in detail:
            raise HTTPException(status_code=403, detail=detail)
        raise HTTPException(status_code=422, detail=detail)


# ── Routes: Leave Balances ────────────────────────────────────────────────

@router.get(
    "/leave-balances/{employee_id}",
    response_model=list[LeaveBalanceOut],
    tags=["leave-balances"],
)
def get_leave_balances(
    employee_id: int,
    year: Optional[int] = Query(None, description="Calendar year, defaults to current year"),
    caller_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    employee = services.get_employee(db, employee_id=employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    balances = services.get_leave_balances(db, employee_id=employee_id, year=year)
    return [LeaveBalanceOut.model_validate(b) for b in balances]


# ── Routes: Holidays ──────────────────────────────────────────────────────

@router.get(
    "/holidays",
    response_model=PaginatedHolidays,
    tags=["holidays"],
)
def list_holidays(
    year: Optional[int] = Query(None, description="Filter by year"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    caller_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    items, total = services.list_holidays(db, year=year, page=page, page_size=page_size)
    return PaginatedHolidays(
        items=[HolidayOut.model_validate(h) for h in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/holidays",
    response_model=HolidayOut,
    status_code=201,
    tags=["holidays"],
)
def create_holiday(
    body: HolidayCreate,
    caller_id: int = Depends(require_manager),
    db: Session = Depends(get_db),
):
    try:
        return services.create_holiday(db, holiday_date=body.date, name=body.name, caller_id=caller_id)
    except services.LeaveError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.put(
    "/holidays/{holiday_id}",
    response_model=HolidayOut,
    tags=["holidays"],
)
def update_holiday(
    holiday_id: int,
    body: HolidayUpdate,
    caller_id: int = Depends(require_manager),
    db: Session = Depends(get_db),
):
    try:
        return services.update_holiday(db, holiday_id=holiday_id, holiday_date=body.date, name=body.name, caller_id=caller_id)
    except services.LeaveError as e:
        detail = str(e)
        if "not found" in detail.lower():
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=422, detail=detail)


@router.delete(
    "/holidays/{holiday_id}",
    status_code=204,
    tags=["holidays"],
)
def delete_holiday(
    holiday_id: int,
    caller_id: int = Depends(require_manager),
    db: Session = Depends(get_db),
):
    try:
        services.delete_holiday(db, holiday_id=holiday_id, caller_id=caller_id)
    except services.LeaveError as e:
        raise HTTPException(status_code=404, detail=str(e))


# Mount the router
app.include_router(router)
