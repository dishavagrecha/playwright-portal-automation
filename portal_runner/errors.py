"""Errors classified by whether repeating a stage can plausibly succeed."""


class RetryableError(RuntimeError):
    pass


class FinalError(RuntimeError):
    pass


class AuthenticationError(FinalError):
    pass
