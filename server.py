"""MCP server for Siemens SIMATIC Manager (STEP 7 V5.x classic).

STEP 7's automation interface (Simatic.Simatic) is a 32-bit in-process COM
server, so it cannot be loaded into 64-bit Python. This server keeps a 32-bit
Windows PowerShell worker (bridge/s7bridge.ps1) alive and exchanges one JSON
object per line with it. Live PLC values go through s7client.py instead.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

import starter
from ladder import stl_to_ladder, stl_to_ladder_html
from plc_values import Operand, decode, encode, parse_address
from s7client import S7Client, S7Error, S7OnlineTransport, TcpTransport
from worker import DEFAULT_TIMEOUT, Bridge, BridgeError

# Block.GenerateSource flag; not in the type library, found by probing STEP 7 V5.6.
GENERATE_SOURCE_ABSOLUTE = 32
ALLOW_PLC_WRITES = os.environ.get("S7_MCP_ALLOW_PLC_WRITES", "") == "1"
# Block.CompareOnlineOffline return codes, found by probing against S7-PLCSIM.
COMPARE_RESULTS = {0: "identical", -1: "not in CPU"}
SYMBOL_IMPORT_MODES = {"insert": 0, "overwrite_by_name": 1, "overwrite_by_address": 2}
SYMBOL_CACHE_SECONDS = 60

bridge = Bridge("s7bridge.ps1")
mcp = FastMCP(
    "simatic-manager",
    instructions=(
        "Engineering access to Siemens STEP 7 V5.x (SIMATIC Manager) projects and their PLCs. "
        "Start with list_projects, then get_project_tree to find programs. "
        "A 'project' argument is a project name or its path; 'program' is a program name or path and may be "
        "omitted when the project has exactly one program. To change logic: get_block_source, edit the STL/SCL "
        "text, import_source, then compile_source. Tools that change the PLC (download, start/stop, writing "
        "values, memory reset) act on real machinery unless S7-PLCSIM is running: confirm with the user first. "
        "Drives (SINAMICS, MICROMASTER) configured with STARTER are handled by the starter_* tools: "
        "starter_list_drives first, then starter_read_parameters / starter_search_parameters; the same "
        "confirmation rule applies to writing to an online drive, downloading and RAM to ROM."
    ),
)


def _parse_sdf(text: str) -> list[dict[str, str]]:
    symbols = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3:
            continue
        cells = [c.strip() for c in row] + [""]
        symbols.append({"name": cells[0], "address": " ".join(cells[1].split()), "data_type": " ".join(cells[2].split()), "comment": cells[3]})
    return symbols


def _format_sdf(symbols: list[dict[str, str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    for s in symbols:
        writer.writerow([s["name"], s["address"], s["data_type"], s.get("comment", "")])
    return out.getvalue()


@mcp.tool()
def step7_status() -> dict:
    """Check that STEP 7 is reachable and report its version."""
    return bridge.call("ping")


@mcp.tool()
def list_projects() -> list[dict]:
    """List all projects and libraries registered in SIMATIC Manager (name, path, kind)."""
    return bridge.call("list_projects")


@mcp.tool()
def get_project_tree(project: str) -> dict:
    """Show a project's stations and S7 programs, with the block/source containers of each program."""
    return bridge.call("project_tree", project=project)


@mcp.tool()
def list_blocks(project: str, program: str | None = None, block_type: str | None = None) -> list[dict]:
    """List the blocks of an S7 program (OB/FB/FC/DB/UDT/VAT/SFC/SFB) with symbol, language, size and header info.

    block_type optionally filters by type, e.g. "FC" or "DB".
    """
    blocks = bridge.call("list_blocks", project=project, program=program)
    if block_type:
        blocks = [b for b in blocks if b["type"].upper() == block_type.upper()]
    return blocks


@mcp.tool()
def get_block_source(project: str, blocks: list[str], program: str | None = None, symbolic: bool = True) -> list[dict]:
    """Decompile blocks (e.g. ["OB1", "FC5"]) into STL source text.

    LAD/FBD blocks are returned as their STL equivalent. By default operands that have a symbol are shown
    by symbol name; symbolic=False shows absolute addresses instead. Know-how-protected blocks return an
    error entry.
    """
    flags = 0 if symbolic else GENERATE_SOURCE_ABSOLUTE
    results = bridge.call("block_source", project=project, program=program, blocks=blocks, flags=flags)
    if not symbolic:
        for r in results:
            if "error" in r and "not found" not in r["error"]:
                # Seen on blocks uploaded from a CPU: STEP 7 only decompiles them with symbolic=True.
                r["hint"] = "Retry with symbolic=True."
    return results


@mcp.tool()
def list_sources(project: str, program: str | None = None) -> list[dict]:
    """List the STL/SCL source files in a program's Sources folder."""
    return bridge.call("list_sources", project=project, program=program)


@mcp.tool()
def get_source(project: str, name: str, program: str | None = None) -> dict:
    """Read the text of a source file from a program's Sources folder."""
    return bridge.call("get_source", project=project, program=program, name=name)


@mcp.tool()
def import_source(
    project: str,
    name: str,
    text: str,
    program: str | None = None,
    language: Literal["awl", "scl"] = "awl",
    overwrite: bool = False,
) -> dict:
    """Create a source file in a program's Sources folder from STL ("awl") or SCL text.

    Modifies the offline project. This only stores the source; call compile_source to generate blocks.
    """
    return bridge.call("import_source", project=project, program=program, name=name, text=text, language=language, overwrite=overwrite)


@mcp.tool()
def compile_source(project: str, name: str, program: str | None = None) -> dict:
    """Compile a source file into blocks, overwriting existing offline blocks with the same numbers.

    Returns the generated block names and the STEP 7 compiler log.
    """
    return bridge.call("compile_source", project=project, program=program, name=name, timeout=max(DEFAULT_TIMEOUT, 600))


@mcp.tool()
def get_symbol_table(project: str, program: str | None = None, filter: str | None = None) -> list[dict]:
    """Read a program's symbol table (name, address, data_type, comment).

    filter is an optional case-insensitive substring matched against name, address and comment.
    """
    symbols = _parse_sdf(bridge.call("export_symbols", project=project, program=program)["sdf"])
    if filter:
        needle = filter.lower()
        symbols = [s for s in symbols if needle in s["name"].lower() or needle in s["address"].lower() or needle in s["comment"].lower()]
    return symbols


@mcp.tool()
def import_symbols(
    project: str,
    symbols: list[dict[str, str]],
    program: str | None = None,
    mode: Literal["insert", "overwrite_by_name", "overwrite_by_address"] = "insert",
) -> dict:
    """Add or update symbols in a program's symbol table. Modifies the offline project.

    Each symbol needs name, address (e.g. "I 0.0", "MW 10", "FC 5") and data_type (e.g. "BOOL", "INT", "FC 5");
    comment is optional. mode "insert" only adds new symbols; the overwrite modes replace existing entries
    matched by name or by address.
    """
    for s in symbols:
        missing = [k for k in ("name", "address", "data_type") if not s.get(k)]
        if missing:
            raise ValueError(f"Symbol {s!r} is missing {', '.join(missing)}")
    return bridge.call("import_symbols", project=project, program=program, sdf=_format_sdf(symbols), mode=SYMBOL_IMPORT_MODES[mode])


@mcp.tool()
def get_station_config(project: str, station: str) -> dict:
    """Export a station's hardware configuration (racks, modules, addresses, parameters) as HW Config text."""
    return bridge.call("station_config", project=project, station=station)


@mcp.tool()
def create_project(name: str, directory: str) -> dict:
    """Create a new empty STEP 7 project inside an existing directory."""
    return bridge.call("create_project", name=name, directory=directory)


@mcp.tool()
def add_program(project: str, name: str) -> dict:
    """Add an empty S7 program (with Blocks and Sources folders) to a project."""
    return bridge.call("add_program", project=project, name=name)


# ------------------------------------------------------------------ online tools


def _require_plc_writes() -> None:
    if not ALLOW_PLC_WRITES:
        raise PermissionError(
            "Tools that change the PLC or a drive (download, start/stop, writing values, memory reset, RAM to ROM) "
            "are disabled. "
            "Set the environment variable S7_MCP_ALLOW_PLC_WRITES=1 in the server's MCP configuration to enable them."
        )


def _describe_compare(entry: dict) -> dict:
    if "code" in entry:
        entry["result"] = COMPARE_RESULTS.get(entry["code"], "different")
    return entry


@mcp.tool()
def get_cpu_state(project: str, program: str | None = None) -> dict:
    """Read the operating state of the CPU a program is assigned to (RUN, STOP, HALT, STARTUP, DEFECT).

    Connects through the PG/PC interface configured in STEP 7. "unknown(0)" means the CPU could not be reached.
    """
    return bridge.call("cpu_state", project=project, program=program)


@mcp.tool()
def list_online_blocks(project: str, program: str | None = None, include_system_blocks: bool = False) -> list[dict]:
    """List the blocks currently loaded in the CPU. SFCs/SFBs are hidden unless include_system_blocks is true."""
    blocks = bridge.call("list_online_blocks", project=project, program=program)
    if not include_system_blocks:
        blocks = [b for b in blocks if b["type"] not in ("SFC", "SFB")]
    return blocks


@mcp.tool()
def compare_online_offline(project: str, program: str | None = None, blocks: list[str] | None = None) -> dict:
    """Compare offline blocks with the blocks in the CPU. Read-only.

    blocks defaults to every OB/FB/FC/DB/UDT of the program. Each block is reported as "identical",
    "different" or "not in CPU".
    """
    result = bridge.call("compare", project=project, program=program, blocks=blocks, timeout=max(DEFAULT_TIMEOUT, 900))
    result["blocks"] = [_describe_compare(b) for b in result["blocks"]]
    return result


@mcp.tool()
def upload_blocks(project: str, program: str | None = None, blocks: list[str] | None = None, overwrite: bool = False) -> dict:
    """Copy blocks from the CPU into the offline project. Does not change the PLC.

    blocks defaults to every OB/FB/FC/DB in the CPU. Existing offline blocks are kept unless overwrite is true.
    """
    return bridge.call("upload", project=project, program=program, blocks=blocks, overwrite=overwrite, timeout=max(DEFAULT_TIMEOUT, 900))


@mcp.tool()
def download_blocks(project: str, program: str | None = None, blocks: list[str] | None = None, overwrite: bool = True) -> dict:
    """Download offline blocks to the CPU. CHANGES THE RUNNING PLC PROGRAM.

    blocks defaults to every OB/FB/FC/DB/UDT of the program. With overwrite false, blocks that already exist
    in the CPU are not replaced. Always confirm with the user before calling this on a real plant.
    """
    _require_plc_writes()
    return bridge.call("download", project=project, program=program, blocks=blocks, overwrite=overwrite, timeout=max(DEFAULT_TIMEOUT, 900))


@mcp.tool()
def start_cpu(project: str, program: str | None = None, mode: Literal["warm_restart", "hot_restart"] = "warm_restart") -> dict:
    """Switch the CPU to RUN. STARTS THE MACHINE'S CONTROL PROGRAM; confirm with the user first.

    Fails if the CPU's mode switch is in STOP.
    """
    _require_plc_writes()
    return bridge.call("cpu_control", project=project, program=program, action=mode)


@mcp.tool()
def stop_cpu(project: str, program: str | None = None) -> dict:
    """Switch the CPU to STOP. HALTS THE MACHINE'S CONTROL PROGRAM; confirm with the user first."""
    _require_plc_writes()
    return bridge.call("cpu_control", project=project, program=program, action="stop")


@mcp.tool()
def compile_station(project: str, station: str, check_only: bool = False) -> dict:
    """Compile a station's hardware configuration (regenerates the offline System Data). Modifies the offline project.

    check_only runs just the consistency check and changes nothing. Configuration errors are returned in
    "error" with "consistent": false.
    """
    return bridge.call("compile_station", project=project, station=station, check_only=check_only, timeout=max(DEFAULT_TIMEOUT, 600))


@mcp.tool()
def get_block_ladder(project: str, blocks: list[str], program: str | None = None) -> list[dict]:
    """Show blocks as text-drawn ladder rungs, one per network.

    Bit logic, edges, coils, timers and counters are drawn as rungs; networks with other instructions
    (loads/transfers, jumps, calls, comparisons, maths) are shown as STL.
    """
    results = bridge.call("block_source", project=project, program=program, blocks=blocks, flags=0)
    for r in results:
        if "source" in r:
            r["ladder"] = stl_to_ladder(r.pop("source"))
    return results


@mcp.tool()
def export_block_ladder(project: str, blocks: list[str], output_dir: str, program: str | None = None) -> list[dict]:
    """Write each block as an HTML page with drawn ladder rungs (SVG), one file per block, into output_dir.

    Same coverage as get_block_ladder: networks that cannot be drawn appear as STL on the page. Returns the
    file paths; open them in a browser.
    """
    folder = Path(output_dir)
    if not folder.is_dir():
        raise ValueError(f"Directory '{output_dir}' does not exist.")
    results = bridge.call("block_source", project=project, program=program, blocks=blocks, flags=0)
    for r in results:
        if "source" in r:
            path = folder / f"{re.sub(r'[^A-Za-z0-9_-]', '_', r['block'])}.html"
            path.write_text(stl_to_ladder_html(r.pop("source"), f"{r['block']} - {project}"), encoding="utf-8")
            r["file"] = str(path)
    return results


@mcp.tool()
def import_station_config(project: str, config: str) -> dict:
    """Create a station from HW Config export text. Modifies the offline project.

    STEP 7 cannot update an existing station from text: the import always creates a NEW station (named with
    a "(n)" suffix if the name is taken) containing an empty S7 program. Workflow for editing hardware:
    get_station_config, change parameters or addresses in the text, import it here, compile_station, then
    bring the program across with copy_blocks and get_symbol_table/import_symbols. The original station is
    left untouched. Changing parameters and addresses is reliable; hand-writing new modules is fragile.
    """
    return bridge.call("import_station", project=project, config=config, timeout=max(DEFAULT_TIMEOUT, 600))


@mcp.tool()
def get_station_hardware(project: str, station: str) -> dict:
    """Show a station's racks, modules, I/O addresses, network addresses and DP slaves as a tree.

    Every object has a path used by the other hardware tools: "0.4" is rack 0 slot 4, "0.2.1" is
    interface 1 of the module in slot 2, "dp1.83" is the slave at address 83 on DP master system 1,
    and "dp1.83.1" is slot 1 of that slave.
    """
    return bridge.call("station_hardware", project=project, station=station)


@mcp.tool()
def get_module_parameters(project: str, station: str, module: str) -> dict:
    """List the parameters of one module or DP slave (path from get_station_hardware) with their current values."""
    return bridge.call("module_parameters", project=project, station=station, module=module)


def _walk_hardware(nodes: list[dict]):
    for node in nodes:
        yield node
        yield from _walk_hardware(node.get("modules", []))


def _check_address_overlap(project: str, station: str, module: str, input_address: int | None, output_address: int | None) -> None:
    """Refuse a new start address that would overlap another module's inputs or outputs."""
    hardware = bridge.call("station_hardware", project=project, station=station)
    roots = hardware["racks"] + [slave for system in hardware["dpMasterSystems"] for slave in system["slaves"]]
    modules = [m for m in _walk_hardware(roots) if "path" in m]
    target = next((m for m in modules if m["path"] == module), None)
    if target is None:
        raise ValueError(f"Module '{module}' not found (see get_station_hardware).")
    for kind, start in (("inputs", input_address), ("outputs", output_address)):
        if start is None:
            continue
        if not target.get(kind):
            raise ValueError(f"Module '{module}' has no {kind[:-1]} address.")
        end = start + target[kind][0]["length"] - 1
        for other in modules:
            if other["path"] == module:
                continue
            for used in other.get(kind, []):
                if start <= used["address"] + used["length"] - 1 and used["address"] <= end:
                    raise ValueError(
                        f"{kind[:-1].capitalize()} address {start}..{end} overlaps '{other['name']}' ({other['path']}) "
                        f"at {used['address']}..{used['address'] + used['length'] - 1}."
                    )


@mcp.tool()
def update_module(
    project: str,
    station: str,
    module: str,
    name: str | None = None,
    parameters: dict[str, str] | None = None,
    input_address: int | None = None,
    output_address: int | None = None,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
    router: str | None = None,
    mpi_address: int | None = None,
    profibus_address: int | None = None,
) -> dict:
    """Change a module in the existing station, in place. Modifies the offline project.

    parameters maps parameter names from get_module_parameters to new values, e.g.
    {"SCAN_CYCLE_MONITORING_TIME": "200"}; use "NAME[3]" for a parameter of channel 3. A value STEP 7 does
    not accept is rejected and the old value kept. input_address / output_address set the module's start
    address. ip_address, subnet_mask and router apply to the Ethernet interface (e.g. the "PN-IO" submodule
    of a CPU, or a CP); router="" switches the router off. STEP 7 does not check address overlaps here, so
    run compile_station (check_only=True first) afterwards.
    """
    # STEP 7 stores whatever it is given here, so validate what it does not.
    for label, value in (("ip_address", ip_address), ("subnet_mask", subnet_mask), ("router", router)):
        if value:
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                raise ValueError(f"{label} '{value}' is not a valid IPv4 address.") from None
    if input_address is not None or output_address is not None:
        _check_address_overlap(project, station, module, input_address, output_address)
    return bridge.call("update_module", project=project, station=station, module=module, name=name, parameters=parameters,
                       input_address=input_address, output_address=output_address, ip_address=ip_address,
                       subnet_mask=subnet_mask, router=router, mpi_address=mpi_address, profibus_address=profibus_address)


@mcp.tool()
def add_module(project: str, station: str, parent: str, slot: int, order_number: str, name: str = "", version: str = "") -> dict:
    """Insert a module into the existing station. Modifies the offline project.

    parent is the rack ("0") or DP slave ("dp1.5") path; order_number is the catalog number, e.g.
    "6ES7 321-1BH02-0AA0"; version is the firmware version ("V3.2") where the catalog has several.
    STEP 7 assigns the next free I/O addresses. Run compile_station afterwards.
    """
    return bridge.call("add_module", project=project, station=station, parent=parent, slot=slot,
                       order_number=order_number, name=name, version=version)


@mcp.tool()
def remove_module(project: str, station: str, module: str) -> dict:
    """Remove a module from the existing station. Modifies the offline project.

    Removing a CPU also removes its S7 program with all blocks; confirm with the user first.
    """
    return bridge.call("remove_module", project=project, station=station, module=module)


@mcp.tool()
def copy_blocks(
    project: str,
    source_program: str,
    target_program: str,
    blocks: list[str] | None = None,
    target_project: str | None = None,
    overwrite: bool = False,
) -> list[dict]:
    """Copy blocks from one S7 program to another (same or another project). Modifies the offline target.

    blocks defaults to every OB/FB/FC/DB/UDT. Existing blocks in the target are kept unless overwrite is true.
    """
    return bridge.call("copy_blocks", project=project, source_program=source_program, target_program=target_program,
                       blocks=blocks, target_project=target_project, overwrite=overwrite, timeout=max(DEFAULT_TIMEOUT, 900))


@mcp.tool()
def download_system_data(project: str, program: str | None = None) -> dict:
    """Download the compiled hardware configuration (System Data) to the CPU. CHANGES THE PLC.

    The CPU must be in STOP. Run compile_station first. Confirm with the user before calling this.
    """
    _require_plc_writes()
    return bridge.call("download_system_data", project=project, program=program, timeout=max(DEFAULT_TIMEOUT, 600))


@mcp.tool()
def memory_reset(project: str, program: str | None = None) -> dict:
    """Perform a memory reset (MRES) of the CPU: ERASES THE WHOLE PLC PROGRAM. The CPU must be in STOP.

    Confirm with the user before calling this.
    """
    _require_plc_writes()
    return bridge.call("cpu_control", project=project, program=program, action="memory_reset")


@mcp.tool()
def compress_memory(project: str, program: str | None = None) -> dict:
    """Compress the CPU's load memory to close gaps left by deleted or reloaded blocks. Changes the PLC."""
    _require_plc_writes()
    return bridge.call("cpu_control", project=project, program=program, action="compress")


# ------------------------------------------------------------- live PLC values

_clients: dict[tuple, S7Client] = {}
_clients_lock = threading.Lock()
_symbol_cache: dict[tuple, tuple[float, dict[str, dict[str, str]]]] = {}

Route = Literal["step7", "direct"]

# Best-effort texts for common diagnostic events; the event ID is always returned as well.
EVENT_TEXTS = {
    0x1381: "Request for manual warm restart", 0x1382: "Request for automatic warm restart",
    0x1383: "Request for manual hot restart", 0x1384: "Request for automatic hot restart",
    0x1385: "Request for manual cold restart", 0x1386: "Request for automatic cold restart",
    0x2521: "BCD conversion error", 0x2522: "Area length error when reading", 0x2523: "Area length error when writing",
    0x2524: "Area error when reading", 0x2525: "Area error when writing", 0x2526: "Timer number error",
    0x2527: "Counter number error", 0x253A: "DB not loaded", 0x253C: "FC not loaded", 0x253E: "FB not loaded",
    0x3501: "Cycle time exceeded", 0x3502: "OB request error", 0x3842: "Module OK", 0x3942: "Module removed or not responding",
    0x38C4: "Distributed I/O station returned", 0x39C4: "Distributed I/O station failure",
    0x4300: "Backed-up power on", 0x4301: "Mode transition from STOP to STARTUP", 0x4302: "Mode transition from STARTUP to RUN",
    0x4303: "STOP caused by stop switch", 0x4304: "STOP caused by PG stop operation or SFB 20",
    0x4307: "Memory reset started by PG", 0x4308: "Memory reset started by switch",
    0x4309: "Memory reset started automatically", 0x430E: "Memory reset executed",
    0x4562: "STOP caused by programming error (OB not loaded or not possible)",
    0x4563: "STOP caused by I/O access error (OB not loaded or not possible)",
    0x530D: "New startup information in STOP mode",
}
CPU_STATES = {1: "STOP", 2: "STOP", 3: "STOP", 4: "STOP", 5: "STARTUP", 6: "STARTUP", 7: "STARTUP",
              8: "RUN", 9: "RUN", 10: "HOLD", 13: "DEFECT"}


def _with_client(address: str, rack: int, slot: int, route: str, action):
    """Run action(client) on a cached connection, reconnecting once if the connection has gone stale."""
    key = (route, address, rack, slot)
    with _clients_lock:
        for attempt in (1, 2):
            client = _clients.get(key)
            try:
                if client is None:
                    transport = S7OnlineTransport(address, rack, slot) if route == "step7" else TcpTransport(address, rack, slot)
                    client = _clients[key] = S7Client(transport)
                return action(client)
            except (BridgeError, OSError, S7Error) as e:
                stale = client is not None and not isinstance(e, S7Error)
                if client is not None and (stale or attempt == 2):
                    _clients.pop(key, None)
                    client.close()
                if not stale or attempt == 2:
                    raise


def _symbols(project: str, program: str | None) -> dict[str, dict[str, str]]:
    key = (project, program)
    cached = _symbol_cache.get(key)
    if cached is None or time.time() - cached[0] > SYMBOL_CACHE_SECONDS:
        table = _parse_sdf(bridge.call("export_symbols", project=project, program=program)["sdf"])
        cached = _symbol_cache[key] = (time.time(), {s["name"].lower(): s for s in table})
    return cached[1]


def _resolve(item: str, project: str | None, program: str | None) -> Operand:
    """An item is an operand address ("MW 10", "DB5.DBX0.1", "MD 20:REAL") or a symbol name."""
    try:
        return parse_address(item)
    except ValueError as address_error:
        if project is None:
            raise ValueError(f"{address_error} To use symbol names, pass the project.") from None
        symbol = _symbols(project, program).get(item.strip().strip('"').lower())
        if symbol is None:
            raise ValueError(f"'{item}' is neither an operand address nor a symbol of this program.") from None
        try:
            return parse_address(symbol["address"], symbol["data_type"])
        except ValueError:
            raise ValueError(f"Symbol '{item}' ({symbol['address']}, {symbol['data_type']}) is not a simple value.") from None


def _read_operands(client: S7Client, operands: list[Operand]) -> list[Any]:
    values = []
    for operand, raw in zip(operands, client.read([o.item for o in operands])):
        values.append(raw if isinstance(raw, S7Error) else decode(operand, raw))
    return values


@mcp.tool()
def read_plc_values(
    address: str,
    items: list[str],
    project: str | None = None,
    program: str | None = None,
    rack: int = 0,
    slot: int = 2,
    route: Route = "step7",
) -> list[dict]:
    """Read current values from the CPU: inputs, outputs, memory bits, timers, counters and DB values.

    address: the CPU's MPI/DP station number (e.g. "2") or IP address.
    items: operand addresses ("I 0.0", "QB 4", "MW 10", "MD 20:REAL", "DB10.DBW4:INT", "T 5", "C 3") or
      symbol names from the program's symbol table (then pass project). ":TYPE" overrides the data type.
    route "step7" goes through STEP 7's PG/PC interface and reaches S7-PLCSIM when the simulator is running;
      "direct" opens a TCP connection straight to the IP address and never reaches PLCSIM.
    """
    results: list[dict] = []
    operands: list[Operand] = []
    for item in items:
        try:
            operand = _resolve(item, project, program)
            operands.append(operand)
            results.append({"item": item, "address": operand.address, "type": operand.data_type})
        except ValueError as e:
            results.append({"item": item, "error": str(e)})
    if operands:
        values = iter(_with_client(address, rack, slot, route, lambda c: _read_operands(c, operands)))
        for r in results:
            if "error" not in r:
                value = next(values)
                r["error" if isinstance(value, S7Error) else "value"] = str(value) if isinstance(value, S7Error) else value
    return results


@mcp.tool()
def write_plc_values(
    address: str,
    values: dict[str, bool | int | float | str],
    project: str | None = None,
    program: str | None = None,
    rack: int = 0,
    slot: int = 2,
    route: Route = "step7",
) -> list[dict]:
    """Write values into the CPU's memory. CHANGES A RUNNING MACHINE; confirm with the user first.

    values maps an operand address or symbol name (as in read_plc_values) to the new value: true/false for
    bits, numbers for BYTE/WORD/INT/DINT/REAL (hex as "W#16#00FF"), seconds for S5TIME, milliseconds for TIME.
    Timers ("T 5") take the remaining time in seconds and counters ("C 3") a count from 0 to 999.
    Inputs and outputs are overwritten again by the next process-image update. Each result includes the
    value read back afterwards.
    """
    _require_plc_writes()
    results: list[dict] = []
    for item, value in values.items():
        try:
            operand = _resolve(item, project, program)
            data = encode(operand, value)

            def write_and_read_back(client: S7Client, operand: Operand = operand, data: bytes = data) -> Any:
                client.write(operand.item, data)
                return _read_operands(client, [operand])[0]

            read_back = _with_client(address, rack, slot, route, write_and_read_back)
            results.append({"item": item, "address": operand.address, "type": operand.data_type, "written": value,
                            "read_back": str(read_back) if isinstance(read_back, S7Error) else read_back})
        except OverflowError:
            results.append({"item": item, "error": f"{value} is out of range for {operand.data_type}."})
        except (ValueError, S7Error) as e:
            results.append({"item": item, "error": str(e)})
    return results


def _text(raw: bytes) -> str:
    return raw.split(b"\x00")[0].decode("latin-1").strip()


def _szl_or_none(client: S7Client, szl_id: int, index: int = 0) -> list[bytes] | None:
    try:
        return client.read_szl(szl_id, index)
    except S7Error:
        return None


def _diagnostics(client: S7Client, entries: int) -> dict:
    info: dict[str, Any] = {}
    if records := _szl_or_none(client, 0x0424):
        info["state"] = CPU_STATES.get(records[0][3] & 0x0F, f"unknown({records[0][3]})")
    for record in _szl_or_none(client, 0x0011) or []:
        index = int.from_bytes(record[:2], "big")
        if index == 1:
            info["order_number"] = _text(record[2:22])
        elif index == 7:
            info["firmware"] = f"V{record[25]}.{record[26]}.{record[27]}"
    names = {1: "station_name", 2: "module_name", 5: "serial_number", 7: "module_type"}
    for record in _szl_or_none(client, 0x001C) or []:
        index = int.from_bytes(record[:2], "big")
        if index in names and _text(record[2:]):
            info[names[index]] = _text(record[2:])
    events = []
    for record in (_szl_or_none(client, 0x00A0, entries) or [])[:entries]:
        event_id = int.from_bytes(record[:2], "big")
        t = record[12:20].hex()
        event = {
            "time": f"{'19' if t[:2] >= '90' else '20'}{t[0:2]}-{t[2:4]}-{t[4:6]} {t[6:8]}:{t[8:10]}:{t[10:12]}.{t[12:15]}",
            "event_id": f"16#{event_id:04X}",
            "text": EVENT_TEXTS.get(event_id, "(no text available; look up the event ID in the STEP 7 help)"),
            "info": record[4:12].hex().upper(),
        }
        if record[2] != 0xFF:                  # priority class set: the event belongs to an OB
            event["ob"] = record[3]
        events.append(event)
    info["diagnostic_buffer"] = events
    return info


@mcp.tool()
def get_cpu_diagnostics(address: str, rack: int = 0, slot: int = 2, route: Route = "step7", entries: int = 10) -> dict:
    """Read the CPU's operating state, identification (order number, firmware) and newest diagnostic buffer entries.

    address and route work as in read_plc_values. Diagnostic entries are newest first; each has the event ID,
    a text for common events, and the raw additional information.
    """
    return _with_client(address, rack, slot, route, lambda c: _diagnostics(c, entries))


def _parse_diagnostic_export(text: str) -> list[dict]:
    """Split STEP 7's diagnostic buffer export into events (number, ID, title, details, direction, time)."""
    events = []
    for chunk in text.split("</EVENT_ID/>"):
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if len(lines) < 2 or "Event ID" not in lines[0]:
            continue
        event: dict[str, Any] = {"number": int(lines[0].split()[1]), "event_id": "16#" + lines[0].split("16#")[1].strip()}
        body = lines[1:]
        # The last line is "time<TAB>date"; the one before says whether the event is incoming or outgoing.
        if "\t" in body[-1]:
            clock, date = body.pop().split("\t")[:2]
            event["time"] = f"{date.strip()} {clock.strip()}"
        if body and body[-1].lower().endswith("event"):
            event["direction"] = body.pop()
        event["text"] = body[0] if body else ""
        if body[1:]:
            event["details"] = body[1:]
        events.append(event)
    return events


@mcp.tool()
def get_diagnostic_buffer(project: str, station: str, cpu: str = "0.2", entries: int = 10) -> dict:
    """Read the CPU's diagnostic buffer with STEP 7's full event texts and details, newest first.

    cpu is the CPU module's hardware path from get_station_hardware (rack 0, slot 2 by default). The CPU must
    be reachable online through STEP 7's PG/PC interface (S7-PLCSIM counts). Use get_cpu_diagnostics when
    only an address is known and no project is at hand.
    """
    result = bridge.call("diagnostic_buffer", project=project, station=station, module=cpu)
    events = _parse_diagnostic_export(result["text"])
    return {"cpu": result["cpu"], "total_events": len(events), "events": events[:entries]}


starter.register(mcp, _require_plc_writes)


if __name__ == "__main__":
    mcp.run()
