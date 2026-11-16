from telco_iq.contracts import Status


class LabError(Exception):
    def __init__(self, message: str, status: Status = Status.FAILED):
        super().__init__(message)
        self.status = status
