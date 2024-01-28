from cornice import Service

from disco.http.contexts.syslog import ListContext
from disco.http.validation.common import bound_schema_validator
from disco.http.validation.syslog import SyslogUrlSchema
from disco.utils.syslog import add_syslog_url, get_syslog_urls, remove_syslog_url

syslog_service = Service(
    name="syslog_service",
    path="/syslog",
    http_cache=(None, dict(private=True)),
    factory=ListContext,
)


@syslog_service.get(
    permission="get_syslog_urls",
)
def projects_service_get(request):
    return dict(
        urls=get_syslog_urls(request.dbsession),
    )


@syslog_service.post(
    schema=SyslogUrlSchema(),
    validators=(bound_schema_validator,),
    permission="add_remove_syslog_url",
)
def projects_service_post(request):
    if request.validated["action"] == "add":
        urls = add_syslog_url(
            request.dbsession, request.validated["url"], request.api_key
        )
    else:
        assert request.valdated["action"] == "remove"
        urls = remove_syslog_url(
            request.dbsession, request.validated["url"], request.api_key
        )
    return dict(
        urls=urls,
    )
