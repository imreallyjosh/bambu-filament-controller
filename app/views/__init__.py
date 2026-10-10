from flask import Flask


def register_blueprints(app: Flask) -> None:
    from . import (annealing, api, auth, dashboard, drying, heater_test, history, notify, presets, printers, schedules,
                   settings)

    for mod in (auth, api, dashboard, printers, heater_test, presets, drying, annealing, schedules, history, notify, settings):
        app.register_blueprint(mod.bp)
