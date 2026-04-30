from unittest.mock import MagicMock, patch

# --- Mock nonebot BEFORE any test module imports query.py ---
# query.py calls on_command() at module level, which triggers get_driver().
# get_driver is called from nonebot.rule.command().

_mock_driver = MagicMock()
_mock_driver.config.command_start = {"/"}
_mock_driver.config.command_sep = {"."}

patch("nonebot.get_driver", return_value=_mock_driver).start()
patch("nonebot.rule.get_driver", return_value=_mock_driver).start()
