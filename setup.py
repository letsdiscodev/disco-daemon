from setuptools import find_packages, setup

setup(
    name="disco",
    version="0.0",
    description="disco",
    classifiers=[
        "Programming Language :: Python",
        "Framework :: Pyramid",
        "Topic :: Internet :: WWW/HTTP",
        "Topic :: Internet :: WWW/HTTP :: WSGI :: Application",
    ],
    author="",
    author_email="",
    url="",
    keywords="web pyramid pylons",
    packages=find_packages(exclude=["tests"]),
    include_package_data=True,
    zip_safe=False,
    extras_require={},
    install_requires=[],
    entry_points={
        "paste.app_factory": [
            "main = disco.http:main",
        ],
        "console_scripts": [
            "disco_worker=disco.worker:main",
            "disco_init=disco.scripts.init:main",
            "disco_add_disco_domain=disco.scripts.add_disco_domain:main",
            "disco_create_api_key=disco.scripts.create_api_key:main",
        ],
    },
)
