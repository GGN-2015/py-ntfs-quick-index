# py-ntfs-quick-index

Fast filesystem indexing and search, with a Windows NTFS fast path.

On Windows, `pnqi` uses NTFS MFT enumeration for initial indexing and the NTFS
USN Journal for incremental refreshes. On Linux and macOS, including mounted
NTFS volumes, `pnqi` uses a portable filesystem scan fallback. Portable refresh
reconciles the SQLite index from the current filesystem state instead of using
USN Journal deltas.

Indexes are stored as SQLite files named `pnqi.index.sqlite` in the volume or
mount root, for example `C:\pnqi.index.sqlite` or
`/mnt/external/pnqi.index.sqlite`.

## Requirements

- Windows, Linux, or macOS
- Windows fast mode requires amd64 / x86_64, administrator privileges, and NTFS
  volumes
- Linux/macOS portable mode works with readable mounted filesystems, including
  mounted NTFS volumes, and requires write access to the mount root for the
  SQLite index
- Python 3.10+

On Windows, the program elevates only at startup through
[`py-admin-launch`](https://pypi.org/project/py-admin-launch/). Internal library
calls require the already-elevated process and do not trigger additional UAC
prompts.

## Install

Install the latest release from PyPI:

```powershell
python -m pip install py-ntfs-quick-index
```

Upgrade an existing install:

```powershell
python -m pip install --upgrade py-ntfs-quick-index
```

For local development from a checkout:

```powershell
python -m pip install -e .
```

## CLI

Create or replace an index for a folder:

```powershell
pnqi index C:\
```

List local drives or refresh one existing index:

```powershell
pnqi drives
pnqi refresh C:\
```

Search with `*` wildcards. `*` matches any string, including `\`. Results are
sorted by displayed size descending.

```powershell
pnqi search "C:\Users\*\Desktop\*.pdf"
pnqi search "Users\*\Desktop\*.pdf" --drive C:\ --details --limit 100
```

Browse direct children like the GUI folder browser, including each child's share
of the current folder's recursive size:

```powershell
pnqi browse C:\Users --limit 100
```

Show descendants sorted by recursive size:

```powershell
pnqi sizes C:\Users --limit 100 --details
```

Show only direct children:

```powershell
pnqi sizes C:\Users --direct
```

CLI progress bars use `tqdm`, and `Ctrl+C` cancels cleanly.
The CLI exposes the same core capabilities as the GUI: drive discovery, drive
index refresh, index creation, wildcard search with optional drive scoping,
direct folder browsing with size shares, and recursive size listings.

## GUI

```powershell
pnqi-gui
```

The GUI supports creating indexes, searching wildcard paths, browsing indexed
folders, and viewing recursive sizes. On launch, the GUI asks which local drive
to load, refreshes that drive's index state, and starts browsing from the drive
root. Use Change Drive to switch disks; searches are constrained to the selected
drive. The folder browser shows each direct child's share of the current
folder's total recursive size; search and size result lists stay focused on
size, type, time, and path. During long operations the interface is locked
except for Cancel. Long tasks run in a worker process so NTFS MFT scans and
indexed searches do not stall the Tk event loop. Search results stream back in
small batches so large result sets remain cancellable, and the Max rows control
limits how many sorted matches are displayed. Cancelled index builds write only
to a temporary SQLite file and do not replace the existing index.

## Build a GUI EXE

To build the GUI as a single-file Windows executable from a checkout:

```bat
scripts\build_gui_exe.bat
```

The script creates an isolated build virtual environment under `.build`, installs
the current project and PyInstaller there, and writes `dist\exe\pnqi-gui.exe`.
It does not change the Poetry package configuration or runtime dependencies.

## Incremental Updates

When a GUI drive is selected, and before searches or browsing on Windows, `pnqi`
checks the drive's existing `pnqi.index.sqlite` file and replays USN Journal
changes into SQLite. Folder sizes are maintained as recursive sums of all
descendant files; older indexes are recalculated once when opened. Incremental
updates replace stale records that still occupy a normalized path before writing
the new file record. If the USN Journal was recreated or no longer contains the
required history, `pnqi` reconciles the existing SQLite index from the current
filesystem and then resumes incremental updates.

On Linux and macOS, refresh always reconciles from the current filesystem state
because the Windows NTFS USN Journal API is not available.
