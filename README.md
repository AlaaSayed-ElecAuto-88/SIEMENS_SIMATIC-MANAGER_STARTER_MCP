# SIMATIC Manager + STARTER MCP

MCP server for two Siemens engineering tools: STEP 7 V5.x (SIMATIC Manager, "classic") and STARTER.
It lets an MCP client such as Claude browse and edit STEP 7 projects, work online with the CPU, read or
write live values, and read, change and download the parameters of SINAMICS and MICROMASTER drives.

Repository: `SIEMENS_SIMATIC-MANAGER_STARTER_MCP`

## How it works

```
                           +-- bridge/s7bridge.ps1 (32-bit PowerShell) -- COM ------- STEP 7 (projects, download, ...)
MCP client -- server.py ---+
 (stdio)   (64-bit Python) +-- s7client.py --+-- bridge/s7online_link.ps1 -- S7ONLINE -- PLCSIM or the real PLC
                                             +-- TCP port 102 ------------------------- real PLC (route "direct")
```

- STEP 7's automation interface (`Simatic.Simatic`) and its `S7ONLINE` driver (`s7onlinx.dll`) are 32-bit
  only, so 64-bit Python cannot load them. The server keeps 32-bit Windows PowerShell workers alive and
  sends them one JSON request at a time.
- Live values use a small built-in S7 protocol client. Through `S7ONLINE` it reaches whatever STEP 7's
  PG/PC interface reaches, which is S7-PLCSIM whenever the simulator is running.

| File | Purpose |
| --- | --- |
| `server.py` | MCP tools |
| `worker.py` | Runs a PowerShell worker and exchanges JSON lines with it |
| `bridge/s7bridge.ps1` | STEP 7 command interface (projects, blocks, compile, download, CPU control) |
| `bridge/S7HwInterop.cs` | Early-bound access to STEP 7's newer hardware interface |
| `bridge/s7online_link.ps1` | Carries S7 messages over the `S7ONLINE` access point |
| `s7client.py` | S7 protocol client: read/write memory, system status lists |
| `plc_values.py` | Operand addresses and data types |
| `ladder.py` | Draws STL networks as text ladder rungs |
| `s7online_gateway.py` | Optional standalone PLCSIM gateway for other S7 client programs |
| `starter.py` | MCP tools for drives configured with STARTER |
| `bridge/starter_bridge.ps1` | STARTER scripting interface (drive units, parameters, online functions) |

## Requirements

- Windows with STEP 7 V5.x installed and licensed (developed against V5.6 + HF1)
- Python 3.10+
- The Windows "Users" group needs write access to the STEP 7 folder and registry key, otherwise STEP 7
  refuses to start with message 256:131 ("Your user rights do not include the use of STEP 7"). Fix once
  from an elevated PowerShell:

  ```powershell
  icacls "C:\Program Files (x86)\Siemens\Step7" /grant "*S-1-5-32-545:(OI)(CI)M" /T /C /Q
  $k='HKLM:\SOFTWARE\WOW6432Node\Siemens\STEP7'; $a=Get-Acl $k; $a.AddAccessRule((New-Object Security.AccessControl.RegistryAccessRule('BUILTIN\Users','FullControl','ContainerInherit,ObjectInherit','None','Allow'))); Set-Acl $k $a
  ```

## Install

Instructions for a person, or for Claude Code when given this repository's link:

1. Clone or download the repository to a permanent folder.
2. Install the dependency: `pip install -r requirements.txt`
3. Register the server with the absolute path to `server.py`:

   ```
   claude mcp add simatic-manager --scope user -- python "C:\path\to\SIEMENS_SIMATIC-MANAGER_STARTER_MCP\server.py"
   ```

   Or copy `.mcp.example.json` to `.mcp.json` (project scope) or into `claude_desktop_config.json`
   (Claude Desktop) and fix the path.
4. Start a new session and call `step7_status`. If it fails with a DLL initialisation error, apply the
   permissions fix above.

## Tools

`project` is a project name or path. `program` is a program name or path and can be omitted when the
project has only one program.

### Read a project (nothing is changed)

| Tool | Purpose |
| --- | --- |
| `step7_status` | Check STEP 7 is reachable, report version |
| `list_projects` | Projects and libraries known to SIMATIC Manager |
| `get_project_tree` | Stations, S7 programs and their containers |
| `list_blocks` | Blocks of a program with header info |
| `get_block_source` | Decompile blocks to STL text (symbolic or absolute) |
| `get_block_ladder` | Blocks as text-drawn ladder rungs |
| `export_block_ladder` | Blocks as HTML pages with drawn ladder rungs (writes files only) |
| `list_sources` / `get_source` | Read the Sources folder |
| `get_symbol_table` | Symbols, optionally filtered |
| `get_station_config` | Hardware configuration as HW Config export text |
| `get_station_hardware` | Racks, modules, addresses and DP slaves as a tree |
| `get_module_parameters` | Parameters of one module or DP slave |

### Change the offline project

| Tool | Purpose |
| --- | --- |
| `import_source` | Store STL/SCL text as a source file |
| `compile_source` | Compile a source into blocks, returns the compiler log |
| `import_symbols` | Add or update symbols |
| `copy_blocks` | Copy blocks between programs or projects |
| `create_project` / `add_program` | New empty project / S7 program |
| `update_module` | Change a module in place: name, parameters, I/O addresses, IP / router / MPI / PROFIBUS address |
| `add_module` / `remove_module` | Insert or remove a module in an existing station |
| `import_station_config` | Create a station from (edited) HW Config text |
| `compile_station` | Compile a station's hardware configuration, or only check its consistency |
| `upload_blocks` | Copy blocks from the CPU into the project |

### Read from the PLC

| Tool | Purpose |
| --- | --- |
| `get_cpu_state` | RUN / STOP / HALT / STARTUP / DEFECT |
| `list_online_blocks` | Blocks loaded in the CPU |
| `compare_online_offline` | Per block: identical / different / not in CPU |
| `read_plc_values` | Live inputs, outputs, memory bits, timers, counters, DB values; by address or symbol name |
| `get_cpu_diagnostics` | State, order number, firmware, diagnostic buffer (by address, no project needed) |
| `get_diagnostic_buffer` | Diagnostic buffer with STEP 7's full event texts and details (needs the project) |

### Change the PLC (disabled by default)

| Tool | Purpose |
| --- | --- |
| `download_blocks` | Download offline blocks to the CPU |
| `download_system_data` | Download the compiled hardware configuration (CPU in STOP) |
| `start_cpu` / `stop_cpu` | Warm/hot restart, or STOP |
| `write_plc_values` | Write memory bits, outputs, DB values, timers and counters |
| `memory_reset` | Erase the CPU's program (CPU in STOP) |
| `compress_memory` | Compress the CPU's load memory |

These refuse to run unless the server is started with `S7_MCP_ALLOW_PLC_WRITES=1` (see the `env` block in
`.mcp.example.json`). They act on whatever STEP 7's PG/PC interface reaches: the real CPU, or S7-PLCSIM
when the simulator is running. Test with PLCSIM first.

## Drives (STARTER)

The `starter_*` tools work with SINAMICS (G120, S120, ...) and MICROMASTER 4 drive units that are
configured with STARTER inside a STEP 7 project. They need STARTER installed (developed against V5.4.2.1)
and run in their own worker (`bridge/starter_bridge.ps1`), which keeps the online connection between calls.

| Tool | Changes | Purpose |
| --- | --- | --- |
| `starter_status` | – | Check STARTER's scripting interface, report version |
| `starter_list_drives` | – | Drive units and their drive objects (Control Unit, drives, infeeds) |
| `starter_read_parameters` | – | Parameter values with description, unit, limits, selection text |
| `starter_search_parameters` | – | Find parameters by number or description |
| `starter_read_faults` | – | Fault and alarm buffers (meaningful online) |
| `starter_get_connection` | – | How STARTER reaches the drive unit |
| `starter_export_drive_unit` | – | XML backup of a drive unit |
| `starter_write_parameters` | project, or **drive** when online | Change parameters |
| `starter_add_drive_unit` | project | Insert a SINAMICS drive unit |
| `starter_add_drive_object` | project | Insert a drive, infeed or terminal module into an S120 unit |
| `starter_set_connection` | project | Change access point / address |
| `starter_set_online` | – | Connect to or disconnect from a drive unit |
| `starter_upload` | project | Load the drive's parameters into the project |
| `starter_download` | **drive** | Download the project's parameters to the drive |
| `starter_ram_to_rom` | **drive** | Save the drive's parameters retentively |

Tools marked **drive** need `S7_MCP_ALLOW_PLC_WRITES=1`, as does `starter_write_parameters` while the drive
unit is online. A drive object is addressed by the drive unit's name plus the object's name; the object
can be omitted when the unit has only one (G120, MICROMASTER). STARTER enforces value ranges and its own
parameter locks (for example values fixed by the telegram selection p922) and the reason is passed on.

- MICROMASTER drive units cannot be created by script, only read and changed where they already exist.
- A project last saved by an older STARTER may ask to be converted when first opened; that dialog blocks
  the call until it times out. Open such a project once in STARTER itself first.
- Tested offline on G120 (CU240E-2 PN), G120C and S120 (CU320-2 DP) drive units. Not yet tested: anything
  online (going online, live values, upload, download, RAM to ROM, fault buffers) and MICROMASTER units.

## Live values

`read_plc_values` and `write_plc_values` take the CPU's `address` (MPI/DP station number such as `"2"`, or
an IP address) and a list of items:

- operand addresses: `I 0.0`, `QB 4`, `MW 10`, `MD 20:REAL`, `DB10.DBX0.1`, `DB10.DBW4:INT`, `T 5`, `C 3`
  (German mnemonics `E`, `A`, `Z` also work); `:TYPE` overrides the data type
- symbol names from the program's symbol table, when `project` is given

`route="step7"` (default) goes through STEP 7's PG/PC interface, so it follows the same path as SIMATIC
Manager and lands in PLCSIM when the simulator is running. `route="direct"` opens TCP port 102 on the IP
address without STEP 7; it never reaches PLCSIM.

## PLCSIM gateway for other programs

Classic S7-PLCSIM has no network port. `s7online_gateway.py` gives it one, so that other S7 client
software (python-snap7, an HMI, a SCADA test) can talk to the simulator:

```
python s7online_gateway.py --address 2 --port 1102
```

Then connect the client to `127.0.0.1`, port 1102. The server itself does not need the gateway.

## Editing the hardware configuration

In place, in the existing station (the program is untouched): `get_station_hardware` to find a module's
path, `get_module_parameters`, then `update_module`, `add_module` or `remove_module`, and finally
`compile_station`. Parameter values STEP 7 does not accept are rejected and the old value is kept. STEP 7
itself does not check IP addresses or I/O address overlaps on this path, so the server does: an invalid
IPv4 address or a start address that overlaps another module is refused.

Network properties, the I/O address commit and the diagnostic buffer export are only available on a newer
STEP 7 interface (`IS7Module6`) that scripting cannot reach; `bridge/S7HwInterop.cs` binds it directly and
is compiled by the bridge at start-up.

The text route remains as an alternative; it always creates a new station with an empty program:
`get_station_config`, edit the text, `import_station_config`, `compile_station`, then `copy_blocks` and
`get_symbol_table` / `import_symbols` to bring the program across. The original station is left untouched.

## Notes

- Close the project's objects in the STEP 7 editors (LAD/STL/FBD, symbol editor) before writing; open
  editors lock them.
- `start_cpu` and `stop_cpu` fail when the CPU's mode switch is in STOP (in PLCSIM: tick RUN-P).
- While PLCSIM is running it answers for every MPI address, so a wrong `address` still gets values.
- Blocks uploaded from a CPU only decompile with `symbolic=True`.
- The ladder tools draw bit logic, edges, compares, coils, timers, counters, and boxes for moves,
  arithmetic and block calls (including the EN pattern). Networks with other jumps or instructions stay as
  STL. On a 107-block production project, 84% of 1891 networks were drawn.
- `get_cpu_diagnostics` has texts for common events only; `get_diagnostic_buffer` has STEP 7's full texts.
- A call that gets no answer within `S7_MCP_TIMEOUT` seconds (default 180) restarts the worker.
- Worker errors outside a request are written to `%TEMP%\s7mcp_*_stderr.log`.
- Not possible: forcing values, opening know-how-protected blocks.

## Test status

Tested against S7-PLCSIM V5.4: project browsing, source import and compile, block download/upload/compare,
CPU start/stop, live read/write (including timers and counters), both diagnostic buffer tools, station
import and compile, block copy, in-place hardware edits (central rack, Ethernet interfaces, DP slaves),
hardware configuration download, memory reset and compress. Not yet tested: anything on a real CPU,
including `route="direct"`.

SCL sources need a working S7-SCL package. On the development PC (STEP 7 V5.6 with S7-SCL V5.7) STEP 7
refused the import with "software package 'S7-SCL' ... not installed or exists in an earlier version".

## Credits

The `S7ONLINE` request block layout follows the open-source NetToPLCsim project by Thomas Wiens.
