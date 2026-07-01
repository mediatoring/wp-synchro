"""Configuration loading and models."""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field


class OldServerConfig(BaseModel):
    ssh_host: str
    ssh_key: Optional[str] = None
    wp_root: str
    php_binary: str = "/usr/bin/php7.3"
    wpcli_path: str = "~/wp.phar"
    server_name_env: str = "cli"
    db_name: str = ""
    table_prefix: str = "wp_"


class NewServerConfig(BaseModel):
    ssh_host: str
    ssh_key: Optional[str] = None
    wp_root: str
    wpcli_binary: str = "wp"


class SyncDir(BaseModel):
    source: str
    dest: str


class SyncConfig(BaseModel):
    profile: str
    old_server: OldServerConfig
    new_server: NewServerConfig
    sync_dirs: List[SyncDir] = Field(default_factory=list)
    excluded_post_types: List[str] = Field(default_factory=lambda: [
        "revision", "nav_menu_item", "custom_css",
        "customize_changeset", "oembed_cache", "user_request", "wp_block",
    ])
    batch_limit: int = 50
    state_dir: str = "~/.wp-synchro"
    temp_dir: str = "/tmp/wp-synchro-transfer"


_config: Optional[SyncConfig] = None


def get_config() -> SyncConfig:
    global _config
    if _config is None:
        path = os.environ.get("WP_SYNCHRO_CONFIG", "configs/example.yaml")
        _config = load_config(path)
    return _config


def load_config(config_path: str) -> SyncConfig:
    with open(config_path) as f:
        data = yaml.safe_load(f)
    cfg = SyncConfig(**data)
    cfg.state_dir = str(Path(cfg.state_dir).expanduser())
    cfg.temp_dir = str(Path(cfg.temp_dir).expanduser())
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.temp_dir).mkdir(parents=True, exist_ok=True)
    return cfg
