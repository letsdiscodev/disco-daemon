from cornice import Service

root_service = Service(
    name="root_service",
    path="/",
    http_cache=(None, dict(private=True)),
)


@root_service.get()
def root_service_get(request):
    return dict(disco=True)
