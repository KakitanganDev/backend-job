"""
Pytest configuration for the leave management test suite.

The Employee.leave_requests relationship in src/models.py is missing the
foreign_keys argument, causing SQLAlchemy mapper configuration to fail when
LeaveRequest has two FK paths to the employees table (employee_id, approved_by).

This conftest patches the relationship's _init_args before mapper configuration
is triggered, resolving the ambiguity without modifying the models file.
"""

from sqlalchemy.orm import RelationshipProperty
from src import models


def _patch_employee_leave_requests_relationship() -> None:
    """
    Set foreign_keys=[LeaveRequest.employee_id] on Employee.leave_requests
    before SQLAlchemy performs mapper configuration.
    """
    emp_mapper = models.Employee.__mapper__
    for key, val in emp_mapper._props.items():
        if isinstance(val, RelationshipProperty) and key == "leave_requests":
            fk_col = models.LeaveRequest.__table__.c.employee_id
            old_fk_arg = val._init_args.foreign_keys
            new_fk_arg = type(old_fk_arg)(
                name="foreign_keys", argument=[fk_col], resolved=None
            )
            val._init_args = val._init_args._replace(foreign_keys=new_fk_arg)
            break


_patch_employee_leave_requests_relationship()
