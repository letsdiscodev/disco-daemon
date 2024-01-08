"""Script that runs when installing Disco on a server"""
import os
import sys

from pyramid.paster import setup_logging

from disco.utils.caddy import add_disco_domain


def usage(argv):
    cmd = os.path.basename(argv[0])
    print(
        "usage: %s <config_uri> <disco_domain>\n"
        '(example: "%s development.ini disco.example.com")' % (cmd, cmd)
    )
    sys.exit(1)


def main(argv=sys.argv):
    if len(argv) < 3:
        usage(argv)
    config_uri = argv[1]
    domain = argv[2]
    setup_logging(config_uri)
    add_disco_domain(domain)
