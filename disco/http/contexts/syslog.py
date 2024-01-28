from pyramid.security import Allow


class ListContext:
    def __init__(self, request):
        pass

    @property
    def __acl__(self):
        return [
            (Allow, "api_key", "get_syslog_urls"),
            (Allow, "api_key", "add_remove_syslog_url"),
        ]
