from src.core.config import Settings


def test_cors_origins_list_defaults_to_frontend_default_port() -> None:
    assert Settings(cors_origins="http://localhost:3000").cors_origins_list == [
        "http://localhost:3000"
    ]


def test_cors_origins_list_accepts_a_single_dynamically_resolved_origin() -> None:
    # docker-compose.yml sets CORS_ORIGINS from FRONTEND_HOST_PORT, which
    # scripts/docker-up.js may bump to any free port -- must not require
    # JSON array syntax for the common single-origin case.
    assert Settings(cors_origins="http://localhost:3003").cors_origins_list == [
        "http://localhost:3003"
    ]


def test_cors_origins_list_splits_multiple_comma_separated_origins() -> None:
    settings = Settings(cors_origins="http://localhost:3000, http://localhost:3003")
    assert settings.cors_origins_list == ["http://localhost:3000", "http://localhost:3003"]
