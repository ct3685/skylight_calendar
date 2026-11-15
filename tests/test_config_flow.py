"""Config, reauth and options flows."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.skylight.const import (
    CONF_ACCESS_TOKEN,
    CONF_FRAME_ID,
    CONF_LISTS_INTERVAL,
    DOMAIN,
)

from .conftest import FRAME_ID, FakeSkylight

pytestmark = pytest.mark.usefixtures("patch_session")

CALLBACK = "https://ourskylight.com/welcome?code=good-code&state=x"


async def test_sign_in_creates_entry(hass: HomeAssistant, skylight: FakeSkylight) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    url = result["description_placeholders"]["auth_url"]
    assert url.startswith("https://app.ourskylight.com/oauth/authorize?")
    assert "code_challenge_method=S256" in url
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": CALLBACK})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test Home"
    assert result["data"][CONF_FRAME_ID] == FRAME_ID
    assert result["data"][CONF_ACCESS_TOKEN] == skylight.access_token
    form = skylight.calls_to("POST", r"^/oauth/token$")[0].kwargs["data"]
    assert form["code"] == "good-code"
    assert len(form["code_verifier"]) >= 43


async def test_rejected_code(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "bad-code"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "  "})
    assert result["errors"] == {"code": "invalid_code"}


async def test_network_failure_is_cannot_connect(hass: HomeAssistant, skylight: FakeSkylight) -> None:
    skylight.fail("POST", r"^/oauth/token$", TimeoutError())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "good-code"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_frame_aborts(hass: HomeAssistant, entry_data) -> None:
    MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id=f"skylight_frame_{FRAME_ID}").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "good-code"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_tokens(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id=f"skylight_frame_{FRAME_ID}", version=2)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "good-code"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == skylight.access_token
    assert entry.data[CONF_FRAME_ID] == FRAME_ID


async def test_reauth_with_other_account_is_refused(hass: HomeAssistant, entry_data) -> None:
    entry_data[CONF_FRAME_ID] = "9999"
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id="skylight_frame_9999", version=2)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "good-code"})
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ACCESS_TOKEN] == entry_data[CONF_ACCESS_TOKEN]


async def test_options_flow(hass: HomeAssistant, entry_data) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id=f"skylight_frame_{FRAME_ID}", version=2)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LISTS_INTERVAL: 300}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_LISTS_INTERVAL] == 300
    assert entry.runtime_data.options[CONF_LISTS_INTERVAL] == 300
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {CONF_LISTS_INTERVAL: 5})
