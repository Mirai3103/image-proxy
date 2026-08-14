import datetime as dt
import ipaddress
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from PIL import Image

import image_proxy.mitm_script


class _FakeOrigin:
    stopped = False


class _FakeOriginThread:
    pass


class _FakeProxyProcess:
    terminated = False
    killed = False
    waited = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        assert timeout is not None
        self.waited = True
        return 0

    def kill(self) -> None:
        self.killed = True


class _FakeLogFile:
    closed = False

    def close(self) -> None:
        self.closed = True


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port: int, ca_path: Path | None = None) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                if ca_path is None or ca_path.exists():
                    return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"proxy port {port} did not become ready")


def origin_certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "origin.crt", directory / "origin.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def start_origin(directory: Path, tls: bool, body: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/manga/page.jpg":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls:
        cert_path, key_path = origin_certificate(directory)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_origin(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize("failure_point", ["log_open", "popen"])
def test_live_proxy_cleans_origin_after_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    origin = _FakeOrigin()
    origin_thread = _FakeOriginThread()
    fake_log_file = _FakeLogFile()
    real_open = Path.open

    def fake_start_origin(directory: Path, tls: bool, body: bytes):
        return origin, origin_thread

    def fake_stop_origin(server, thread) -> None:
        assert server is origin
        assert thread is origin_thread
        origin.stopped = True

    def fake_open(path: Path, *args, **kwargs):
        if path.name != "mitmdump.log":
            return real_open(path, *args, **kwargs)
        if failure_point == "log_open":
            raise RuntimeError("log open failed")
        return fake_log_file

    def fake_popen(*args, **kwargs):
        raise RuntimeError("popen failed")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "start_origin", fake_start_origin)
    monkeypatch.setattr(module, "stop_origin", fake_stop_origin)
    monkeypatch.setattr("pathlib.Path.open", fake_open)
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match=failure_point.replace("_", " ")):
        test_live_proxy_processes_then_serves_cache_without_origin(tmp_path, False)

    assert origin.stopped
    if failure_point == "popen":
        assert fake_log_file.closed


def test_live_proxy_curl_timeout_triggers_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _FakeOrigin()
    origin.server_port = free_port()
    origin_thread = _FakeOriginThread()
    process = _FakeProxyProcess()
    fake_log_file = _FakeLogFile()
    real_open = Path.open

    def fake_start_origin(directory: Path, tls: bool, body: bytes):
        return origin, origin_thread

    def fake_stop_origin(server, thread) -> None:
        assert server is origin
        assert thread is origin_thread
        origin.stopped = True

    def fake_wait_for_port(port: int, ca_path: Path | None = None) -> None:
        return

    def fake_popen(*args, **kwargs):
        return process

    def fake_run(args, **kwargs):
        timeout = kwargs.get("timeout")
        if timeout is None:
            raise AssertionError("curl subprocess is unbounded")
        raise subprocess.TimeoutExpired(args, timeout)

    def fake_open(path: Path, *args, **kwargs):
        if path.name == "mitmdump.log":
            return fake_log_file
        return real_open(path, *args, **kwargs)

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "start_origin", fake_start_origin)
    monkeypatch.setattr(module, "stop_origin", fake_stop_origin)
    monkeypatch.setattr(module, "wait_for_port", fake_wait_for_port)
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr("pathlib.Path.open", fake_open)

    with pytest.raises(subprocess.TimeoutExpired):
        test_live_proxy_processes_then_serves_cache_without_origin(tmp_path, False)

    assert origin.stopped
    assert process.terminated
    assert process.waited
    assert fake_log_file.closed


@pytest.mark.smoke
@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
def test_live_proxy_processes_then_serves_cache_without_origin(
    tmp_path: Path, tls: bool
) -> None:
    source_buffer = BytesIO()
    Image.new("RGB", (320, 480), "white").save(source_buffer, "JPEG")
    source = source_buffer.getvalue()
    origin, origin_thread = start_origin(tmp_path, tls, source)
    origin_stopped = False
    log_file = None
    process = None
    try:
        proxy_port = free_port()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "proxy": {"host": "127.0.0.1", "port": proxy_port},
                    "matching": {"domains": ["127.0.0.1"], "url_regex": []},
                    "processing": {
                        "text": "UPSCALED",
                        "jpeg_quality": 90,
                        "webp_quality": 90,
                        "max_source_mb": 30,
                        "max_pixels": 80_000_000,
                        "workers": 2,
                    },
                    "cache": {
                        "directory": str(tmp_path / "cache"),
                        "ttl_hours": 1,
                        "max_size_gb": 1,
                        "low_watermark_ratio": 0.9,
                        "cleanup_interval_minutes": 10,
                        "eviction_batch_size": 25,
                    },
                }
            )
        )
        confdir = tmp_path / "mitmproxy"
        script_path = Path(image_proxy.mitm_script.__file__).resolve()
        executable = shutil.which("mitmdump")
        assert executable is not None
        log_file = (tmp_path / "mitmdump.log").open("wb")
        process = subprocess.Popen(
            [
                executable,
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                str(proxy_port),
                "--set",
                f"confdir={confdir}",
                "--set",
                "ssl_insecure=true",
                "--set",
                f"image_proxy_config={config_path}",
                "-s",
                str(script_path),
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        ca_path = confdir / "mitmproxy-ca-cert.pem" if tls else None
        wait_for_port(proxy_port, ca_path)
        scheme = "https" if tls else "http"
        url = f"{scheme}://127.0.0.1:{origin.server_port}/manga/page.jpg"
        first, second = tmp_path / "first.jpg", tmp_path / "second.jpg"
        curl = [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--noproxy",
            "",
            "--proxy",
            f"http://127.0.0.1:{proxy_port}",
        ]
        if tls:
            curl.extend(["--cacert", str(ca_path)])
        subprocess.run([*curl, url, "--output", str(first)], check=True, timeout=10)
        stop_origin(origin, origin_thread)
        origin_stopped = True
        subprocess.run([*curl, url, "--output", str(second)], check=True, timeout=10)

        assert first.read_bytes() == second.read_bytes()
        assert first.read_bytes() != source
        with Image.open(first) as processed:
            assert processed.format == "JPEG"
            processed_rgb = processed.convert("RGB")
            pixels = processed_rgb.get_flattened_data()
            red_pixels = sum(
                1
                for red, green, blue in pixels
                if red > 180 and green < 120 and blue < 120
            )
            assert red_pixels > 50
    finally:
        primary_exception = sys.exc_info()[1]
        cleanup_error: BaseException | None = None
        if not origin_stopped:
            try:
                stop_origin(origin, origin_thread)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if process is not None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if log_file is not None:
            try:
                log_file.close()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if primary_exception is None and cleanup_error is not None:
            raise cleanup_error
