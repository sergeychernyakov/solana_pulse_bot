# tests/models/test_task.py

"""Tests for the SQLAlchemy Task model."""

from src.models.task import Task


def test_task_repr_includes_core_fields() -> None:
    """Test task string representation contains id, title, and status."""
    task = Task(id=7, title="Ship checks", completed=True)

    assert repr(task) == "<Task(id=7, title='Ship checks', completed=True)>"
