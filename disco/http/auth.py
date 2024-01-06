import logging

from pyramid.authentication import extract_http_basic_credentials
from pyramid.authorization import ACLHelper, Authenticated, Everyone

from disco.models import AuthenticationToken
from disco.utils.auth import get_valid_token_by_id

log = logging.getLogger(__name__)


def get_token_principals(auth_token: AuthenticationToken | None) -> list[str] | None:
    if auth_token is None:
        return None
    principals = [
        "auth_token",
        f"auth_token:{auth_token.id}",
    ]
    return principals


def get_auth_token_for_request(request) -> AuthenticationToken | None:
    if request.identity is None:
        return None
    return request.identity["auth_token"]


def get_auth_token_id_for_request(request) -> str | None:
    auth_params = extract_http_basic_credentials(request)
    if auth_params is None:
        return None
    return auth_params.username


class SecurityPolicy:
    def permits(self, request, context, permission):
        principals = [Everyone]
        identity = self.identity(request)
        if identity is not None:
            principals.append(Authenticated)
            principals += get_token_principals(identity["auth_token"])
        return ACLHelper().permits(context, principals, permission)

    def identity(self, request):
        token_id = get_auth_token_id_for_request(request)
        auth_token = get_valid_token_by_id(request.dbsession, token_id)
        if auth_token is None:
            return None
        return dict(
            auth_token=auth_token,
            auth_token_log_id=auth_token.log_id,
        )

    def authenticated_userid(self, request):
        if request.identity is None:
            return None
        return request.identity["auth_token"].id

    def remember(self, request, userid, **kw):
        pass  # no op

    def forget(self, request, **kw):
        pass  # no op


def includeme(config):
    config.set_security_policy(SecurityPolicy())
    config.add_request_method(get_auth_token_for_request, "auth_token", reify=True)
