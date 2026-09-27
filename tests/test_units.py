"""Fast unit tests: redaction, policy, parsing, tenancy, schema integrity."""

import pytest

from cua.replay.engine import parse_value
from cua.runtime import POLICY, PROFILE
from cua.safety.policy import Policy
from cua.safety.redact import Redactor
from cua.tenancy import AppProfile


# ---------------------------------------------------------------- redaction

def test_redacts_ssn_pan_email_secrets_and_keys():
    r = Redactor(["demo-pass-123"])
    out = r.text("SSN 521-44-9087 card 4111 1111 1111 1111 mail a.b@x.org pw demo-pass-123 key sk-ant-abcdefghijklmnopqrst")
    assert "521-44" not in out and "***-**-9087" in out
    assert "4111 1111" not in out and "****1111" in out
    assert "a.b@x.org" not in out
    assert "demo-pass-123" not in out
    assert "sk-ant-" not in out


def test_redaction_keeps_ordinary_numbers_and_ui_words():
    r = Redactor()
    assert r.text("member 10492 balance $14,250.80") == "member 10492 balance $14,250.80"
    assert r.text('password field next to "PASSWORD"') == 'password field next to "PASSWORD"'
    assert r.text("1234 5678 9012 3456") == "1234 5678 9012 3456"  # fails Luhn: not a PAN


def test_redacts_key_value_credentials():
    r = Redactor()
    assert "hunter22" not in r.text('{"password": "hunter22"}')
    assert "abc123" not in r.text("token=abc123")


# ------------------------------------------------------------------- policy

@pytest.fixture(scope="module")
def policy():
    return Policy.load(str(POLICY))


def test_url_allowlist(policy):
    assert policy.url_allowed("http://127.0.0.1:8600/mbr?id=1")[0]
    assert not policy.url_allowed("https://evil.example.com/")[0]
    assert not policy.url_allowed("http://127.0.0.1:8600/_harness/faults")[0]
    assert not policy.url_allowed("file:///etc/passwd")[0]


def test_risk_classification(policy):
    assert policy.classify("fill", "POST TRANSFER") == "safe"  # typing never commits
    assert policy.classify("click", "POST TRANSFER") == "irreversible"
    assert policy.classify("click", "INQUIRE") == "safe"
    assert policy.classify("click", "Save") == "reversible"
    assert policy.check_action("click", "irreversible").verdict == "approve"
    assert policy.check_action("download", "safe").verdict == "block"


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize("raw,typ,expected", [
    ("$14,250.80", "currency", "14250.80"),
    ("$(8,120.00)", "currency", "-8120.00"),
    ("$58", "currency", "58.00"),
    ("03/14/2009", "date", "2009-03-14"),
    ("1,204", "integer", 1204),
    ("DALE", "string", "DALE"),
])
def test_parse_value(raw, typ, expected):
    assert parse_value(raw, typ) == expected


def test_parse_value_rejects_garbage():
    with pytest.raises(ValueError):
        parse_value("N/A", "currency")
    with pytest.raises(ValueError):
        parse_value("", "string")


# ------------------------------------------------------------------ profile

def test_condition_precedence_business_over_recoverable():
    prof = AppProfile.load(str(PROFILE))
    c = prof.detect("SYSTEM NOTICE ... E-404 NO MEMBER ON FILE FOR 99999")
    assert c.code == "MEMBER_NOT_FOUND" and c.cls == "business"
    assert prof.detect("MAIN MENU") is None
    assert prof.detect("SESSION EXPIRED - PLEASE SIGN ON AGAIN").cls == "recoverable"
