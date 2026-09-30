from .model import DataError


class SyncError(DataError):
    def __init__(self, message: str, code: str = "network"):
        super().__init__(message)
        self.code = code


def safe_error(exc: Exception) -> SyncError:
    if isinstance(exc, SyncError):
        return exc
    if isinstance(exc, DataError):
        return SyncError(str(exc), "data")
    # Never show HTTP exception text, request URLs, cookies or response bodies.
    return SyncError("Не удалось получить данные Instagram. Предыдущие списки сохранены; повторите позже.")
