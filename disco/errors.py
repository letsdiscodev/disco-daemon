class DiscoError(Exception):
    pass


class ProcessStatusError(DiscoError):
    def __init__(self, *arg, status: int | None, **kw):
        self.status = status
        super().__init__(*arg, **kw)
