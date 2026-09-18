"""Owner IDs in config.yaml are digits — YAML hands them over as ``int``.

Observed 2026-09-16 21:20 on the "Vi" bot (VPS lesivi): right after the plugin
auto-update the gateway logged

    File "/root/.hermes/plugins/zalo-personal/adapter.py", line 864, in __init__
        ).strip()
    AttributeError: 'int' object has no attribute 'strip'
    ERROR gateway.run: Platform 'zalo-personal' is registered but adapter
    creation failed (check dependencies and config)

and Zalo stayed dead for two days while Telegram kept running. The config held

    platforms:
      zalo-personal:
        extra:
          owner_user_id: 7577463786   # no quotes -> YAML int

``__init__`` chained ``.strip()`` straight onto the value, so any unquoted ID
in config.yaml kills the whole adapter. The env vars are always ``str``, which
is why the bug only shows up on installs that configure the owner through
config.yaml instead of ``.env``.

``adapter.py`` pulls in ``gateway.*`` (absent outside a Hermes install), so the
two assignments are lifted out of ``__init__`` with ``ast`` and executed
against a bare stub — same trick as ``test_owner_slash_command_gate.py``.
"""

import ast
import os
import textwrap
import unittest

_ADAPTER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter.py"
)
_OWNER_FIELDS = ("owner_uid", "owner_user_id")


def _owner_id_source() -> str:
    """Source of the two ``self.owner_*`` assignments in ``__init__``."""
    with open(_ADAPTER, encoding="utf-8") as f:
        src = f.read()
    cls = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ClassDef) and n.name == "ZaloPersonalAdapter"
    )
    init = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    stmts = [
        n for n in init.body
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr in _OWNER_FIELDS
            for t in n.targets
        )
    ]
    assert len(stmts) == len(_OWNER_FIELDS), f"tìm thấy {len(stmts)} phép gán owner"
    return "\n".join(textwrap.dedent(ast.get_source_segment(src, s)) for s in stmts)


_SOURCE = _owner_id_source()


class _Bot:
    pass


def _resolve(extra: dict, env: dict = None) -> _Bot:
    """Run the lifted assignments with ``extra`` from config and ``env`` set."""
    saved = {k: os.environ.pop(k, None) for k in (
        "ZALO_PERSONAL_OWNER_UID", "ZALO_PERSONAL_OWNER_USER_ID"
    )}
    os.environ.update(env or {})
    try:
        bot = _Bot()
        exec(_SOURCE, {"os": os}, {"self": bot, "extra": extra})
        return bot
    finally:
        for k in ("ZALO_PERSONAL_OWNER_UID", "ZALO_PERSONAL_OWNER_USER_ID"):
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


class OwnerIdFromYamlIntTest(unittest.TestCase):
    def test_unquoted_ids_in_config_do_not_crash(self):
        """The exact lesivi config: both IDs unquoted -> YAML ints."""
        bot = _resolve({"owner_uid": 7440520648218846215, "owner_user_id": 7577463786})
        self.assertEqual(bot.owner_uid, "7440520648218846215")
        self.assertEqual(bot.owner_user_id, "7577463786")

    def test_quoted_ids_still_work(self):
        bot = _resolve({"owner_uid": "7440520648218846215", "owner_user_id": "7577463786"})
        self.assertEqual(bot.owner_uid, "7440520648218846215")
        self.assertEqual(bot.owner_user_id, "7577463786")

    def test_env_wins_over_config(self):
        bot = _resolve(
            {"owner_uid": 111, "owner_user_id": 222},
            {"ZALO_PERSONAL_OWNER_UID": " 333 ", "ZALO_PERSONAL_OWNER_USER_ID": "444"},
        )
        self.assertEqual(bot.owner_uid, "333")
        self.assertEqual(bot.owner_user_id, "444")

    def test_owner_user_id_falls_back_to_owner_uid(self):
        bot = _resolve({"owner_uid": 7440520648218846215})
        self.assertEqual(bot.owner_user_id, "7440520648218846215")

    def test_no_owner_configured_stays_empty(self):
        bot = _resolve({})
        self.assertEqual(bot.owner_uid, "")
        self.assertEqual(bot.owner_user_id, "")


if __name__ == "__main__":
    unittest.main()
