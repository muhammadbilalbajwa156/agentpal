"""Autonomous companies demo: business agents that spend only through AgentPal (sandbox)."""

from .operator import Operator
from .runner import run_company
from .scenarios import COMPANIES

__all__ = ["COMPANIES", "Operator", "run_company"]
