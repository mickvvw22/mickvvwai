import inspect

from agent import prompt_builder
from agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    HERMES_AGENT_HELP_GUIDANCE,
    HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS,
)
from hermes_cli.default_soul import DEFAULT_SOUL_MD, _SCAFFOLD_HEAD
from hermes_cli.setup_profile import SETUP_CHAT_TITLE, SETUP_PROFILE_DESCRIPTION, SETUP_SOUL


def test_user_facing_persona_templates_use_mickvvw_not_xinyuan() -> None:
    surfaces = (
        DEFAULT_AGENT_IDENTITY,
        HERMES_AGENT_HELP_GUIDANCE,
        HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS,
        DEFAULT_SOUL_MD,
        _SCAFFOLD_HEAD,
        SETUP_CHAT_TITLE,
        SETUP_PROFILE_DESCRIPTION,
        SETUP_SOUL,
    )

    for surface in surfaces:
        assert "xinyuan" not in surface.casefold()
        assert "鑫源" not in surface


def test_default_and_setup_personas_do_not_pitch_profiles_or_features() -> None:
    assert "unsolicited welcome pitch" in DEFAULT_SOUL_MD.casefold()
    assert "suggest creating a personal profile" in DEFAULT_SOUL_MD.casefold()
    assert "do not give an unsolicited feature tour" in SETUP_SOUL.casefold()
    assert "do not initiate check-ins or feature suggestions" in SETUP_SOUL.casefold()


def test_prompt_builder_has_no_legacy_product_name_in_platform_hints() -> None:
    source = inspect.getsource(prompt_builder)
    assert "xinyuan" not in source.casefold()
    assert "鑫源" not in source
