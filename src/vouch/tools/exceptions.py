class RetryableToolError(Exception):
    """Exception raised when a tool call fails but is retryable."""
    pass
