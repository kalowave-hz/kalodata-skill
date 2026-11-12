"""Exercise image search through the CLI and a real multipart HTTP request."""

import json

import pytest

from kalodata import api
from kalodata.cli import main
from kalodata.config import Config


ENDPOINT = "/tiktok/product/picsearch/one-step"
MATCH = {"product_id": "123", "product_name": "Matching product", "revenue": "12.5",
         "sales_volumn": "3", "unit_price": "4.5"}


def test_picsearch_upload_and_table(env, image_file, capsys):
    env.responses[ENDPOINT] = {"success": True, "data": [MATCH]}
    assert main(["product", "picsearch", str(image_file), "--fields", "unit_price"]) == 0
    out = capsys.readouterr().out
    assert "products[1]{product_id,product_name,revenue,sales_volumn,unit_price}:" in out
    assert '"123",Matching product,12.5,3,4.5' in out
    assert "kalo product detail" in out

    path, headers, body = env.requests[0]
    assert path == ENDPOINT
    assert headers["x-api-key"] == "test-key"
    assert "secret-key" not in headers
    assert headers["source-type"] == "SKILL"
    assert "x-user-id" not in headers
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    assert set(body) == {"pic", "req"}
    assert body["pic"] == {"filename": "product.png", "content_type": "image/png",
                           "content": image_file.read_bytes()}
    assert json.loads(body["req"]) == {
        "region": "US", "language": "en-US", "currency": "USD", "date_range": "last30Day",
        "page_number": 1, "page_size": 20, "sort_field": {"field": "revenue", "type": "DESC"},
    }


def test_picsearch_flags_and_json(env, image_file, capsys):
    env.responses[ENDPOINT] = {"success": True, "data": {"data": [MATCH]}}
    assert main([
        "product", "picsearch", str(image_file), "--region", "GB", "--range", "last7Day",
        "--lang", "zh-CN", "--currency", "GBP", "--page", "2", "--limit", "5",
        "--sort", "sales_volumn", "--asc", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == [{**MATCH, "revenue": 12.5,
                                                  "sales_volumn": 3, "unit_price": 4.5}]
    assert json.loads(env.requests[0][2]["req"]) == {
        "region": "GB", "language": "zh-CN", "currency": "GBP", "date_range": "last7Day",
        "page_number": 2, "page_size": 5, "sort_field": {"field": "sales_volumn", "type": "ASC"},
    }


@pytest.mark.parametrize("case", ["missing", "directory", "empty", "text"])
def test_picsearch_invalid_file_does_not_query(env, tmp_path, capsys, case):
    image = tmp_path / "product.jpg"
    if case == "directory":
        image.mkdir()
    elif case == "empty":
        image.touch()
    elif case == "text":
        image = tmp_path / "product.txt"
        image.write_text("not a picture")
    assert main(["product", "picsearch", str(image)]) == 2
    assert "error:" in capsys.readouterr().out
    assert env.requests == []


@pytest.mark.parametrize("flags", [
    ["--region", "INVALID"], ["--range", "last90Day"], ["--sort", "invalid"],
    ["--fields", "invalid"],
])
def test_picsearch_invalid_search_does_not_query(env, image_file, capsys, flags):
    assert main(["product", "picsearch", str(image_file), *flags]) == 2
    assert "error:" in capsys.readouterr().out
    assert env.requests == []


def test_picsearch_requires_one_image(env, image_file, capsys):
    assert main(["product", "picsearch"]) == 2
    assert "requires <image_path>" in capsys.readouterr().out
    assert main(["product", "picsearch", str(image_file), str(image_file)]) == 2
    assert "takes at most 1" in capsys.readouterr().out
    assert env.requests == []


def test_picsearch_missing_key_does_not_query(env, image_file, monkeypatch, capsys):
    monkeypatch.delenv("KALODATA_API_KEY")
    assert main(["product", "picsearch", str(image_file)]) == 1
    assert "no KaloData API key" in capsys.readouterr().out
    assert env.requests == []


def test_picsearch_membership_failure(env, image_file, capsys):
    env.responses[ENDPOINT] = {
        "success": False, "code": "2016", "message": "Insufficient rights and interests",
    }
    assert main(["product", "picsearch", str(image_file)]) == 1
    assert "Insufficient rights and interests" in capsys.readouterr().out
    assert len(env.requests) == 1


def test_picsearch_empty_result(env, image_file, capsys):
    env.responses[ENDPOINT] = {"success": True, "data": []}
    assert main(["product", "picsearch", str(image_file)]) == 0
    assert "products: 0 results" in capsys.readouterr().out


def test_multipart_retry_preserves_upload(monkeypatch, image_file):
    attempts = []

    def send(url, data, headers, timeout, method):
        attempts.append((url, data, dict(headers)))
        if len(attempts) == 1:
            raise api._Retryable("temporary connection failure")
        return 200, b'{"success": true, "data": []}'

    monkeypatch.setattr(api, "_do_request", send)
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    assert api.request(
        Config(api_key="test-key"), ENDPOINT, {"req": "{}"},
        files={"pic": (image_file.name, image_file.read_bytes(), "image/png")},
        auth_header="X-API-Key",
    ) == []
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert attempts[0][2]["X-API-Key"] == "test-key"
    assert "secret-key" not in attempts[0][2]
    assert image_file.read_bytes() in attempts[0][1]
