#!/usr/bin/env python3
"""Generate the provisioned Freematics Grafana dashboard."""

from __future__ import annotations

import json
from pathlib import Path


DS = {"type": "prometheus", "uid": "freematics-prometheus"}
DEVICE = 'device_id="$device"'
TRIP = 'trip_id=~"$trip"'
KM_TO_MI = 0.621371
IMPERIAL_GALLON_LITRES = 4.54609
# kph * this constant / litres-per-hour = UK (imperial) mpg.
KPH_TO_UK_MPG_PER_LPH = KM_TO_MI * IMPERIAL_GALLON_LITRES
# Fallback fuel-flow estimate when the ECU omits optional PID 0x5E. It is
# deliberately labelled as an estimate: petrol stoichiometric AFR and density
# are assumptions, not vehicle-specific facts.
PETROL_STOICH_AFR = 14.7
PETROL_DENSITY_G_PER_LITRE = 745.0


def target(
    expression: str,
    ref: str = "A",
    legend: str = "",
    *,
    instant: bool = False,
    table: bool = False,
) -> dict:
    result = {
        "datasource": DS,
        "editorMode": "code",
        "expr": expression,
        "legendFormat": legend,
        "range": not instant,
        "refId": ref,
    }
    if instant:
        result["instant"] = True
    if table:
        result["format"] = "table"
    return result


def thresholds(*steps: tuple[float | None, str]) -> dict:
    return {
        "mode": "absolute",
        "steps": [{"value": value, "color": colour} for value, colour in steps],
    }


def stat(
    panel_id: int,
    title: str,
    x: int,
    y: int,
    expression: str,
    *,
    unit: str = "short",
    description: str = "",
    mappings: list[dict] | None = None,
    threshold_steps: tuple[tuple[float | None, str], ...] = ((None, "green"),),
    decimals: int | None = None,
    no_value: str = "No data",
    text_mode: str = "auto",
    width: int = 4,
) -> dict:
    defaults: dict = {
        "color": {"mode": "thresholds"},
        "mappings": mappings or [],
        "noValue": no_value,
        "thresholds": thresholds(*threshold_steps),
        "unit": unit,
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "datasource": DS,
        "description": description,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": {"h": 3, "w": width, "x": x, "y": y},
        "id": panel_id,
        "options": {
            "colorMode": "value",
            "graphMode": "none",
            "justifyMode": "auto",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showPercentChange": False,
            "textMode": text_mode,
            "wideLayout": True,
        },
        "targets": [target(expression, instant=True)],
        "title": title,
        "type": "stat",
    }


def timeseries(
    panel_id: int,
    title: str,
    x: int,
    y: int,
    width: int,
    height: int,
    targets: list[dict],
    *,
    unit: str = "short",
    description: str = "",
    overrides: list[dict] | None = None,
    legend_calcs: list[str] | None = None,
) -> dict:
    return {
        "datasource": DS,
        "description": description,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic-by-name"},
                "custom": {
                    "axisCenteredZero": False,
                    "axisColorMode": "text",
                    "axisLabel": "",
                    "axisPlacement": "auto",
                    "barAlignment": 0,
                    "drawStyle": "line",
                    "fillOpacity": 8,
                    "gradientMode": "none",
                    "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                    "insertNulls": False,
                    "lineInterpolation": "smooth",
                    "lineWidth": 2,
                    "pointSize": 3,
                    "scaleDistribution": {"type": "linear"},
                    "showPoints": "never",
                    "spanNulls": False,
                    "stacking": {"group": "A", "mode": "none"},
                    "thresholdsStyle": {"mode": "off"},
                },
                "mappings": [],
                "thresholds": thresholds((None, "green")),
                "unit": unit,
            },
            "overrides": overrides or [],
        },
        "gridPos": {"h": height, "w": width, "x": x, "y": y},
        "id": panel_id,
        "options": {
            "legend": {
                "calcs": legend_calcs or ["lastNotNull", "min", "max"],
                "displayMode": "table",
                "placement": "bottom",
                "showLegend": True,
            },
            "tooltip": {"hideZeros": False, "mode": "multi", "sort": "desc"},
        },
        "targets": targets,
        "title": title,
        "type": "timeseries",
    }


def by_name(name: str, *properties: tuple[str, object]) -> dict:
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": prop, "value": value} for prop, value in properties],
    }


def build_dashboard() -> dict:
    value_mapping = lambda options: [{"type": "value", "options": options}]
    panels: list[dict] = []

    selection = f"{{{DEVICE},{TRIP}}}"
    speed_kph = f'freematics_obd_value{{{DEVICE},{TRIP},pid="0x10D"}}'
    speed_mph = f"({speed_kph} * {KM_TO_MI})"
    gps_speed_kph = f"freematics_gps_speed_kilometres_per_hour{selection}"
    gps_speed_mph = f"({gps_speed_kph} * {KM_TO_MI})"
    rpm = f'freematics_obd_value{{{DEVICE},{TRIP},pid="0x10C"}}'
    fuel_level = f'freematics_obd_value{{{DEVICE},{TRIP},pid="0x12F"}}'
    fuel_rate = f'freematics_obd_value{{{DEVICE},{TRIP},pid="0x15E"}}'
    maf = f'freematics_obd_value{{{DEVICE},{TRIP},pid="0x110"}}'
    estimated_fuel_rate = (
        f"({maf} * 3600 / ({PETROL_STOICH_AFR} * {PETROL_DENSITY_G_PER_LITRE}))"
    )
    # Only returns a value when the ECU reports PID 0x5E (engine fuel rate).
    instant_uk_mpg = (
        f"(({speed_kph} * {KPH_TO_UK_MPG_PER_LPH}) "
        f"/ on(device_id,trip_id) clamp_min({fuel_rate}, 0.01))"
        f" or (({gps_speed_mph} * {IMPERIAL_GALLON_LITRES}) "
        f"/ on(device_id,trip_id) clamp_min({fuel_rate}, 0.01))"
    )
    estimated_uk_mpg = (
        f"(({speed_kph} * {KPH_TO_UK_MPG_PER_LPH}) "
        f"/ on(device_id,trip_id) clamp_min({estimated_fuel_rate}, 0.01))"
        f" or (({gps_speed_mph} * {IMPERIAL_GALLON_LITRES}) "
        f"/ on(device_id,trip_id) clamp_min({estimated_fuel_rate}, 0.01))"
    )
    accel_x = f'freematics_acceleration_g{{{DEVICE},{TRIP},axis="x"}}'

    panels.extend(
        [
            stat(
                1,
                "Vehicle link",
                0,
                0,
                f"max(freematics_device_connected{{{DEVICE}}})",
                width=3,
                description="Online means the collector has received telemetry within its channel timeout.",
                mappings=value_mapping(
                    {
                        "0": {"color": "red", "index": 1, "text": "Offline"},
                        "1": {"color": "green", "index": 0, "text": "Online"},
                    }
                ),
                no_value="Never seen",
                threshold_steps=((None, "red"), (1, "green")),
            ),
            stat(
                2,
                "Active uplink",
                3,
                0,
                f"max(freematics_network_transport{{{DEVICE}}})",
                width=3,
                description="Transport reported by the firmware: cellular is preferred; Wi-Fi is fallback.",
                mappings=value_mapping(
                    {
                        "0": {"color": "red", "index": 2, "text": "Offline"},
                        "1": {"color": "blue", "index": 1, "text": "Wi-Fi"},
                        "2": {"color": "green", "index": 0, "text": "Cellular"},
                    }
                ),
                no_value="Starting",
                threshold_steps=((None, "red"), (1, "blue"), (2, "green")),
            ),
            stat(
                3,
                "Telemetry age",
                6,
                0,
                f"max(freematics_device_data_age_seconds{{{DEVICE}}})",
                width=3,
                unit="s",
                description="Age of the newest packet at the collector. Parked standby deliberately creates long gaps.",
                decimals=1,
                no_value="No packets",
                threshold_steps=((None, "green"), (15, "orange"), (60, "red")),
            ),
            stat(
                4,
                "Vehicle voltage",
                9,
                0,
                f"max(freematics_device_battery_voltage_volts{{{DEVICE}}})",
                width=3,
                unit="volt",
                decimals=2,
                description="Voltage reported by the Freematics power input. Bench USB voltage is not a vehicle-battery reading.",
                no_value="Unavailable",
                threshold_steps=((None, "red"), (11.8, "orange"), (12.2, "green"), (15.0, "red")),
            ),
            stat(
                36,
                "Fuel level",
                12,
                0,
                'last_over_time(freematics_obd_value{device_id="$device",pid="0x12F"}[5m])',
                unit="percent",
                decimals=1,
                description="Live ECU fuel-tank percentage. This is a gauge percentage, not litres; red means at or below 15%.",
                no_value="Not reported",
                threshold_steps=((None, "red"), (15, "red"), (25, "orange"), (100, "green")),
                width=3,
            ),
            stat(
                5,
                "GPS satellites",
                15,
                0,
                f"max(freematics_gps_satellites{{{DEVICE}}})",
                width=3,
                unit="short",
                decimals=0,
                description="Satellites used by the current fix. No value indoors is expected, not fabricated as zero.",
                no_value="No fix",
                threshold_steps=((None, "red"), (4, "orange"), (7, "green")),
            ),
            stat(
                6,
                "Diagnostic faults",
                18,
                0,
                f"sum(freematics_diagnostic_trouble_codes{{{DEVICE}}})",
                width=3,
                unit="short",
                decimals=0,
                description="Total stored, pending and permanent DTCs from the latest completed scan.",
                no_value="Not scanned",
                threshold_steps=((None, "green"), (1, "red")),
            ),
            stat(
                37,
                "ECU metrics",
                21,
                0,
                'count(count by (pid) (freematics_obd_value{device_id="$device"}))',
                unit="short",
                decimals=0,
                description="Distinct numeric Mode 01 PIDs currently reported by the vehicle ECU. The raw table below keeps the complete inventory visible.",
                no_value="No ECU data",
                threshold_steps=((None, "red"), (1, "green")),
                width=3,
            ),
        ]
    )

    panels.extend(
        [
            stat(
                7,
                "Trip start",
                0,
                3,
                f"max(last_over_time(freematics_trip_start_time_seconds{selection}[$__range])) * 1000",
                unit="dateTimeAsIso",
                description="Collector login time for the selected trip. Choose a trip above the dashboard.",
                no_value="Select trip",
            ),
            stat(
                8,
                "Duration",
                4,
                3,
                f"max(max_over_time(freematics_trip_elapsed_seconds{selection}[$__range]))",
                unit="s",
                decimals=0,
                description="Collector-observed duration of the selected trip.",
                no_value="No trip",
            ),
            stat(
                9,
                "Distance",
                8,
                3,
                f"max(max_over_time(freematics_trip_distance_kilometres{selection}[$__range])) * {KM_TO_MI}",
                unit="suffix: mi",
                decimals=2,
                description="Distance integrated by the device from OBD speed, with GPS fallback, displayed in statute miles for UK driving.",
                no_value="No distance",
            ),
            stat(
                10,
                "Average speed",
                12,
                3,
                f"(avg(avg_over_time({speed_kph}[$__range])) * {KM_TO_MI}) or (avg(avg_over_time({gps_speed_kph}[$__range])) * {KM_TO_MI})",
                unit="suffix: mph",
                decimals=1,
                description="Time-average speed in miles per hour. OBD speed is preferred, with GPS as the fallback.",
                no_value="No speed",
            ),
            stat(
                11,
                "Maximum speed",
                16,
                3,
                f"(max(max_over_time({speed_kph}[$__range])) * {KM_TO_MI}) or (max(max_over_time({gps_speed_kph}[$__range])) * {KM_TO_MI})",
                unit="suffix: mph",
                decimals=1,
                description="Highest observed speed in miles per hour, with GPS used when OBD speed is unavailable.",
                no_value="No speed",
            ),
            stat(
                12,
                "Peak engine speed",
                20,
                3,
                f"max(max_over_time({rpm}[$__range]))",
                unit="rpm",
                decimals=0,
                description="Highest engine RPM in the selected trip and time range.",
                no_value="No ECU data",
            ),
            stat(
                13,
                "Fuel at start",
                0,
                6,
                f"max(last_over_time({fuel_level}[$__range]) - delta({fuel_level}[$__range]))",
                unit="percent",
                decimals=1,
                description="Estimated first fuel-level value from the final sample and gauge delta over the selected range.",
                no_value="Unsupported",
            ),
            stat(
                14,
                "Fuel at end",
                4,
                6,
                f"max(last_over_time({fuel_level}[$__range]))",
                unit="percent",
                decimals=1,
                description="Latest fuel-level sample in the selected time range.",
                no_value="Unsupported",
            ),
            stat(
                15,
                "Fuel level change",
                8,
                6,
                f"max(-delta({fuel_level}[$__range]))",
                unit="percent",
                decimals=1,
                description="Start minus end fuel-tank percentage. Sensor quantisation means short trips may show zero or noise.",
                no_value="Unsupported",
            ),
            stat(
                16,
                "Peak acceleration",
                12,
                6,
                f"max(max_over_time({accel_x}[$__range]))",
                unit="accG",
                decimals=2,
                description="Peak positive acceleration on device X. Confirm mounting orientation in the car before treating it as longitudinal.",
                no_value="No motion data",
            ),
            stat(
                17,
                "Peak braking",
                16,
                6,
                f"abs(min(min_over_time({accel_x}[$__range])))",
                unit="accG",
                decimals=2,
                description="Magnitude of peak negative acceleration on device X; mounting orientation must be confirmed.",
                no_value="No motion data",
            ),
            stat(
                18,
                "Maximum coolant",
                20,
                6,
                f'max(max_over_time(freematics_obd_value{{{DEVICE},{TRIP},pid="0x105"}}[$__range]))',
                unit="celsius",
                decimals=1,
                description="Maximum engine coolant temperature exposed by the ECU.",
                no_value="Unsupported",
                threshold_steps=((None, "blue"), (75, "green"), (105, "orange"), (115, "red")),
            ),
        ]
    )

    trip_table_targets = [
        target(
            f"last_over_time(freematics_trip_start_time_seconds{selection}[$__range]) * 1000",
            "A",
            "Start",
            instant=True,
            table=True,
        ),
        target(
            f"max_over_time(freematics_trip_elapsed_seconds{selection}[$__range])",
            "B",
            "Duration",
            instant=True,
            table=True,
        ),
        target(
            f"max_over_time(freematics_trip_distance_kilometres{selection}[$__range]) * {KM_TO_MI}",
            "C",
            "Distance",
            instant=True,
            table=True,
        ),
        target(
            f"avg_over_time({speed_kph}[$__range]) * {KM_TO_MI}",
            "D",
            "Average speed",
            instant=True,
            table=True,
        ),
        target(
            f"max_over_time({speed_kph}[$__range]) * {KM_TO_MI}",
            "E",
            "Maximum speed",
            instant=True,
            table=True,
        ),
    ]
    panels.append(
        {
            "datasource": DS,
            "description": "One row per collector trip in the dashboard time range. Use the Trip selector for a detailed route and metric investigation.",
            "fieldConfig": {
                "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                "overrides": [
                    by_name("Start", ("unit", "dateTimeAsIso")),
                    by_name("Duration", ("unit", "s")),
                    by_name("Distance", ("unit", "suffix: mi"), ("decimals", 2)),
                    by_name("Average speed", ("unit", "suffix: mph"), ("decimals", 1)),
                    by_name("Maximum speed", ("unit", "suffix: mph"), ("decimals", 1)),
                ],
            },
            "gridPos": {"h": 6, "w": 24, "x": 0, "y": 9},
            "id": 19,
            "options": {
                "cellHeight": "sm",
                "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
                "showHeader": True,
                "sortBy": [{"displayName": "Start", "desc": True}],
            },
            "targets": trip_table_targets,
            "title": "Trip index",
            "transformations": [{"id": "joinByField", "options": {"byField": "trip_id", "mode": "outer"}}],
            "type": "table",
        }
    )

    panels.append(
        {
            "datasource": DS,
            "description": "GPS route for the selected trip and time range. No line is shown until a valid outdoor fix is received.",
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "thresholds"},
                    "custom": {"hideFrom": {"legend": False, "tooltip": False, "viz": False}},
                    "mappings": [],
                    "thresholds": thresholds((None, "green")),
                },
                "overrides": [],
            },
            "gridPos": {"h": 10, "w": 10, "x": 0, "y": 15},
            "id": 20,
            "options": {
                "basemap": {"config": {}, "name": "Basemap", "type": "default"},
                "controls": {
                    "mouseWheelZoom": True,
                    "showAttribution": True,
                    "showDebug": False,
                    "showMeasure": True,
                    "showScale": True,
                    "showZoom": True,
                },
                "layers": [
                    {
                        "config": {
                            "arrow": 0,
                            "style": {
                                "color": {"fixed": "super-light-blue"},
                                "lineWidth": 4,
                                "opacity": 0.9,
                                "rotation": {"fixed": 0, "max": 360, "min": -360, "mode": "mod"},
                                "size": {"fixed": 5, "max": 15, "min": 2},
                                "symbol": {"fixed": "img/icons/marker/circle.svg", "mode": "fixed"},
                            },
                        },
                        "location": {"latitude": "Latitude", "longitude": "Longitude", "mode": "coords"},
                        "name": "Vehicle route",
                        "tooltip": True,
                        "type": "route",
                    }
                ],
                "tooltip": {"mode": "details"},
                "view": {"allLayers": True, "id": "fit", "lat": 54.5, "lon": -3.0, "zoom": 5},
            },
            "targets": [
                target(f"freematics_gps_latitude_degrees{selection}", "A", "Latitude"),
                target(f"freematics_gps_longitude_degrees{selection}", "B", "Longitude"),
            ],
            "title": "Trip route",
            "transformations": [{"id": "joinByField", "options": {"byField": "Time", "mode": "outer"}}],
            "type": "geomap",
        }
    )

    panels.append(
        timeseries(
            21,
            "Road speed and engine speed",
            10,
            15,
            14,
            10,
            [
                target(speed_mph, "A", "OBD speed (mph)"),
                target(gps_speed_mph, "B", "GPS speed (mph)"),
                target(rpm, "C", "Engine RPM"),
            ],
            unit="suffix: mph",
            description="UK display units: OBD and GPS speed are shown in mph; RPM uses the right axis. Gaps remain visible instead of being invented.",
            overrides=[
                by_name("Engine RPM", ("unit", "rpm"), ("custom.axisPlacement", "right"), ("color", {"fixedColor": "orange", "mode": "fixed"})),
                by_name("OBD speed (mph)", ("color", {"fixedColor": "blue", "mode": "fixed"})),
                by_name("GPS speed (mph)", ("color", {"fixedColor": "light-blue", "mode": "fixed"}), ("custom.lineStyle", {"dash": [8, 6], "fill": "dash"})),
            ],
        )
    )

    panels.extend(
        [
            timeseries(
                22,
                "Acceleration by device axis",
                0,
                25,
                8,
                7,
                [target(f"freematics_acceleration_g{selection}", "A", "{{axis}} axis")],
                unit="accG",
                description="Bias-corrected MEMS acceleration. Mount the unit consistently before interpreting X as acceleration/braking.",
            ),
            timeseries(
                23,
                "Engine load and driver demand",
                8,
                25,
                8,
                7,
                [
                    target(f'freematics_obd_value{{{DEVICE},{TRIP},pid="0x104"}}', "A", "Engine load"),
                    target(f'freematics_obd_value{{{DEVICE},{TRIP},pid="0x111"}}', "B", "Throttle"),
                    target(
                        f'freematics_obd_value{{{DEVICE},{TRIP},name=~"accelerator_pedal_position_.*|relative_accelerator_pedal_position|driver_demand_engine_torque|actual_engine_torque"}}',
                        "C",
                        "{{description}}",
                    ),
                ],
                unit="percent",
                description="Only PIDs advertised by the ECU appear. Missing accelerator or torque series means unsupported, not zero.",
            ),
            timeseries(
                24,
                "Thermal system",
                16,
                25,
                8,
                7,
                [
                    target(f'freematics_obd_value{{{DEVICE},{TRIP},name=~".*temperature.*"}}', "A", "{{description}}"),
                    target(f"freematics_device_temperature_celsius{selection}", "B", "TeleLogger enclosure"),
                ],
                unit="celsius",
                description="Coolant, intake, oil, catalyst and ambient temperatures, plus the logger enclosure, when exposed.",
            ),
            timeseries(
                25,
                "Fuel level, trim and mixture",
                0,
                32,
                8,
                7,
                [
                    target(f'freematics_obd_value{{{DEVICE},{TRIP},name=~"fuel_level|.*fuel_trim.*"}}', "A", "{{description}}"),
                    target(f'freematics_obd_value{{{DEVICE},{TRIP},name="commanded_equivalence_ratio"}}', "B", "Equivalence ratio"),
                ],
                unit="percent",
                description="Fuel gauge percentage and closed-loop trims stay on the primary axis. Equivalence ratio uses the right axis; fuel percentage is not a volume measurement.",
                overrides=[by_name("Equivalence ratio", ("unit", "none"), ("custom.axisPlacement", "right"))],
            ),
            timeseries(
                26,
                "Airflow and pressure",
                8,
                32,
                8,
                7,
                [
                    target(maf, "A", "Mass airflow"),
                    target(f'freematics_obd_value{{{DEVICE},{TRIP},name=~".*pressure.*"}}', "B", "{{description}}"),
                ],
                unit="kpascal",
                description="Intake, fuel-rail, barometric and evaporative pressures with mass airflow on its own axis.",
                overrides=[by_name("Mass airflow", ("unit", "gps"), ("custom.axisPlacement", "right"))],
            ),
            timeseries(
                27,
                "GPS quality",
                16,
                32,
                8,
                7,
                [
                    target(f"freematics_gps_satellites{selection}", "A", "Satellites"),
                    target(f"freematics_gps_hdop{selection}", "B", "HDOP"),
                    target(f"freematics_gps_altitude_metres{selection}", "C", "Altitude"),
                ],
                unit="short",
                description="Satellite count and HDOP explain route quality; altitude uses the right axis.",
                overrides=[
                    by_name("HDOP", ("unit", "none")),
                    by_name("Altitude", ("unit", "lengthm"), ("custom.axisPlacement", "right")),
                ],
            ),
        ]
    )

    panels.append(
        {
            "datasource": DS,
            "description": "Transport transitions over the selected range. Parked standby is intentionally offline.",
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "thresholds"},
                    "custom": {"fillOpacity": 90, "lineWidth": 0, "spanNulls": False},
                    "mappings": value_mapping(
                        {
                            "0": {"color": "red", "index": 2, "text": "Offline"},
                            "1": {"color": "blue", "index": 1, "text": "Wi-Fi"},
                            "2": {"color": "green", "index": 0, "text": "Cellular"},
                        }
                    ),
                    "thresholds": thresholds((None, "red"), (1, "blue"), (2, "green")),
                },
                "overrides": [],
            },
            "gridPos": {"h": 5, "w": 6, "x": 0, "y": 39},
            "id": 28,
            "options": {
                "alignValue": "left",
                "legend": {"displayMode": "hidden", "placement": "bottom", "showLegend": False},
                "mergeValues": True,
                "rowHeight": 0.8,
                "showValue": "always",
                "tooltip": {"mode": "single", "sort": "none"},
            },
            "targets": [target(f"freematics_network_transport{selection}", "A", "Uplink")],
            "title": "Network path",
            "type": "state-timeline",
        }
    )

    panels.append(
        {
            "datasource": DS,
            "description": "Read-only stored, pending and permanent diagnostic trouble codes. The firmware never clears codes.",
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}}, "overrides": []},
            "gridPos": {"h": 5, "w": 6, "x": 6, "y": 39},
            "id": 29,
            "options": {"cellHeight": "sm", "showHeader": True, "sortBy": [{"displayName": "status", "desc": False}]},
            "targets": [
                target(
                    f"max_over_time(freematics_diagnostic_trouble_code_info{selection}[$__range])",
                    "A",
                    "Fault",
                    instant=True,
                    table=True,
                )
            ],
            "title": "Diagnostic trouble codes",
            "transformations": [
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True, "Value": True, "__name__": True, "device_id": True, "job": True, "instance": True},
                        "indexByName": {"code": 0, "status": 1, "system": 2, "trip_id": 3},
                        "renameByName": {"code": "Code", "status": "Status", "system": "System", "trip_id": "Trip"},
                    },
                }
            ],
            "type": "table",
        }
    )

    panels.append(
        timeseries(
            30,
            "Radio and telemetry health",
            12,
            39,
            6,
            5,
            [
                target(f"freematics_device_rssi_dbm{{{DEVICE}}}", "A", "Signal"),
                target(f"freematics_device_sample_rate_per_minute{{{DEVICE}}}", "B", "Samples/min"),
                target(f"freematics_device_data_age_seconds{{{DEVICE}}}", "C", "Age"),
            ],
            unit="dBm",
            description="Signal strength, decoded sample throughput and packet age expose connectivity degradation without oversized status cards.",
            overrides=[
                by_name("Samples/min", ("unit", "ops"), ("custom.axisPlacement", "right")),
                by_name("Age", ("unit", "s"), ("custom.axisPlacement", "right")),
            ],
            legend_calcs=["lastNotNull"],
        )
    )

    panels.append(
        timeseries(
            38,
            "Fuel rate and economy",
            18,
            39,
            6,
            5,
            [
                target(fuel_rate, "A", "ECU fuel rate (L/h)"),
                target(estimated_fuel_rate, "B", "MAF fuel rate estimate (L/h)"),
                target(instant_uk_mpg, "C", "ECU economy (UK mpg)"),
                target(estimated_uk_mpg, "D", "MAF economy estimate (UK mpg)"),
            ],
            unit="suffix: L/h",
            description="Fuel rate is shown directly when standard PID 0x5E is reported. The MAF estimate is a transparent petrol-only fallback (14.7:1 AFR, 745 g/L) and is not calibrated consumption; fuel percentage alone cannot produce efficiency.",
            overrides=[
                by_name("ECU economy (UK mpg)", ("unit", "suffix: mpg UK"), ("custom.axisPlacement", "right")),
                by_name("MAF economy estimate (UK mpg)", ("unit", "suffix: mpg UK"), ("custom.axisPlacement", "right"), ("custom.lineStyle", {"dash": [6, 5], "fill": "dash"})),
            ],
            legend_calcs=["lastNotNull", "min", "max"],
        )
    )

    payload_bytes = f"sum(increase(freematics_device_data_received_bytes_total{{{DEVICE}}}[$__range]))"
    panels.extend(
        [
            stat(
                32,
                "Telemetry payload in range",
                0,
                44,
                payload_bytes,
                unit="decbytes",
                description="Application payload accepted by the collector during the selected dashboard range.",
                decimals=2,
                width=6,
            ),
            stat(
                33,
                "Estimated SIM payload cost",
                6,
                44,
                f"({payload_bytes}) / 1000000 * 0.005",
                unit="currencyGBP",
                description="Payload estimate at £0.005 per decimal MB. It assumes all collector payload used cellular and excludes TCP, TLS and mobile-network overhead.",
                decimals=4,
                width=6,
            ),
            stat(
                34,
                "Average payload rate",
                12,
                44,
                f"({payload_bytes}) / 1000 / $__range_s * 3600",
                unit="suffix: KB/hour",
                description="Average accepted payload rate across the selected dashboard range, including parked time.",
                decimals=1,
                width=6,
            ),
            stat(
                35,
                "30-day payload estimate",
                18,
                44,
                f"avg_over_time(rate(freematics_device_data_received_bytes_total{{{DEVICE}}}[5m])[$__range:30s]) * 2592000 / 1000000 * 0.005",
                unit="currencyGBP",
                description="Thirty-day projection from the average payload rate in the selected range. Actual Simbase billing includes network overhead and excludes Wi-Fi traffic.",
                decimals=2,
                width=6,
            ),
        ]
    )

    raw_targets = [
        target(f"last_over_time(freematics_obd_value{selection}[$__range])", "A", "Latest", instant=True, table=True),
        target(f"min_over_time(freematics_obd_value{selection}[$__range])", "B", "Minimum", instant=True, table=True),
        target(f"avg_over_time(freematics_obd_value{selection}[$__range])", "C", "Average", instant=True, table=True),
        target(f"max_over_time(freematics_obd_value{selection}[$__range])", "D", "Maximum", instant=True, table=True),
        target(f"last_over_time(freematics_obd_value_age_seconds{selection}[$__range])", "E", "Age", instant=True, table=True),
    ]
    panels.append(
        {
            "datasource": DS,
            "description": "Every standard Mode 01 PID the vehicle advertised, with friendly metadata from kierandrewett/obd. This table is deliberately exhaustive and sortable.",
            "fieldConfig": {
                "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}, "decimals": 2},
                "overrides": [by_name("Age", ("unit", "s"), ("decimals", 1))],
            },
            "gridPos": {"h": 10, "w": 24, "x": 0, "y": 47},
            "id": 31,
            "options": {
                "cellHeight": "sm",
                "footer": {"countRows": True, "fields": "", "reducer": ["count"], "show": True},
                "showHeader": True,
                "sortBy": [{"displayName": "description", "desc": False}],
            },
            "targets": raw_targets,
            "title": "Every ECU metric exposed by this vehicle",
            "transformations": [{"id": "joinByField", "options": {"byField": "pid", "mode": "outer"}}],
            "type": "table",
        }
    )

    return {
        "annotations": {"list": []},
        "description": "History-first vehicle telemetry: trip index, route, OBD, GNSS, motion, diagnostics and every ECU-advertised standard PID.",
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": [
            {
                "asDropdown": False,
                "icon": "external link",
                "includeVars": True,
                "keepTime": True,
                "tags": [],
                "targetBlank": True,
                "title": "Raw Freematics trip archive",
                "tooltip": "Open the collector's archived trip files",
                "type": "link",
                "url": "https://freematics-admin.drewett.dev/trips.html?devid=$device",
            }
        ],
        "liveNow": True,
        "panels": panels,
        "refresh": "2s",
        "schemaVersion": 41,
        "tags": ["vehicle", "obd", "freematics", "trips", "gps", "diagnostics"],
        "templating": {
            "list": [
                {
                    "current": {"selected": True, "text": "ZKUCALJ0", "value": "ZKUCALJ0"},
                    "datasource": DS,
                    "definition": "label_values(freematics_device_connected, device_id)",
                    "hide": 0,
                    "includeAll": False,
                    "label": "Vehicle",
                    "multi": False,
                    "name": "device",
                    "options": [],
                    "query": {"query": "label_values(freematics_device_connected, device_id)", "refId": "PrometheusVariableQueryEditor-VariableQuery"},
                    "refresh": 1,
                    "regex": "",
                    "skipUrlSync": False,
                    "sort": 1,
                    "type": "query",
                },
                {
                    "allValue": ".*",
                    "current": {"selected": True, "text": "All trips", "value": "$__all"},
                    "datasource": DS,
                    "definition": "label_values(freematics_trip_start_time_seconds{device_id=\"$device\"}, trip_id)",
                    "hide": 0,
                    "includeAll": True,
                    "label": "Trip",
                    "multi": False,
                    "name": "trip",
                    "options": [],
                    "query": {
                        "query": "label_values(freematics_trip_start_time_seconds{device_id=\"$device\"}, trip_id)",
                        "refId": "PrometheusVariableQueryEditor-VariableQuery",
                    },
                    "refresh": 2,
                    "regex": "",
                    "skipUrlSync": False,
                    "sort": 2,
                    "type": "query",
                },
            ]
        },
        "time": {"from": "now-5m", "to": "now"},
        "timepicker": {
            "refresh_intervals": ["2s", "5s", "10s", "30s", "1m", "5m"],
            "time_options": ["5m", "15m", "1h", "6h", "12h", "24h", "2d", "7d", "30d", "90d", "1y"],
        },
        "timezone": "browser",
        "title": "Vehicle · Freematics",
        "uid": "freematics-vehicle",
        "version": 1,
        "weekStart": "monday",
    }


def main() -> None:
    output = Path(__file__).with_name("grafana-dashboard.json")
    output.write_text(json.dumps(build_dashboard(), indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
