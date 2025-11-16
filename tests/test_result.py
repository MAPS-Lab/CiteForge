from __future__ import annotations


class BaseTestResult:
    """
    Base class for all test results, providing common functionality for tracking test
    execution status, messages, warnings, and details.
    """
    def __init__(self, name: str):
        """
        Initialize test result with a descriptive name.
        """
        self.name = name
        self.passed = False
        self.message = ""
        self.warnings = []
        self.details = {}

    def success(self, msg: str = "", details=None):
        """
        Mark test as successful with an optional message and details.
        """
        self.passed = True
        self.message = msg or "PASS"
        if details is not None:
            self.details = details

    def failure(self, msg: str, details=None):
        """
        Mark test as failed with a message and optional details.
        """
        self.passed = False
        self.message = msg
        if details is not None:
            self.details = details

    def add_warning(self, msg: str):
        """
        Add a warning message to the test result.
        """
        self.warnings.append(msg)
