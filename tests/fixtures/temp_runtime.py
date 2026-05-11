import tempfile

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class TempAstrbotEnv:
    def __init__(self):
        self._temp_dir = None

    @property
    def path(self):
        return self._temp_dir.name

    def __enter__(self):
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self._temp_dir.name)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()
        return False

    def cleanup(self):
        if self._temp_dir is None:
            return
        try:
            self._temp_dir.cleanup()
        except Exception:
            pass
