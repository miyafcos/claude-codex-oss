from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nanogpt_key_setup_test", ROOT / "scripts" / "setup-nanogpt-key.py")
SETUP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SETUP)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", ["https://example.com/collect", "http://example.com/collect", "https://nano-gpt.com/other"])
def test_key_check_refuses_every_redirect(status, target):
    request = Request("https://nano-gpt.com/api/subscription/v1/usage", headers={"Authorization": "Bearer fixture-secret"})
    with pytest.raises(HTTPError):
        SETUP._NoRedirect().redirect_request(request, None, status, "redirect", {}, target)


@pytest.mark.parametrize("routing", [
    {}, {"paidSpendPolicyAllowsBalance": True},
    {"paidSpendPolicyAllowsBalance": False, "recommendedMode": "paygo", "subscriptionQuotaAvailable": True},
    {"paidSpendPolicyAllowsBalance": False, "recommendedMode": "subscription", "subscriptionQuotaAvailable": None},
])
def test_registration_rejects_unsafe_or_unknown_billing(monkeypatch, routing):
    class Reply(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            return Reply(json.dumps({"routing": routing}).encode())

    monkeypatch.setattr(SETUP, "build_opener", lambda handler: Opener())
    with pytest.raises(ValueError):
        SETUP._check_subscription("fixture-secret")


def test_registration_accepts_subscription_only(monkeypatch):
    class Reply(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://nano-gpt.com/api/subscription/v1/usage"
            return Reply(json.dumps({"routing": {"paidSpendPolicyAllowsBalance": False, "recommendedMode": "subscription", "subscriptionQuotaAvailable": True}}).encode())

    monkeypatch.setattr(SETUP, "build_opener", lambda handler: Opener())
    SETUP._check_subscription("fixture-secret")
