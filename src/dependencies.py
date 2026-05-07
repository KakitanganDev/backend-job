"""
FastAPI dependencies for authentication and authorization.
"""


from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from src.database import get_db
from src.models import Employee

_AUTH_ERROR = "Missing or malformed Authorization header"


def get_current_employee(
    authorization: str | None = Header(None, description="Bearer {employee_id}"),
) -> int:
    """Parse Authorization header and return the caller's employee_id.

    Missing or malformed header returns 401.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail=_AUTH_ERROR)

    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail=_AUTH_ERROR)

    try:
        employee_id = int(token)
    except ValueError:
        raise HTTPException(status_code=401, detail=_AUTH_ERROR)

    return employee_id


def require_manager(
    employee_id: int = Depends(get_current_employee),
    db: Session = Depends(get_db),
) -> int:
    """Require that the caller is a top-level employee (manager_id IS NULL).

    Returns the employee_id if authorized, 403 otherwise.
    """
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee or employee.manager_id is not None:
        raise HTTPException(
            status_code=403, detail="Only managers can perform this action"
        )

    return employee_id
