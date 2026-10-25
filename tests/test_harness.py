import pytest
from django.conf import settings
from django.db import connection


@pytest.mark.django_db
def test_postgis_available() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT PostGIS_Version()")
        (version,) = cursor.fetchone()
    assert isinstance(version, str) and version


def test_timezone_setting() -> None:
    assert settings.TIME_ZONE == "Europe/Paris"
    assert settings.USE_TZ is True
