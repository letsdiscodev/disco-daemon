import logging

from pyramid.authentication import extract_http_basic_credentials
from pyramid.authorization import ACLHelper, Authenticated, Everyone

from disco.models import ApiKey
from disco.utils.auth import get_valid_api_key_by_id

log = logging.getLogger(__name__)


def get_api_key_principals(api_key: ApiKey | None) -> list[str] | None:
    if api_key is None:
        return None
    principals = [
        "api_key",
        f"api_key:{api_key.id}",
    ]
    return principals


def get_api_key_for_request(request) -> ApiKey | None:
    if request.identity is None:
        return None
    return request.identity["api_key"]


def get_api_key_id_for_request(request) -> str | None:
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
            principals += get_api_key_principals(identity["api_key"])
        return ACLHelper().permits(context, principals, permission)

    def identity(self, request):
        api_key_id = get_api_key_id_for_request(request)
        api_key = get_valid_api_key_by_id(request.dbsession, api_key_id)
        if api_key is None:
            return None
        return dict(
            api_key=api_key,
            api_key_log_id=api_key.log_id,
        )

    def authenticated_userid(self, request):
        if request.identity is None:
            return None
        return request.identity["api_key"].id

    def remember(self, request, userid, **kw):
        pass  # no op

    def forget(self, request, **kw):
        pass  # no op


def includeme(config):
    config.set_security_policy(SecurityPolicy())
    config.add_request_method(get_api_key_for_request, "api_key", reify=True)
