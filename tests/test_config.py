import pytest
from pydantic import SecretStr, ValidationError

from grocery.config import Settings


def base(**kw) -> dict:
    values = dict(allowed_hosts=["x"], _env_file=None)
    values.update(kw)
    return values


def test_oidc_is_off_by_default_and_needs_nothing_else():
    settings = Settings(**base())
    assert settings.oidc_issuer == "" and settings.oidc_redirect_uri_list == []


def test_a_full_oidc_block_validates():
    settings = Settings(**base(
        oidc_issuer="https://auth.example.nl/application/o/grocery-agent/",
        oidc_client_secret=SecretStr("s3cret"),
        oidc_redirect_uris="https://ga.example.nl/login/oidc/callback",
    ))
    assert settings.oidc_client_id == "grocery-agent" and settings.oidc_admin_group == "grocery-agent-admin"
    assert settings.oidc_session_days == 7


def test_several_redirect_uris_are_split_on_comma_or_space():
    settings = Settings(**base(
        oidc_issuer="https://auth.example.nl/application/o/grocery-agent/",
        oidc_client_secret=SecretStr("s3cret"),
        oidc_redirect_uris="https://a.example/login/oidc/callback, https://b.example/login/oidc/callback",
    ))
    assert settings.oidc_redirect_uri_list == [
        "https://a.example/login/oidc/callback", "https://b.example/login/oidc/callback",
    ]


@pytest.mark.parametrize("missing", ["oidc_client_secret", "oidc_redirect_uris"])
def test_a_half_filled_oidc_block_is_refused(missing):
    values = base(
        oidc_issuer="https://auth.example.nl/application/o/grocery-agent/",
        oidc_client_secret=SecretStr("s3cret"),
        oidc_redirect_uris="https://ga.example.nl/login/oidc/callback",
    )
    values[missing] = "" if missing == "oidc_redirect_uris" else None
    with pytest.raises(ValidationError, match="required too"):
        Settings(**values)


def test_an_emptied_out_client_id_is_also_refused():
    with pytest.raises(ValidationError, match="required too"):
        Settings(**base(
            oidc_issuer="https://auth.example.nl/application/o/grocery-agent/",
            oidc_client_secret=SecretStr("s3cret"),
            oidc_redirect_uris="https://ga.example.nl/login/oidc/callback",
            oidc_client_id="",
        ))


def test_the_issuer_must_be_https_except_localhost():
    with pytest.raises(ValidationError, match="must be an https URL"):
        Settings(**base(
            oidc_issuer="http://auth.example.nl/", oidc_client_secret=SecretStr("s"),
            oidc_redirect_uris="https://ga.example.nl/login/oidc/callback",
        ))
    Settings(**base(  # http allowed for localhost, for local testing
        oidc_issuer="http://localhost:9000/application/o/grocery-agent/", oidc_client_secret=SecretStr("s"),
        oidc_redirect_uris="http://localhost:8090/login/oidc/callback",
    ))


@pytest.mark.parametrize("uri", [
    "https://ga.example.nl/wrong/path", "http://ga.example.nl/login/oidc/callback", "not-a-url/login/oidc/callback",
])
def test_a_redirect_uri_must_be_https_and_end_in_the_callback_path(uri):
    with pytest.raises(ValidationError, match="login/oidc/callback"):
        Settings(**base(
            oidc_issuer="https://auth.example.nl/application/o/grocery-agent/",
            oidc_client_secret=SecretStr("s"), oidc_redirect_uris=uri,
        ))


def test_the_internal_url_must_be_http_or_https():
    with pytest.raises(ValidationError, match="OIDC_INTERNAL_URL"):
        Settings(**base(
            oidc_issuer="https://auth.example.nl/application/o/grocery-agent/",
            oidc_client_secret=SecretStr("s"), oidc_redirect_uris="https://ga.example.nl/login/oidc/callback",
            oidc_internal_url="authentik:9000",
        ))


@pytest.mark.parametrize("days", [0, 91])
def test_session_days_must_be_in_range(days):
    with pytest.raises(ValidationError, match="OIDC_SESSION_DAYS"):
        Settings(**base(
            oidc_issuer="https://auth.example.nl/application/o/grocery-agent/",
            oidc_client_secret=SecretStr("s"), oidc_redirect_uris="https://ga.example.nl/login/oidc/callback",
            oidc_session_days=days,
        ))
