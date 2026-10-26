from __future__ import annotations

from pathlib import Path

import pytest

from cite.config import Principal, Settings, parse_key_acls


def settings(**overrides) -> Settings:
    values = {
        "corpus_dir": Path("data/corpus"),
        "index_path": Path("data/index.sqlite3"),
        "golden_path": Path("eval/golden.yaml"),
    }
    values.update(overrides)
    return Settings(**values)


def test_parse_key_acls_accepts_collections_and_wildcard() -> None:
    parsed = parse_key_acls("reader-a:handbook,support;reader-b:*")
    assert parsed == (
        ("reader-a", ("handbook", "support")),
        ("reader-b", ("*",)),
    )


@pytest.mark.parametrize(
    "raw, message",
    [
        ("no-delimiter", "must look like"),
        (":handbook", "empty key"),
        ("reader:Bad Name", "invalid collection"),
    ],
)
def test_parse_key_acls_rejects_malformed_input(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_key_acls(raw)


def test_auth_disabled_makes_the_local_caller_an_admin() -> None:
    principal = settings().principal(None, "ip:127.0.0.1")
    assert principal.role == "admin"
    assert principal.collections is None
    assert principal.can_read("anything")


def test_keys_keep_full_access_when_no_acl_map_is_configured() -> None:
    principal = settings(api_keys=("key-a",)).principal("key-a", "key:abc")
    assert principal.role == "reader"
    assert principal.collections is None


def test_acl_map_denies_unlisted_collections_and_unknown_keys() -> None:
    configured = settings(
        api_keys=("reader", "admin", "unknown"),
        admin_keys=("admin",),
        key_acls=(("reader", ("handbook",)),),
    )
    reader = configured.principal("reader", "key:r")
    assert reader.can_read("handbook")
    assert not reader.can_read("legal")

    unknown = configured.principal("unknown", "key:u")
    assert unknown.collections == frozenset()
    assert not unknown.can_read("handbook")

    admin = configured.principal("admin", "key:a")
    assert admin.role == "admin"
    assert admin.can_read("legal")


def test_wildcard_grant_reads_every_collection_without_admin() -> None:
    configured = settings(
        api_keys=("reader",),
        admin_keys=("admin",),
        key_acls=(("reader", ("*",)),),
    )
    reader = configured.principal("reader", "key:r")
    assert reader.collections is None
    assert reader.role == "reader"
    with pytest.raises(PermissionError):
        reader.require_admin()


def test_index_path_for_named_collection_follows_the_default_suffix() -> None:
    sqlite = settings()
    assert sqlite.index_path_for("support") == Path("data/collections/support.sqlite3")
    json_store = settings(index_path=Path("data/index.json"))
    assert json_store.index_path_for("support") == Path("data/collections/support.json")


def test_collection_paths_prefers_the_configured_format(tmp_path) -> None:
    directory = tmp_path / "collections"
    directory.mkdir()
    (directory / "support.json").write_text("{}", encoding="utf-8")
    (directory / "support.sqlite3").write_bytes(b"")
    (directory / "legal.json").write_text("{}", encoding="utf-8")
    configured = settings(collections_dir=directory)
    names = {path.name for path in configured.collection_paths()}
    assert names == {"support.sqlite3", "legal.json"}
    assert configured.collection_names() == ["default", "legal", "support"]


def test_principal_permission_error_message() -> None:
    with pytest.raises(PermissionError, match="admin"):
        Principal(identity="key:r", collections=frozenset()).require_admin()
