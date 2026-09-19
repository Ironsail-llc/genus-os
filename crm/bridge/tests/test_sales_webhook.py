"""Provider credentials bypass JWT parsing only on the exact webhook route."""

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_provider_header_reaches_own_authentication_only_on_exact_post(
    test_client, monkeypatch
):
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("ROBOTHOR_BRIDGE_HOST", "0.0.0.0")
    headers = {"Authorization": "Bearer webhook-test-secret"}
    path = "/api/integrations/instantly/test-tenant/webhook"
    with patch("robothor.sales.ingestion.vault.get", return_value="webhook-test-secret") as secret:
        # Reaching body validation proves the custom header was not parsed as JWT.
        result = await test_client.post(path, content=b"not-json", headers=headers)
        assert result.status_code == 422
        assert secret.call_args.kwargs["tenant_id"] == "test-tenant"
        secret.reset_mock()
        for method, target in [
            ("GET", path),
            ("POST", path + "/extra"),
            ("POST", "/api/sales/settings"),
        ]:
            assert (await test_client.request(method, target, headers=headers)).status_code == 401
        assert not any(
            call.args[0].startswith("providers/instantly/") for call in secret.call_args_list
        )
        assert (
            await test_client.post(path, content=b"{}", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
