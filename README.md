# py-ntfs-quick-index

Fast Windows NTFS indexing and search for amd64 machines.

`pnqi` uses NTFS MFT enumeration for initial indexing and the NTFS USN Journal
for incremental refreshes. Indexes are stored as SQLite files named
`pnqi.index.sqlite` in the volume root, for example `C:\pnqi.index.sqlite`.

## Requirements

- Windows only
- amd64 / x86_64 CPU only
- Administrator privileges
- NTFS volumes only
- Python 3.10+

The program elevates only at startup through
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

Search with `*` wildcards. `*` matches any string, including `\`.

```powershell
pnqi search "C:\Users\*\Desktop\*.pdf"
```

Show descendants sorted by recursive size:

```powershell
pnqi sizes C:\Users --limit 100
```

Show only direct children:

```powershell
pnqi sizes C:\Users --direct
```

CLI progress bars use `tqdm`, and `Ctrl+C` cancels cleanly.

## GUI

```powershell
pnqi-gui
```

The GUI supports creating indexes, searching wildcard paths, browsing indexed
folders, and viewing recursive sizes. During long operations the interface is
locked except for Cancel. Cancelled index builds write only to a temporary
SQLite file and do not replace the existing index.

## Incremental Updates

On startup, and before searches or browsing, `pnqi` checks existing
`pnqi.index.sqlite` files and replays USN Journal changes into SQLite. If the
USN Journal was recreated or no longer contains the required history, `pnqi`
reports that the index must be recreated.
