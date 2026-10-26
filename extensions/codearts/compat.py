"""Process-local compatibility for CodeArts on supported native Hub versions."""

def install_heartbeat_compat(hub, runtime):
    native = getattr(hub, "SSEHeartbeat", None)
    if native is None:
        return

    class CodeArtsHeartbeat(native):
        def __init__(self, *args, **kwargs):
            self._codearts = getattr(runtime.local, "codearts_stream", False)
            self._terminal_sent = False
            super().__init__(*args, **kwargs)

        def write(self, data):
            # Native Messages catches upstream failure after __exit__ closes the
            # heartbeat. Restore only its terminal error for CodeArts requests.
            with self._lock:
                if self._closed and self._codearts:
                    if not self._terminal_sent and data.startswith(b"event: error\n"):
                        self._terminal_sent = True
                        self._write(data)
                    return
                return super().write(data)

    hub.SSEHeartbeat = CodeArtsHeartbeat
