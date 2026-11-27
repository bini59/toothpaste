"""HTTP handlers for the dashboard."""


class Forbidden(Exception):
    """The requester may use the dashboard but cannot perform this action."""
