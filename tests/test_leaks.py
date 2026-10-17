import json
import random

import pytest

from loglens.backends import parse_line
from loglens.cli import main
from loglens.index import index_source
from loglens.leaks import LeakConfig, find_leaks, mask, scan_record, scan_text


def kinds(text, **kw):
    return [(f.kind, f.masked) for f in scan_text(text, **kw)]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("key AKIAIOSFODNN7EXAMPLE used", [("aws_access_key", "AKIA***LE")]),
        ("gh token ghp_" + "a" * 36, [("github_token", "ghp_***aa")]),
        ("stripe sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc", [("stripe_key", "sk_l***dc")]),
        ('login {"password": "hunter22", "user": "x"}', [("password", "***")]),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", [("bearer_token", "abcd***56")]),
        (
            "-----BEGIN RSA PRIVATE KEY----- MIIE",
            [("private_key", "-----BEGIN RSA PRIVATE KEY-----")],
        ),
        ("user bob@example.com logged in", [("email", "b***@example.com")]),
        ("iban DE89 3704 0044 0532 0130 00 ok", [("iban", "******************3000")]),
        ("ssn 123-45-6789", [("us_ssn", "***-**-**89")]),
        ("card 4111 1111 1111 1111 charged", [("card_number", "************1111")]),
        ("amex 3782 822463 10005", [("card_number", "***********0005")]),
        ("paid with card 4111111111111111", [("card_number", "************1111")]),
    ],
)
def test_detectors_find_and_mask(text, expected):
    assert kinds(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "charging order 41 for 1999 cents",
        "order 4111111111111111 shipped",  # Luhn-valid but no card context: an ID
        "card 4111111111111112 declined",  # fails Luhn
        "ts 1760000000000 trace 2026-10-16-12345",
        "iban DE00 0000 0000 0000 0000 00",  # fails mod-97
        "ssn 000-12-3456",
        "password reset requested",  # the word, not a value
    ],
)
def test_detectors_ignore_lookalikes(text):
    assert kinds(text) == []


def test_random_ids_are_not_cards():
    rng = random.Random(1)
    lines = (
        f"ts {rng.randint(10**12, 2 * 10**12)} order {rng.randint(10**15, 10**16)}"
        for _ in range(5000)
    )
    assert sum(bool(scan_text(line)) for line in lines) == 0


def test_masks_never_contain_the_full_value():
    for kind, value in [
        ("email", "jane.doe@corp.com"),
        ("card_number", "4111 1111 1111 1111"),
        ("password", "correct-horse-battery"),
        ("us_ssn", "123-45-6789"),
    ]:
        masked = mask(kind, value)
        assert value not in masked and "*" in masked


def test_config_ignore_allow_and_fields():
    text = "from qa@example.com to jane@corp.com"
    assert [m for _, m in kinds(text, cfg=LeakConfig(allow=[r"@example\.com$"]))] == [
        "j***@corp.com"
    ]
    assert kinds(text, cfg=LeakConfig(ignore={"email"})) == []
    with pytest.raises(ValueError, match="unknown leak kind"):
        LeakConfig.from_dict({"ignore": ["emails"]})

    record = parse_line(
        json.dumps(
            {
                "message": "signup",
                "user": {"email": "jane@corp.com"}.get("email"),
                "pathname": "/srv/a@b.co/app.py",  # metadata is never scanned
                "note": "jane@corp.com",
            }
        )
    )
    assert [(f.where, f.kind) for f in scan_record(record)] == [
        ("user", "email"),
        ("note", "email"),
    ]
    assert [f.where for f in scan_record(record, LeakConfig(ignore_fields={"note"}))] == ["user"]


def test_find_leaks_groups_by_log_call_secrets_first():
    sites = index_source(
        'logger.info("signup %s", email)\nlogger.info("auth header %s", h)\n', "app.py"
    )
    records = [
        parse_line("signup jane@corp.com"),
        parse_line("signup bob@corp.com"),
        parse_line("signup nobody"),  # no leak
        parse_line("auth header Bearer abcdefghijklmnopqrstuvwxyz123456"),
        parse_line("something else entirely: ssn 123-45-6789"),  # no matching call
    ]
    leaks, scanned = find_leaks(sites, records)
    assert scanned == 5
    assert [(e.id, e.records, e.kinds) for e in leaks] == [
        ("app.py:2", 1, {"bearer_token": 1}),  # secrets rank first
        ("app.py:1", 2, {"email": 2}),
        ("<unknown source>", 1, {"us_ssn": 1}),
    ]
    assert leaks[1].samples["email"].masked == "j***@corp.com"


def test_cli_leaks_on_demo_app(monkeypatch, capsys):
    monkeypatch.chdir("examples/demo_app")
    assert main(["leaks"]) == 1
    captured = capsys.readouterr()
    assert "app/checkout.py:13" in captured.out
    assert "card_number" in captured.out and "************1111" in captured.out
    assert "4111 1111 1111 1111" not in captured.out + captured.err
    assert main(["leaks", "--format", "json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["scanned"] == 7 and out["leaks"][0]["id"] == "app/checkout.py:13"


def test_bad_leaks_config_is_a_clean_error(tmp_path, monkeypatch):
    (tmp_path / "loglens.toml").write_text('[leaks]\nignore = ["emails"]\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="bad config: unknown leak kind"):
        main(["index"])
