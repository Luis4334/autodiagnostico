"""api/__init__.py — Registro de Blueprints."""
from flask import Flask
from .monitoreo     import bp as monitoreo_bp
from .configuracion import bp as config_bp
from .alarmas       import bp as alarmas_bp
from .exportacion   import bp as exportacion_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(monitoreo_bp,   url_prefix="/api/monitoreo")
    app.register_blueprint(config_bp,      url_prefix="/api")
    app.register_blueprint(alarmas_bp,     url_prefix="/api")
    app.register_blueprint(exportacion_bp, url_prefix="/api/exportacion")
