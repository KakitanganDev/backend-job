"""
FastAPI application for Kakitangan Leave Management System.

This is the entrypoint. Routes are defined but most business logic
in services.py needs to be implemented to make everything work.
"""

from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.database import engine, get_db, Base
from src.models import LeaveType, LeaveStatus
from src import services

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Kakitangan Leave Management API", version="0.1.0")


# ── Schemas ──────────────────────────────────────────────────────────────

class EmployeeOut(BaseModel):
    id: int
    name: str
    email: str
    department: str
    manager_id: Optional[int] = None

    class Config:
        from_attributes = True


class LeaveBalanceOut(BaseModel):
    leave_type: LeaveType
    year: int
    total_days: float
    used_days: float
    remaining_days: float

    class Config:
        from_attributes = True


class LeaveRequestCreate(BaseModel):
    employee_id: int
    leave_type: LeaveType
    start_date: date
    end_date: date
    reason: Optional[str] = None


class LeaveRequestOut(BaseModel):
    id: int
    employee_id: int
    leave_type: LeaveType
    start_date: date
    end_date: date
    reason: Optional[str]
    status: LeaveStatus
    approved_by: Optional[int]
    approved_at: Optional[datetime]

    class Config:
        from_attributes = True


class LeaveRequestApprove(BaseModel):
    approver_id: int = Field(description="ID of the manager performing the review")
    decision: LeaveStatus = Field(description="approved or rejected")


class PaginatedLeaveRequests(BaseModel):
    items: list[LeaveRequestOut]
    total: int
    page: int
    page_size: int


# ── Routes ───────────────────────────────────────────────────────────────

@app.get("/employees", response_model=list[EmployeeOut])
def list_employees(db: Session = Depends(get_db)):
    from src.models import Employee
    return db.query(Employee).all()


@app.get("/employees/{employee_id}", response_model=dict)
def get_employee(employee_id: int, db: Session = Depends(get_db)):
    from src.models import Employee
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    balances = services.get_leave_balances(db, employee_id)
    return {
        "employee": EmployeeOut.model_validate(emp),
        "leave_balances": [LeaveBalanceOut.model_validate(b) for b in balances],
    }


@app.post("/leave-requests", response_model=LeaveRequestOut, status_code=201)
def create_leave_request(body: LeaveRequestCreate, db: Session = Depends(get_db)):
    try:
        lr = services.create_leave_request(
            db, employee_id=body.employee_id, leave_type=body.leave_type,
            start_date=body.start_date, end_date=body.end_date, reason=body.reason,
        )
        return lr
    except services.LeaveError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/leave-requests", response_model=PaginatedLeaveRequests)
def list_leave_requests(
    employee_id: Optional[int] = Query(None),
    status: Optional[LeaveStatus] = Query(None),
    leave_type: Optional[LeaveType] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    items, total = services.get_leave_requests(
        db, employee_id=employee_id, status=status, leave_type=leave_type,
        from_date=from_date, to_date=to_date, page=page, page_size=page_size,
    )
    return PaginatedLeaveRequests(
        items=[LeaveRequestOut.model_validate(i) for i in items],
        total=total, page=page, page_size=page_size,
    )


@app.get("/leave-requests/{leave_request_id}", response_model=LeaveRequestOut)
def get_leave_request(leave_request_id: int, db: Session = Depends(get_db)):
    from src.models import LeaveRequest
    lr = db.query(LeaveRequest).filter(LeaveRequest.id == leave_request_id).first()
    if not lr:
        raise HTTPException(status_code=404, detail="Leave request not found")
    return lr


@app.post("/leave-requests/{leave_request_id}/review", response_model=LeaveRequestOut)
def review_leave_request(
    leave_request_id: int,
    body: LeaveRequestApprove,
    db: Session = Depends(get_db),
):
    try:
        lr = services.approve_leave_request(
            db, leave_request_id=leave_request_id, approver_id=body.approver_id, decision=body.decision,
        )
        return lr
    except services.LeaveError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/leave-requests/{leave_request_id}/cancel", response_model=LeaveRequestOut)
def cancel_leave_request(leave_request_id: int, employee_id: int = Query(...), db: Session = Depends(get_db)):
    try:
        lr = services.cancel_leave_request(db, leave_request_id=leave_request_id, employee_id=employee_id)
        return lr
    except services.LeaveError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/leave-balances/{employee_id}", response_model=list[LeaveBalanceOut])
def get_balance(employee_id: int, year: Optional[int] = Query(None), db: Session = Depends(get_db)):
    balances = services.get_leave_balances(db, employee_id=employee_id, year=year)
    return [LeaveBalanceOut.model_validate(b) for b in balances]


@app.on_event("startup")
def on_startup():
    db = next(get_db())
    try:
        services.seed_demo_data(db)
    finally:
        db.close()
