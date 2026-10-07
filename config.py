"""Fail-closed configuration, always loaded in the bound native profile."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    owner_ids: frozenset[int]
    # None means Telegram's managed-chat selection is the source of truth.
    # An explicit empty set still disables all chats, rather than widening access.
    allowed_chats: frozenset[int] | None = None
    triggers: tuple[str, ...] = ("Hermes", "Гермес")
    cli: str = "hermes"
    timeout: int = 300
    stt_timeout: int = 180
    max_concurrent: int = 2
    max_voice_bytes: int = 10 * 1024 * 1024
    max_voice_seconds: int = 300

    @classmethod
    def load(cls, home: Path):
        from hermes_cli.config_effective import load_user_config_effective
        raw = load_user_config_effective(home / "config.yaml", fail_closed=True)
        block = raw.get("plugins", {}).get("entries", {}).get("business-powerpack", {})
        def ids(key):
            values = block.get(key, [])
            if not isinstance(values, list) or any(type(v) is not int or v <= 0 for v in values):
                raise ValueError("Expected positive numeric private-chat IDs")
            return frozenset(values)
        triggers = block.get("triggers", ["Hermes", "Гермес"])
        if not isinstance(triggers, list) or not triggers or any(
            not isinstance(t, str) or not t.isalpha() or len(t) > 32 for t in triggers
        ):
            raise ValueError("Invalid triggers")
        cli = block.get("cli", "hermes")
        if not isinstance(cli, str) or not cli or (cli != "hermes" and not Path(cli).is_absolute()):
            raise ValueError("CLI must be hermes or an absolute executable path")
        bounds = {"timeout": (300, 1, 1800), "stt_timeout": (180, 1, 1800), "max_concurrent": (2, 1, 8),
                  "max_voice_bytes": (10 * 1024 * 1024, 1, 20 * 1024 * 1024),
                  "max_voice_seconds": (300, 1, 1200)}
        opts = {}
        for key, (default, low, high) in bounds.items():
            value = block.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid limit")
            opts[key] = value
        chats = ids("allowed_chats") if "allowed_chats" in block else None
        return cls(ids("owner_ids"), chats, tuple(triggers), cli, **opts)

    def allows_chat(self, chat: int) -> bool:
        return self.allowed_chats is None or chat in self.allowed_chats
