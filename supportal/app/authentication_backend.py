import json
import logging

import jwt
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in
from django.core.cache import cache
from jwt import algorithms
from rest_framework.authentication import (
    BaseAuthentication,
    exceptions,
    get_authorization_header,
)
from rest_framework.request import Request

from supportal.app.models import APIKey

User = get_user_model()

JWT_VERIFY_OPTS = {
    "verify_signature": True,
    "verify_exp": True,
    "verify_iat": True,
    "verify_aud": False,  # we verify aud manually for id tokens
    "verify_iss": True,
    "require_exp": True,
}
__COGNITO_USER_POOL_JWKS = None


class CognitoJWTAuthentication(BaseAuthentication):
    def authenticate(self, request):
        """Authenticate requests with JWTs from AWS Cognito

        We accept two different types of tokens:

        'id' tokens contain user-identifying information and originate from the
        authorization_code or implicit OAuth2 grants. In this case we return
        the User associated with the token. If the User doesn't exist, we create
        one since we trust Cognito as the source of truth for users in our system.

        'access' tokens do not contain user information and are generated by the
        client_credentials OAuth2 grant. These are not tied to a user but rather
        have 'scopes' that determine what the token can do. We don't support any
        scopes at this point, so we grant this token the privileges of the user
        associated with the API key in the database.
        """
        token = _get_bearer_token(request)
        if not token:
            return None

        token_data = validate_jwt(token)
        token_use = token_data.get("token_use")
        if token_use == "id":
            _validate_id_token_data(token_data)
            username = token_data.get("cognito:username")
            email = token_data.get("email")
            email_verified = token_data.get("email_verified")
            if not email_verified or not email or not username:
                raise exceptions.AuthenticationFailed("Invalid user state")
            try:
                user = User.objects.get_by_natural_key(username)
            except User.DoesNotExist:
                raise exceptions.AuthenticationFailed("User does not exist")
        elif token_use == "access":
            client_id = token_data.get("client_id")

            if not client_id:
                raise exceptions.AuthenticationFailed("Invalid access token")
            try:
                key = APIKey.objects.get(pk=client_id)
                user = key.user
            except APIKey.DoesNotExist:
                raise exceptions.AuthenticationFailed("Invalid access token")
        else:
            raise exceptions.AuthenticationFailed(f"Unknown token_use: {token_use}")

        if not user.is_active:
            raise exceptions.AuthenticationFailed("User deactivated")

        # Only allow admins to impersonate users
        if (
            user.is_admin
            and user.impersonated_user is not None
            and user.id != user.impersonated_user.id
        ):
            # Intentionally don't fire the user_logged_in signal when impersonating
            return user.impersonated_user, token_data

        user_logged_in.send(sender=user.__class__, request=request, user=user)
        return user, token_data

    def authenticate_header(self, request):
        """Value of the `WWW-Authenticate` header in a `401 Unauthenticated` response

        If this is not supplied, auth failures will return `403 Permission Denied` responses.
        """
        return "Bearer: realm=api"


def get_jwks():
    global __COGNITO_USER_POOL_JWKS
    if not __COGNITO_USER_POOL_JWKS:
        cached_val = cache.get("cognito_user_pool_jwks")
        if cached_val is not None:
            __COGNITO_USER_POOL_JWKS = cached_val
        else:
            res = requests.get(
                f"{settings.COGNITO_USER_POOL_URL}/.well-known/jwks.json"
            )
            res.raise_for_status()
            payload = res.json()
            cache.set("cognito_user_pool_jwks", payload)
            __COGNITO_USER_POOL_JWKS = payload
        if not __COGNITO_USER_POOL_JWKS:
            raise Exception("We did not get any JWKs from Cognito.")
    return __COGNITO_USER_POOL_JWKS


def validate_jwt(token):
    """Validate the signature of the JWT token from Cognito"""
    jwks = get_jwks()
    try:
        return jwt.decode(
            token,
            _get_public_key(token, jwks),
            issuer=settings.COGNITO_USER_POOL_URL,
            algorithms=["RS256"],
            options=JWT_VERIFY_OPTS,
        )
    except jwt.exceptions.PyJWTError:
        # return a generic error message and log the exception, as this might
        # mean that someone is tampering with tokens
        logging.exception("Error decoding JWT token")
        raise exceptions.AuthenticationFailed("Invalid token")


def _get_bearer_token(request: Request):
    """Extract the Bearer token from the 'Authentication' header"""
    return _get_auth_token(request, b"bearer")


def _get_auth_token(request: Request, auth_type):
    """Extract the Bearer token from the 'Authentication' header"""
    header = get_authorization_header(request)
    split = header.split()
    if len(split) == 0:
        return None
    elif len(split) != 2 or split[0].lower() != auth_type:
        raise exceptions.AuthenticationFailed(
            f"Invalid auth header. Format should be '{auth_type} <token>'"
        )
    else:
        return split[1]


def _get_public_key(token, jwks):
    """Find the appropriate JSON Web Key (JWK) to verify this token using RSA"""
    header = jwt.get_unverified_header(token)
    key_id = header.get("kid")

    alg = header.get("alg")
    if not key_id:
        raise exceptions.AuthenticationFailed("Invalid token missing 'kid' header")
    if alg != "RS256":
        raise exceptions.AuthenticationFailed(f"Unsupported 'alg' header {alg}")

    try:
        key = [k for k in jwks["keys"] if k["kid"] == key_id][0]
    except IndexError:
        # If there is an IndexError, the list constructed above probably empty.
        # That likely means the token was generated against a different
        # backend, so the header kid does not appear in our list of jwks keys
        logging.exception("Header key id not present in passed jwks keys")
        raise exceptions.AuthenticationFailed("Forbidden")

    return algorithms.RSAAlgorithm.from_jwk(json.dumps(key))


def _validate_id_token_data(token_data):
    """Validate additional claims on a decoded 'id' token"""
    aud = token_data.get("aud")
    if not aud or aud != settings.COGNITO_USER_LOGIN_CLIENT_ID:
        raise exceptions.AuthenticationFailed("Invalid id token")
