#!/usr/bin/env python3
"""Shared settings and onboarding routes for the local web surface."""

from __future__ import annotations

import base64
import binascii

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from settings_store import (
    import_material as import_user_material,
)
from settings_store import (
    load_bootstrap as load_user_bootstrap,
)
from settings_store import (
    load_settings as load_user_settings,
)
from settings_store import (
    save_settings as save_user_settings,
)


class SaveSettingsRequest(BaseModel):
    materials: dict[str, str] = Field(default_factory=dict)
    providers: dict[str, str | bool | None] = Field(default_factory=dict)
    credentials: dict[str, str | None] = Field(default_factory=dict)


class ImportMaterialRequest(BaseModel):
    material_key: str
    text: str | None = None
    source_url: str | None = None
    file_name: str | None = None
    content_type: str | None = None
    content_base64: str | None = None


def register_settings_routes(app: FastAPI, *, is_worker_running) -> None:
    @app.get("/api/bootstrap")
    def bootstrap():
        payload = load_user_bootstrap()
        payload["worker_running"] = is_worker_running()
        return payload

    @app.get("/api/settings")
    def get_settings():
        return load_user_settings()

    @app.post("/api/settings")
    def save_settings(payload: SaveSettingsRequest):
        try:
            return save_user_settings(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/settings/materials/import")
    def import_material(payload: ImportMaterialRequest):
        try:
            content_bytes = None
            if payload.content_base64 is not None:
                content_bytes = base64.b64decode(payload.content_base64, validate=True)

            return import_user_material(
                payload.material_key,
                text=payload.text,
                source_url=payload.source_url,
                file_name=payload.file_name,
                content_type=payload.content_type,
                content_bytes=content_bytes,
            )
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(400, str(exc)) from exc
