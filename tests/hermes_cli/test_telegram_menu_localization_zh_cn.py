"""Regression tests for the Simplified Chinese Telegram command menu."""

from hermes_cli import commands_platforms


def test_telegram_menu_localizes_descriptions_without_changing_command_names(monkeypatch):
    names = ["help", "restartapp", "addmodel"]
    english_descriptions = [(name, "English placeholder") for name in names]
    monkeypatch.setattr(
        commands_platforms,
        "telegram_bot_commands",
        lambda **_kwargs: english_descriptions,
    )
    monkeypatch.setattr(
        commands_platforms,
        "_collect_gateway_skill_entries",
        lambda **_kwargs: ([], 0),
    )
    monkeypatch.setattr(
        commands_platforms,
        "_prioritize_telegram_menu_candidates",
        lambda candidates: candidates,
    )

    menu, hidden = commands_platforms.telegram_menu_commands(max_commands=len(names))

    assert hidden == 0
    assert [name for name, _description in menu] == names
    expected = commands_platforms._TELEGRAM_MENU_DESCRIPTIONS_ZH_CN
    assert [description for _name, description in menu] == [expected[name] for name in names]
    assert all(any("\u4e00" <= char <= "\u9fff" for char in description) for _, description in menu)


def test_localized_menu_descriptions_fit_telegram_limits():
    mapping = commands_platforms._TELEGRAM_MENU_DESCRIPTIONS_ZH_CN
    assert mapping
    assert all(len(description) <= 256 for description in mapping.values())
    assert all(
        name and len(name) <= 32 and all(char in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in name)
        for name in mapping
    )
