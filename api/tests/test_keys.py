from app.auth.keys import generate_key, hash_secret, parse_key, verify_secret

PEPPER = "test-pepper"


def test_generated_key_parses_back_to_its_parts() -> None:
    key = generate_key("dev")

    assert key.prefix == f"om_dev_{key.key_id}"
    assert key.raw == f"{key.prefix}_{key.secret}"
    assert parse_key(key.raw) == (key.key_id, key.secret)


def test_two_generations_differ() -> None:
    assert generate_key("dev").raw != generate_key("dev").raw


def test_parse_key_rejects_malformed_keys() -> None:
    assert parse_key("om_dev_short_x") is None
    assert parse_key("") is None
    assert parse_key("om_dev_" + "a" * 12) is None
    assert parse_key(generate_key("dev").raw + "x") is None


def test_verify_secret_accepts_the_secret_and_rejects_everything_else() -> None:
    key = generate_key("dev")
    secret_hash = hash_secret(key.secret, PEPPER)

    assert secret_hash.startswith("$argon2id$")
    assert key.secret not in secret_hash
    assert verify_secret(secret_hash, key.secret, PEPPER)
    assert not verify_secret(secret_hash, key.secret, "wrong-pepper")
    assert not verify_secret(secret_hash, generate_key("dev").secret, PEPPER)
    assert not verify_secret("not-a-hash", key.secret, PEPPER)
