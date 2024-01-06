from pyramid.config import Configurator


def main(global_config, **settings):
    with Configurator(settings=settings) as config:
        config.include("disco.models")
        config.include("disco.http.auth")
        config.scan("disco.http.endpoints")
    return config.make_wsgi_app()
