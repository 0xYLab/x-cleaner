from __future__ import annotations

import asyncio
import builtins
import importlib
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class LauncherTests(unittest.TestCase):
    def test_cookie_entry_returns_to_login_menu(self) -> None:
        # Import is delayed until the implementation file exists in a normal
        # installation.  The mock module ensures this test remains fully local.
        fake_cleaner = type(sys)("cookie_cleaner")
        fake_cleaner.run_cookie_cleaner = AsyncMock(return_value=None)
        answers = iter(["1", "0"])
        with patch.dict(sys.modules, {"cookie_cleaner": fake_cleaner}):
            sys.modules.pop("main", None)
            module = importlib.import_module("main")
            with patch.object(builtins, "input", side_effect=lambda _="": next(answers)):
                result = asyncio.run(module.app())
        self.assertEqual(result, 0)
        fake_cleaner.run_cookie_cleaner.assert_awaited_once_with()

    def test_invalid_choice_does_not_start_login(self) -> None:
        fake_cleaner = type(sys)("cookie_cleaner")
        fake_cleaner.run_cookie_cleaner = AsyncMock(return_value=None)
        answers = iter(["x", "0"])
        with patch.dict(sys.modules, {"cookie_cleaner": fake_cleaner}):
            sys.modules.pop("main", None)
            module = importlib.import_module("main")
            with patch.object(builtins, "input", side_effect=lambda _="": next(answers)):
                result = asyncio.run(module.app())
        self.assertEqual(result, 0)
        fake_cleaner.run_cookie_cleaner.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
