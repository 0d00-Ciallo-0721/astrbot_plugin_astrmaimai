import importlib
import unittest

from tests.fixtures import TempAstrbotEnv


class SharedTestSupportRefactorTests(unittest.TestCase):
    def test_temp_astrbot_env_installs_local_data_path(self):
        with TempAstrbotEnv() as env:
            path_mod = importlib.import_module("astrbot.core.utils.astrbot_path")
            self.assertEqual(path_mod.get_astrbot_data_path(), env.path)
