"""Immutable snapshots decoded from the hub's REST API."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScheduledAction:
    """The next scheduled action at the instant a snapshot was fetched."""

    delay_seconds: int
    enabled: bool
    local_time: str
    entry_index: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "ScheduledAction":
        """Decode the API's next-action object without schedule entry details."""
        return cls(
            delay_seconds=data["in_s"],
            enabled=data["on"],
            local_time=data["at"],
            entry_index=data["entry"],
        )


@dataclass(frozen=True)
class PortStatus:
    """One immutable port snapshot; unavailable readings are None, not zero."""

    number: int
    name: str
    enabled: bool
    occupied: bool
    fault: bool
    led_state: str
    monitor_present: bool
    voltage_v: float | None
    current_ma: float | None
    power_mw: float | None
    power_mode: str | None
    schedule_count: int
    next_action: ScheduledAction | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PortStatus":
        """Decode either port endpoint; bulk snapshots omit power_mode."""
        monitor = data["monitor"]
        schedule = data["schedule"]
        next_action = schedule["next"]
        return cls(
            number=data["n"],
            name=data["name"],
            enabled=data["enabled"],
            occupied=data["occupied"],
            fault=data["fault"],
            led_state=data["state"],
            monitor_present=monitor,
            voltage_v=float(data["volts"]) if monitor else None,
            current_ma=float(data["ma"]) if monitor else None,
            power_mw=float(data["mw"]) if monitor else None,
            power_mode=data.get("power_mode"),
            schedule_count=schedule["count"],
            next_action=(
                ScheduledAction.from_api(next_action)
                if next_action is not None
                else None
            ),
        )


@dataclass(frozen=True)
class HubInfo:
    """Immutable hub-wide snapshot, with invalid sensor and clock data absent."""

    name: str
    firmware: str
    uptime_seconds: int
    reset_reason: str
    main_5v: bool
    upstream_5v: bool
    total_current_ma: float
    total_power_mw: float
    temperature_c: float | None
    humidity_percent: float | None
    clock_synced: bool
    time_iso: str | None
    utc_offset_hours: float
    network_connected: bool
    ip_address: str
    ssid: str
    rssi_dbm: int
    hostname: str
    expander_present: bool
    current_monitor_1_present: bool
    current_monitor_2_present: bool
    environment_sensor_present: bool
    heap_free_bytes: int
    heap_largest_block_bytes: int
    port_count: int
    ports_enabled: int
    ports_occupied: int
    ports_faulted: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "HubInfo":
        """Normalize /api/hub, retaining only currently valid measurements."""
        power = data["power"]
        sensors = data["sensors"]
        clock = data["clock"]
        network = data["network"]
        devices = data["devices"]
        memory = data["memory"]
        ports = data["ports_summary"]
        return cls(
            name=data["name"],
            firmware=data["fw"],
            uptime_seconds=data["uptime_s"],
            reset_reason=data["reset_reason"],
            main_5v=power["main_5v"],
            upstream_5v=power["upstream_5v"],
            total_current_ma=float(power["total_ma"]),
            total_power_mw=float(power["total_mw"]),
            temperature_c=float(sensors["temp_c"]) if sensors["valid"] else None,
            humidity_percent=float(sensors["humidity"]) if sensors["valid"] else None,
            clock_synced=clock["synced"],
            time_iso=clock["iso"] if clock["synced"] else None,
            utc_offset_hours=float(clock["utc_offset"]),
            network_connected=network["connected"],
            ip_address=network["ip"],
            ssid=network["ssid"],
            rssi_dbm=network["rssi"],
            hostname=network["hostname"],
            expander_present=devices["expander"],
            current_monitor_1_present=devices["ina_1"],
            current_monitor_2_present=devices["ina_2"],
            environment_sensor_present=devices["aht20"],
            heap_free_bytes=memory["heap_free"],
            heap_largest_block_bytes=memory["heap_max_block"],
            port_count=ports["count"],
            ports_enabled=ports["powered"],
            ports_occupied=ports["occupied"],
            ports_faulted=ports["faulted"],
        )
