from setuptools import find_packages, setup

setup(
    name="disco",
    version="0.0",
    description="disco",
    classifiers=[],
    author="",
    author_email="",
    url="",
    keywords="",
    packages=find_packages(exclude=["tests"]),
    include_package_data=True,
    zip_safe=False,
    extras_require={},
    install_requires=[],
    entry_points={
        "console_scripts": [
            "disco_worker=disco.worker:main",
            "disco_init=disco.scripts.init:main",
            "disco_add_disco_domain=disco.scripts.add_disco_domain:main",
            "disco_create_api_key=disco.scripts.create_api_key:main",
        ],
    },
)
