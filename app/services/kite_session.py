"""Kite Connect session exchange and profile fetch."""

from dataclasses import dataclass

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException


@dataclass(frozen=True)
class KiteSessionResult:
    access_token: str
    public_token: str | None
    user_id: str | None
    user_name: str | None
    user_shortname: str | None
    email: str | None
    broker: str | None


def exchange_request_token(
    api_key: str,
    api_secret: str,
    request_token: str,
) -> KiteSessionResult:
    """
    Exchange one-time request_token for access_token.
    Raises KiteException on API errors.
    """
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data["access_token"]
    kite.set_access_token(access_token)

    profile = kite.profile()
    return KiteSessionResult(
        access_token=access_token,
        public_token=data.get("public_token"),
        user_id=profile.get("user_id"),
        user_name=profile.get("user_name"),
        user_shortname=profile.get("user_shortname"),
        email=profile.get("email"),
        broker=profile.get("broker"),
    )


def fetch_profile(api_key: str, access_token: str) -> dict:
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite.profile()


__all__ = ["KiteSessionResult", "exchange_request_token", "fetch_profile", "KiteException"]
