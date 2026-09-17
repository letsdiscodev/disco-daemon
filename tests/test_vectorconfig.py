import pytest

from disco.utils import vectorconfig as vc


@pytest.mark.parametrize(
    "url,host,port,tls",
    [
        (
            "syslog://logs.papertrailapp.com:28576",
            "logs.papertrailapp.com",
            28576,
            False,
        ),
        (
            "syslog+tls://logs4.papertrailapp.com:27257",
            "logs4.papertrailapp.com",
            27257,
            True,
        ),
        ("syslog://10.0.0.5:5514", "10.0.0.5", 5514, False),
        (
            "syslog+tls://srcf6ryd-309.syslog.axiom.co:6514",
            "srcf6ryd-309.syslog.axiom.co",
            6514,
            True,
        ),
        ("syslog://h:1", "h", 1, False),
        ("syslog://h:65535", "h", 65535, False),
    ],
)
def test_parse_syslog_url_accepts(url, host, port, tls):
    d = vc.parse_syslog_url(url)
    assert (d.host, d.port, d.tls) == (host, port, tls)
    assert d.address == f"{host}:{port}"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "syslog://",
        "syslog://host",  # missing port
        "syslog://host:",
        "syslog://host:0",
        "syslog://host:65536",
        "syslog://host:12a",
        "syslog://:514",
        "syslog:///514",
        "syslogs://host:514",
        "tcp://host:514",
        "http://host:514",
        "syslog+ssl://host:514",
        "syslog://user:pw@host:514",  # userinfo
        "syslog://host:514/path",
        "syslog://host:514?sd=x",  # F9 query string (no key)
        "syslog://host:514?api_key=abc",  # F9 query string (with key)
        "syslog://host?api_key=abc:514",
        "syslog://host:514#frag",
        "syslog://host:514 ",
        " syslog://host:514",
        "syslog://host:514\n",
        "syslog://-host:514",
        "syslog://host-:514",
        "syslog://ho st:514",
        "SYSLOG://host:514",
    ],
)
def test_parse_syslog_url_rejects(url):
    with pytest.raises(vc.InvalidSyslogUrl):
        vc.parse_syslog_url(url)


def test_destination_id_stable_and_type_sensitive():
    a = vc.destination_id("syslog://h:1", "GLOBAL")
    assert a == vc.destination_id("syslog://h:1", "GLOBAL")
    assert a != vc.destination_id("syslog://h:1", "CORE")
    assert a != vc.destination_id("syslog://h:2", "GLOBAL")
    assert len(a) == 12


def test_render_udp_global():
    cfg = vc.render_syslog_config("syslog://10.0.0.5:5514", "GLOBAL")
    assert "type: docker_logs" in cfg
    assert "type: dedupe" in cfg and "match: [container_id, timestamp, message]" in cfg
    assert "inputs: [dedupe]" in cfg
    assert "mode: udp" in cfg
    assert "address: 10.0.0.5:5514" in cfg
    assert "tls:" not in cfg
    assert "method: bytes" in cfg
    assert "when_full: drop_newest" in cfg
    assert 'downcase(to_string(.label."disco.log.exclude") ?? "") != "true"' in cfg
    assert 'downcase(to_string(.label."disco.run") ?? "") != "true"' in cfg
    assert "disco.log.core" not in cfg
    assert (
        '"%Y-%m-%dT%H:%M:%SZ"' in cfg and "%.3f" not in cfg
    )  # whole seconds, as logspout
    assert 'get_env_var("SYSLOG_HOSTNAME") ?? "-"' in cfg
    assert 'hostname = ""' in cfg  # no host given: env fallback
    cfg2 = vc.render_syslog_config(
        "syslog://10.0.0.5:5514", "GLOBAL", disco_host="my.host"
    )
    assert 'hostname = "my.host"' in cfg2
    assert vc.config_hash(cfg) != vc.config_hash(cfg2)
    assert 'hostname = "a\\"b"' in vc.render_syslog_config(
        "syslog://h:1", "GLOBAL", disco_host='a"b'
    )
    assert "truncate(app, 48)" in cfg
    assert 'if .stream == "stderr" { 3 } else { 6 }' in cfg
    assert "codec: raw_message" in cfg


def test_render_tls_core():
    cfg = vc.render_syslog_config("syslog+tls://logs4.papertrailapp.com:27257", "CORE")
    assert "mode: tcp" in cfg
    assert "address: logs4.papertrailapp.com:27257" in cfg
    assert (
        "enabled: true" in cfg
        and "verify_certificate: true" in cfg
        and "verify_hostname: true" in cfg
    )
    assert "method: newline_delimited" in cfg
    assert "when_full: block" in cfg
    # CORE keeps only core containers AND still honours the exclude label (F4)
    assert (
        'downcase(to_string(.label."disco.log.core") ?? "") == "true" && downcase(to_string(.label."disco.log.exclude") ?? "") != "true"'
        in cfg
    )


def test_render_rejects_bad_url_and_type():
    with pytest.raises(vc.InvalidSyslogUrl):
        vc.render_syslog_config("syslog://host:514?api_key=x", "GLOBAL")
    with pytest.raises(ValueError):
        vc.render_syslog_config("syslog://host:514", "OTHER")  # type: ignore[arg-type]


def test_buffer_size_floor_and_override():
    cfg = vc.render_syslog_config("syslog://h:1", "GLOBAL", buffer_bytes=10)
    assert f"max_size: {vc.MIN_DISK_BUFFER_BYTES}" in cfg
    cfg = vc.render_syslog_config("syslog://h:1", "GLOBAL", buffer_bytes=2**30)
    assert f"max_size: {2**30}" in cfg
    cfg = vc.render_syslog_config("syslog://h:1", "GLOBAL")
    assert f"max_size: {vc.DEFAULT_DESTINATION_BUFFER_BYTES}" in cfg


def test_config_hash_changes_with_content():
    a = vc.render_syslog_config("syslog://h:1", "GLOBAL")
    b = vc.render_syslog_config("syslog://h:2", "GLOBAL")
    c = vc.render_syslog_config("syslog://h:1", "CORE")
    assert len({vc.config_hash(a), vc.config_hash(b), vc.config_hash(c)}) == 3
    assert vc.config_hash(a) == vc.config_hash(
        vc.render_syslog_config("syslog://h:1", "GLOBAL")
    )


def test_render_streaming():
    cfg = vc.render_streaming_config(12345)
    assert "type: dedupe" in cfg and "inputs: [dedupe]" in cfg
    assert "address: disco:12345" in cfg
    assert "mode: tcp" in cfg
    assert "codec: json" in cfg
    assert "method: newline_delimited" in cfg
    assert "when_full: block" in cfg
    for key in ('"container"', '"labels"', '"timestamp"', '"message"'):
        assert key in cfg
    assert "disco.log.exclude" not in cfg  # streaming shows everything, as logspout did
    assert "disco.run" not in cfg
    assert '"ts": format_timestamp(.timestamp, "%Y-%m-%dT%H:%M:%S%.3fZ")' in cfg
    with pytest.raises(ValueError):
        vc.render_streaming_config(0)


def test_newline_escape_is_literal_backslash_n():
    # the yaml must carry replace(msg, "\n", "\\n") so vector turns a real newline into
    # the two characters backslash + n (one frame per record)
    cfg = vc.render_syslog_config("syslog://h:1", "GLOBAL")
    assert 'replace(msg, "\\n", "\\\\n")' in cfg


def test_spec_revision_in_config():
    cfg = vc.render_syslog_config("syslog://h:1", "GLOBAL")
    assert f"service spec revision {vc.SERVICE_SPEC_REVISION}" in cfg
    assert (
        f"service spec revision {vc.SERVICE_SPEC_REVISION}"
        in vc.render_streaming_config(1)
    )


def test_data_dir_per_collector():
    a = vc.render_syslog_config("syslog://h:1", "GLOBAL", disco_host="x")
    b = vc.render_syslog_config("syslog://h:1", "GLOBAL", disco_host="y")
    da = [line for line in a.splitlines() if line.startswith("data_dir:")][0]
    db = [line for line in b.splitlines() if line.startswith("data_dir:")][0]
    assert da.startswith(f"data_dir: {vc.VECTOR_DATA_DIR}/") and da != db
    assert a == vc.render_syslog_config("syslog://h:1", "GLOBAL", disco_host="x")
    assert vc.render_streaming_config(5).count("data_dir: /var/lib/vector/") == 1


def test_image_pin():
    assert vc.VECTOR_IMAGE == "timberio/vector:0.58.0-alpine"
    assert (
        vc.VECTOR_IMAGE_DIGEST.startswith("sha256:")
        and len(vc.VECTOR_IMAGE_DIGEST) == 71
    )
