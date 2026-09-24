"""Connection settings for reading the training set off fedora-1.

Resolution order, highest priority first:

1. Keyword arguments — `DatabaseSettings(port=5432)`
2. `ML_DB_*` environment variables
3. `ml/.env`
4. The `fedora_readonly` target in `~/.dbt/profiles.yml`
5. The defaults below

Step 4 means that on a laptop already set up for dbt this needs no config at
all: the readonly credentials live in one place, the dbt profile, rather than
being copied into a second file that can drift. The SSH tunnel still has to be
open first (`ssh -N fedora-1-db`) — nothing here falls back to another database.
"""

from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ML_DIR = Path(__file__).resolve().parents[1]
DBT_PROFILES = Path.home() / ".dbt" / "profiles.yml"
DBT_PROFILE = "baby_data"
DBT_TARGET = "fedora_readonly"

# dbt profile keys that differ from our field names.
_DBT_KEY_TO_FIELD = {"dbname": "name"}


class DbtProfileSource(PydanticBaseSettingsSource):
    """Reads connection fields from one target of the local dbt profile.

    Missing file, profile or target all mean "contribute nothing" rather than
    an error, so the class still works on a machine without dbt configured.
    Values that are dbt Jinja (`{{ env_var(...) }}`) are skipped — only a plain
    literal target like `fedora_readonly` can be read without running dbt.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        profiles_path: Path = DBT_PROFILES,
        profile: str = DBT_PROFILE,
        target: str = DBT_TARGET,
    ) -> None:
        super().__init__(settings_cls)
        self._values = self._read(profiles_path, profile, target)

    @staticmethod
    def _read(profiles_path: Path, profile: str, target: str) -> dict[str, Any]:
        if not profiles_path.exists():
            return {}
        profiles = yaml.safe_load(profiles_path.read_text()) or {}
        output = profiles.get(profile, {}).get("outputs", {}).get(target, {})
        values = {}
        for key, value in output.items():
            if isinstance(value, str) and "{{" in value:
                continue
            values[_DBT_KEY_TO_FIELD.get(key, key)] = value
        return values

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return {
            name: self._values[name]
            for name in self.settings_cls.model_fields
            if name in self._values
        }


class DatabaseSettings(BaseSettings):
    """Where `ml.ml_sleep_training_set` lives. Defaults point at the tunnel."""

    host: str = "localhost"
    port: int = 5433
    name: str = "baby_data"
    user: str = "readonly"
    password: SecretStr = SecretStr("")

    model_config = SettingsConfigDict(
        env_prefix="ML_DB_",
        env_file=ML_DIR / ".env",
        # The dbt target also carries type/schema/threads; ignore them.
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            DbtProfileSource(settings_cls),
        )

    @property
    def url(self) -> str:
        """SQLAlchemy URL. Credentials are percent-encoded so a password
        containing @ : / ? # can't corrupt it."""
        user = quote(self.user, safe="")
        password = quote(self.password.get_secret_value(), safe="")
        return f"postgresql+psycopg2://{user}:{password}@{self.host}:{self.port}/{self.name}"
