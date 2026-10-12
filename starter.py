"""MCP tools for STARTER: SINAMICS (G120, S120, ...) and MICROMASTER 4 drive units in STEP 7 projects.

STARTER's scripting object is a 32-bit in-process COM server, so the work is done by
bridge/starter_bridge.ps1 in its own worker process, separate from the STEP 7 bridge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from mcp.server.fastmcp import FastMCP

from worker import DEFAULT_TIMEOUT, Bridge

starter_bridge = Bridge("starter_bridge.ps1", label="STARTER")
LONG_TIMEOUT = max(DEFAULT_TIMEOUT, 900)

# Fault and alarm parameters differ between the drive families; unknown ones are skipped.
FAULT_PARAMETERS = {
    "p945": "fault codes",
    "p947": "fault numbers",
    "p949": "fault values",
    "p2122": "alarm codes",
    "p2124": "alarm values",
    "p2110": "alarm numbers (MICROMASTER)",
}


def register(mcp: FastMCP, require_writes: Callable[[], None]) -> None:
    """Add the STARTER tools to the server; require_writes raises when changing the machine is not allowed."""

    def _is_online(project: str, device: str) -> bool:
        return bool(starter_bridge.call("is_online", project=project, device=device)["online"])

    @mcp.tool()
    def starter_status() -> dict:
        """Check that STARTER's scripting interface is reachable and report its version."""
        return starter_bridge.call("ping")

    @mcp.tool()
    def starter_list_drives(project: str) -> dict:
        """List the drive units of a project with their drive objects (Control Unit, drives, infeeds).

        project is a project name from list_projects, a project folder, or a .s7p file. Opening a project
        that was last saved by an older STARTER can make STARTER ask to convert it, which blocks until the
        call times out; open such a project once in STARTER itself first.
        """
        return starter_bridge.call("devices", project=project)

    @mcp.tool()
    def starter_read_parameters(project: str, device: str, parameters: list[str], drive_object: str | None = None) -> dict:
        """Read drive parameters with their description, unit, limits and selection text.

        parameters: "p1082", "r2", "p1000[1]"; without an index every index is returned. drive_object can be
        omitted when the drive unit has only one (G120, MICROMASTER). Values come from the drive when the
        drive unit is online (starter_set_online), otherwise from the offline project.
        """
        return starter_bridge.call("read", project=project, device=device, drive_object=drive_object, items=parameters)

    @mcp.tool()
    def starter_search_parameters(project: str, device: str, text: str, drive_object: str | None = None, limit: int = 40) -> dict:
        """Find parameters of a drive object whose number or description contains text (e.g. "ramp", "telegram").

        The first search on a drive object reads its whole parameter list and takes about ten seconds.
        """
        return starter_bridge.call("search", project=project, device=device, drive_object=drive_object, text=text,
                                   limit=limit, timeout=LONG_TIMEOUT)

    @mcp.tool()
    def starter_write_parameters(
        project: str,
        device: str,
        values: dict[str, bool | int | float | str],
        drive_object: str | None = None,
    ) -> dict:
        """Change drive parameters. Offline this modifies the project; ONLINE IT CHANGES THE RUNNING DRIVE.

        values maps a parameter ("p1082", "p1000[1]"; index 0 when omitted) to its new value: a number, a
        selection text as shown in the expert list, or a BICO source such as "2090:0". STARTER checks the
        range and write protection. Online changes are held in RAM until starter_ram_to_rom. Confirm with the
        user before writing to an online drive.
        """
        if _is_online(project, device):
            require_writes()
        return starter_bridge.call("write", project=project, device=device, drive_object=drive_object, values=values)

    @mcp.tool()
    def starter_read_faults(project: str, device: str, drive_object: str | None = None) -> dict:
        """Read the fault and alarm buffers of a drive object. Only meaningful while the drive unit is online."""
        result = starter_bridge.call("read", project=project, device=device, drive_object=drive_object,
                                     items=list(FAULT_PARAMETERS))
        buffers = []
        for p in result["parameters"]:
            if "error" in p:
                continue
            values = p.get("values", [p.get("value")])
            # Only the occupied entries are interesting; index 0 is the newest.
            entries = {f"[{i}]": v for i, v in enumerate(values) if str(v) not in ("0", "0.0", "", "None")}
            buffers.append({"parameter": p["parameter"], "description": p["description"], "entries": entries})
        return {"driveObject": result["driveObject"], "online": _is_online(project, device),
                "active": any(b["entries"] for b in buffers), "buffers": buffers}

    @mcp.tool()
    def starter_add_drive_unit(project: str, type_name: str, version: str, name: str) -> dict:
        """Insert a new SINAMICS drive unit into a project. Modifies the offline project.

        type_name is STARTER's type identifier, e.g. "SINAMICS_G120_CU240E_2_PN", "SINAMICS_G120_CU240E_2_DP",
        "SINAMICS_CUG120C_PN", "SINAMICS_S120_2_DP", "SINAMICS_S120_2_PN"; version is the firmware version,
        e.g. "4.7.10" or "4.8". MICROMASTER drive units cannot be created this way, only read and changed.
        """
        return starter_bridge.call("add_device", project=project, type=type_name, version=version, name=name, timeout=LONG_TIMEOUT)

    @mcp.tool()
    def starter_add_drive_object(project: str, device: str, type_name: str, name: str) -> dict:
        """Insert a drive object into an S120 drive unit. Modifies the offline project.

        type_name is STARTER's type identifier, e.g. "SINAMICS_SERVO_SL" (servo drive), "SINAMICS_VECTOR_SVC"
        (vector drive), "SINAMICS_ALM" (active infeed), "SINAMICS_TM31" (terminal module). The new drive
        object has no power unit, motor or encoder yet. G120 and MICROMASTER units have a fixed drive object.
        """
        return starter_bridge.call("add_drive_object", project=project, device=device, type=type_name, name=name, timeout=LONG_TIMEOUT)

    @mcp.tool()
    def starter_export_drive_unit(project: str, device: str, output_dir: str) -> dict:
        """Export a drive unit with all its parameters as XML files into output_dir (a backup that STARTER can re-import)."""
        if not Path(output_dir).is_dir():
            raise ValueError(f"Directory '{output_dir}' does not exist.")
        return starter_bridge.call("export_device", project=project, device=device, path=output_dir, timeout=LONG_TIMEOUT)

    @mcp.tool()
    def starter_get_connection(project: str, device: str) -> dict:
        """Show how STARTER reaches a drive unit: access point, interface, address type and address."""
        return starter_bridge.call("get_connection", project=project, device=device)

    @mcp.tool()
    def starter_set_connection(
        project: str,
        device: str,
        address: str | None = None,
        address_type: str | None = None,
        interface: str | None = None,
        access_point: str | None = None,
    ) -> dict:
        """Change how STARTER reaches a drive unit. Modifies the offline project, not the drive.

        Only the given settings change. access_point is "DEVICE" or "S7ONLINE". Use starter_get_connection
        to see the current values and their spelling.
        """
        return starter_bridge.call("set_connection", project=project, device=device, address=address,
                                   address_type=address_type, interface=interface, access_point=access_point)

    @mcp.tool()
    def starter_set_online(project: str, device: str | None = None, online: bool = True) -> dict:
        """Connect STARTER to a drive unit (online=True, device required) or disconnect the project (online=False).

        Going online changes nothing in the drive. While online, parameter reads show live values and
        parameter writes go to the drive.
        """
        if online and not device:
            raise ValueError("device is required to go online.")
        return starter_bridge.call("set_online", project=project, device=device, online=online, timeout=LONG_TIMEOUT)

    @mcp.tool()
    def starter_download(project: str, device: str) -> dict:
        """Download the project's parameters to the drive unit. OVERWRITES THE DRIVE'S PARAMETERS.

        The drive unit must be online. The data is in RAM afterwards; call starter_ram_to_rom to keep it
        after a power cycle. Confirm with the user first.
        """
        require_writes()
        return starter_bridge.call("device_action", project=project, device=device, action="download", timeout=LONG_TIMEOUT)

    @mcp.tool()
    def starter_upload(project: str, device: str) -> dict:
        """Load the drive unit's parameters into the project (upload to PG). Overwrites the offline project data.

        The drive unit must be online. The drive itself is not changed.
        """
        return starter_bridge.call("device_action", project=project, device=device, action="upload", timeout=LONG_TIMEOUT)

    @mcp.tool()
    def starter_ram_to_rom(project: str, device: str) -> dict:
        """Save the drive unit's current parameters retentively (copy RAM to ROM). The drive unit must be online."""
        require_writes()
        return starter_bridge.call("device_action", project=project, device=device, action="ram_to_rom", timeout=LONG_TIMEOUT)
