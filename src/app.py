"""
FastAPI application for Kakitangan Leave Management System.

Auth note: this scaffold has no real authentication. The actor is
identified by an `X-Employee-Id` header on every mutating endpoint —
see DESIGN.md §4. In production this would be replaced by a JWT or
session cookie populated by an auth middleware.
"""

from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from src import services
from src.database import Base, engine, get_db
from src.models import Employee, LeaveRequest, LeaveStatus, LeaveType
from src.observability import (
    RequestIdMiddleware,
    configure_logging,
    get_logger,
    setup_otel,
)

configure_logging()

Base.metadata.create_all(bind=engine)

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = next(get_db())
    try:
        services.seed_demo_data(db)
    finally:
        db.close()
    log.info("app_started", version=app.version)
    yield


app = FastAPI(
    title="Kakitangan Leave Management API",
    version="0.2.0",
    lifespan=lifespan,
)
app.add_middleware(RequestIdMiddleware)
setup_otel(app, engine)


# ── Auth dependency ──────────────────────────────────────────────────────

def current_user_id(
    x_employee_id: int | None = Header(default=None, alias="X-Employee-Id"),
) -> int:
    """Identify the acting user via X-Employee-Id header.

    Stub auth — see DESIGN.md §4. Production would derive this from a
    verified JWT or session.
    """
    if x_employee_id is None:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Employee-Id header",
        )
    return x_employee_id


# ── Schemas ──────────────────────────────────────────────────────────────

class EmployeeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    department: str
    manager_id: int | None = None


class LeaveBalanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    leave_type: LeaveType
    year: int
    total_days: float
    used_days: float
    remaining_days: float


class LeaveRequestCreate(BaseModel):
    employee_id: int
    leave_type: LeaveType
    start_date: date
    end_date: date
    reason: str | None = None
    start_half_day: bool = False
    end_half_day: bool = False


class LeaveRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    leave_type: LeaveType
    start_date: date
    end_date: date
    start_half_day: bool
    end_half_day: bool
    working_days: float
    reason: str | None
    status: LeaveStatus
    approved_by: int | None
    approved_at: datetime | None


class LeaveRequestReview(BaseModel):
    decision: LeaveStatus = Field(description="approved or rejected")


class PaginatedLeaveRequests(BaseModel):
    items: list[LeaveRequestOut]
    total: int
    page: int
    page_size: int


# ── Error mapping ────────────────────────────────────────────────────────

def _map_leave_error(exc: services.LeaveError) -> HTTPException:
    """Map domain errors to HTTP statuses with consistent semantics."""
    if isinstance(exc, (services.EmployeeNotFoundError, services.LeaveRequestNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (services.SelfApprovalError, services.NotAuthorizedError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, services.CannotModifyApprovedLeaveError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


# ── Routes ───────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/employees", response_model=list[EmployeeOut])
def list_employees(db: Session = Depends(get_db)):
    return db.query(Employee).all()


@app.get("/employees/{employee_id}")
def get_employee(employee_id: int, db: Session = Depends(get_db)):
    emp = db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    balances = services.get_leave_balances(db, employee_id)
    return {
        "employee": EmployeeOut.model_validate(emp),
        "leave_balances": [LeaveBalanceOut.model_validate(b) for b in balances],
    }


@app.post("/leave-requests", response_model=LeaveRequestOut, status_code=201)
def create_leave_request(
    body: LeaveRequestCreate,
    actor_id: int = Depends(current_user_id),
    db: Session = Depends(get_db),
):
    if body.employee_id != actor_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot submit leave on behalf of another employee",
        )
    try:
        return services.create_leave_request(
            db,
            employee_id=body.employee_id,
            leave_type=body.leave_type,
            start_date=body.start_date,
            end_date=body.end_date,
            reason=body.reason,
            start_half_day=body.start_half_day,
            end_half_day=body.end_half_day,
        )
    except services.LeaveError as e:
        raise _map_leave_error(e) from e


@app.get("/leave-requests", response_model=PaginatedLeaveRequests)
def list_leave_requests(
    employee_id: int | None = Query(None),
    status: LeaveStatus | None = Query(None),
    leave_type: LeaveType | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    try:
        items, total = services.get_leave_requests(
            db,
            employee_id=employee_id,
            status=status,
            leave_type=leave_type,
            from_date=from_date,
            to_date=to_date,
            page=page,
            page_size=page_size,
        )
    except services.LeaveError as e:
        raise _map_leave_error(e) from e
    return PaginatedLeaveRequests(
        items=[LeaveRequestOut.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@app.get("/leave-requests/{leave_request_id}", response_model=LeaveRequestOut)
def get_leave_request(leave_request_id: int, db: Session = Depends(get_db)):
    lr = db.get(LeaveRequest, leave_request_id)
    if not lr:
        raise HTTPException(status_code=404, detail="Leave request not found")
    return lr


@app.post("/leave-requests/{leave_request_id}/review", response_model=LeaveRequestOut)
def review_leave_request(
    leave_request_id: int,
    body: LeaveRequestReview,
    actor_id: int = Depends(current_user_id),
    db: Session = Depends(get_db),
):
    try:
        return services.approve_leave_request(
            db,
            leave_request_id=leave_request_id,
            approver_id=actor_id,
            decision=body.decision,
        )
    except services.LeaveError as e:
        raise _map_leave_error(e) from e


@app.post("/leave-requests/{leave_request_id}/cancel", response_model=LeaveRequestOut)
def cancel_leave_request(
    leave_request_id: int,
    actor_id: int = Depends(current_user_id),
    db: Session = Depends(get_db),
):
    try:
        return services.cancel_leave_request(
            db,
            leave_request_id=leave_request_id,
            employee_id=actor_id,
        )
    except services.LeaveError as e:
        raise _map_leave_error(e) from e


@app.get("/leave-balances/{employee_id}", response_model=list[LeaveBalanceOut])
def get_balance(
    employee_id: int,
    year: int | None = Query(None),
    db: Session = Depends(get_db),
):
    balances = services.get_leave_balances(db, employee_id=employee_id, year=year)
    return [LeaveBalanceOut.model_validate(b) for b in balances]


