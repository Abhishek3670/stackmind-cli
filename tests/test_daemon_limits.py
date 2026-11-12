"""Tests for daemon request size limits and concurrency throttling."""

import json
import socket
import threading
import time
import urllib.request
import urllib.error
import pytest
from validators.kernel.daemon.server import LocalDaemon, MAX_REQUEST_BODY_SIZE


def test_missing_content_length_returns_411(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        sock = socket.create_connection(daemon.address)
        try:
            req = b"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            sock.sendall(req)
            resp = sock.recv(1024)
            assert b"411" in resp
        finally:
            sock.close()


def test_negative_content_length_returns_400(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        sock = socket.create_connection(daemon.address)
        try:
            req = b"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: -5\r\n\r\n"
            sock.sendall(req)
            resp = sock.recv(1024)
            assert b"400" in resp
        finally:
            sock.close()


def test_oversized_body_returns_413(tmp_path, monkeypatch):
    # Set limit to 1024 bytes for test
    monkeypatch.setattr("validators.kernel.daemon.server.MAX_REQUEST_BODY_SIZE", 1024)
    with LocalDaemon(tmp_path) as daemon:
        sock = socket.create_connection(daemon.address)
        try:
            req = (
                f"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2048\r\n\r\n".encode("utf-8")
                + b"x" * 2048
            )
            sock.sendall(req)
            resp = b""
            while len(resp) < 200:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                resp += chunk
            assert b"413" in resp
            assert b"request body exceeds maximum limit" in resp
        finally:
            sock.close()


def test_concurrency_saturation_returns_503(tmp_path):
    # Limit daemon concurrency to 2 concurrent requests
    with LocalDaemon(tmp_path, max_concurrent_requests=2) as daemon:
        barrier = threading.Barrier(3)
        holding_sockets = []

        # Open 2 slow connections that hold the request
        for _ in range(2):
            s = socket.create_connection(daemon.address)
            # Send partial request headers so the worker thread is blocked waiting for body
            s.sendall(b"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 100\r\n\r\npartial")
            holding_sockets.append(s)

        time.sleep(0.1)

        # Third connection should be immediately rejected with 503
        rejected_sock = socket.create_connection(daemon.address)
        try:
            rejected_sock.sendall(b"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\n\r\n{}")
            resp = rejected_sock.recv(1024)
            assert b"503" in resp
            assert b"server overloaded" in resp
        finally:
            rejected_sock.close()
            for s in holding_sockets:
                s.close()


def test_valid_json_request_succeeds(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        req_data = json.dumps({
            "jsonrpc": "2.0",
            "method": "session.list",
            "params": {"auth_token": daemon.auth_token},
            "id": 1,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{daemon.url}/rpc",
            data=req_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {daemon.auth_token}"},
            method="POST",
        )
        with urllib.request.urlopen(req) as response:
            assert response.status == 200
            data = json.loads(response.read().decode("utf-8"))
            assert data.get("result") == []
