import threading


class ProviderAdmission:
    def __init__(self, foreground_timeout=120):
        self._condition = threading.Condition()
        self._active = None
        self._waiting = False
        self._timeout = foreground_timeout

    def acquire_foreground(self):
        with self._condition:
            if self._waiting or self._active == "foreground":
                return False
            if self._active == "diagram":
                self._waiting = True
                try:
                    if not self._condition.wait_for(lambda: self._active is None, self._timeout):
                        return False
                finally:
                    self._waiting = False
            self._active = "foreground"
            return True

    def acquire_diagram(self):
        with self._condition:
            if self._active is not None or self._waiting:
                return False
            self._active = "diagram"
            return True

    def diagram_dispatch_allowed(self):
        with self._condition:
            return self._active == "diagram" and not self._waiting

    def acquire(self, blocking=True):
        with self._condition:
            if blocking:
                self._condition.wait_for(lambda: self._active is None and not self._waiting)
            elif self._active is not None or self._waiting:
                return False
            self._active = "foreground"
            return True

    def release(self):
        with self._condition:
            if self._active is None:
                raise ValueError("Provider slot already released")
            self._active = None
            self._condition.notify_all()
