from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from .errors import NotNtfsError, PnqiError
from .pathing import normalize_windows_path

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_HANDLE_EOF = 38
ERROR_JOURNAL_NOT_ACTIVE = 1179

FILE_ATTRIBUTE_DIRECTORY = 0x00000010

FSCTL_ENUM_USN_DATA = 0x000900B3
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_CREATE_USN_JOURNAL = 0x000900E7


class Handle:
    def __init__(self, value: int) -> None:
        if value == INVALID_HANDLE_VALUE:
            raise_last_error("CreateFileW failed")
        self.value = value

    def close(self) -> None:
        if self.value != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(self.value)
            self.value = INVALID_HANDLE_VALUE

    def __enter__(self) -> "Handle":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class MFT_ENUM_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("StartFileReferenceNumber", ctypes.c_ulonglong),
        ("LowUsn", ctypes.c_longlong),
        ("HighUsn", ctypes.c_longlong),
    ]


class USN_JOURNAL_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("UsnJournalID", ctypes.c_ulonglong),
        ("FirstUsn", ctypes.c_longlong),
        ("NextUsn", ctypes.c_longlong),
        ("LowestValidUsn", ctypes.c_longlong),
        ("MaxUsn", ctypes.c_longlong),
        ("MaximumSize", ctypes.c_ulonglong),
        ("AllocationDelta", ctypes.c_ulonglong),
    ]


class READ_USN_JOURNAL_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("StartUsn", ctypes.c_longlong),
        ("ReasonMask", wintypes.DWORD),
        ("ReturnOnlyOnClose", wintypes.DWORD),
        ("Timeout", ctypes.c_ulonglong),
        ("BytesToWaitFor", ctypes.c_ulonglong),
        ("UsnJournalID", ctypes.c_ulonglong),
    ]


class CREATE_USN_JOURNAL_DATA(ctypes.Structure):
    _fields_ = [
        ("MaximumSize", ctypes.c_ulonglong),
        ("AllocationDelta", ctypes.c_ulonglong),
    ]


@dataclass(frozen=True)
class VolumeInfo:
    root: str
    device: str
    serial: int
    filesystem: str


@dataclass(frozen=True)
class FileIdInfo:
    frn: int
    volume_serial: int
    attributes: int


@dataclass(frozen=True)
class JournalInfo:
    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int


@dataclass(frozen=True)
class UsnRecord:
    frn: int
    parent_frn: int
    usn: int
    reason: int
    attributes: int
    name: str
    timestamp: int


def raise_last_error(context: str) -> None:
    code = ctypes.get_last_error()
    raise PnqiError(f"{context}: Win32 error {code}: {ctypes.FormatError(code)}")


kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
kernel32.GetVolumePathNameW.restype = wintypes.BOOL
kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR,
    wintypes.DWORD,
]
kernel32.GetVolumeInformationW.restype = wintypes.BOOL
kernel32.GetFileInformationByHandle.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
]
kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
kernel32.GetLogicalDrives.argtypes = []
kernel32.GetLogicalDrives.restype = wintypes.DWORD


def logical_drive_roots() -> list[str]:
    mask = int(kernel32.GetLogicalDrives())
    if mask == 0:
        raise_last_error("GetLogicalDrives failed")
    return [f"{chr(ord('A') + index)}:\\" for index in range(26) if mask & (1 << index)]


def get_volume_info(path: str) -> VolumeInfo:
    buffer = ctypes.create_unicode_buffer(32768)
    if not kernel32.GetVolumePathNameW(path, buffer, len(buffer)):
        raise_last_error("GetVolumePathNameW failed")
    root = normalize_windows_path(buffer.value)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    fs = ctypes.create_unicode_buffer(256)
    if not kernel32.GetVolumeInformationW(
        root, None, 0, ctypes.byref(serial), ctypes.byref(max_component), ctypes.byref(flags), fs, len(fs)
    ):
        raise_last_error("GetVolumeInformationW failed")
    filesystem = fs.value.upper()
    if filesystem != "NTFS":
        raise NotNtfsError(f"{root} uses {filesystem or 'an unknown filesystem'}, not NTFS.")
    drive = root[:2]
    return VolumeInfo(root=root, device=f"\\\\.\\{drive}", serial=int(serial.value), filesystem=filesystem)


def open_volume(volume: VolumeInfo) -> Handle:
    handle = kernel32.CreateFileW(
        volume.device,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    return Handle(int(handle))


def open_path_for_info(path: str) -> Handle:
    handle = kernel32.CreateFileW(
        path,
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    return Handle(int(handle))


def get_file_id(path: str) -> FileIdInfo:
    with open_path_for_info(path) as handle:
        info = BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(handle.value, ctypes.byref(info)):
            raise_last_error("GetFileInformationByHandle failed")
        frn = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        return FileIdInfo(
            frn=frn,
            volume_serial=int(info.dwVolumeSerialNumber),
            attributes=int(info.dwFileAttributes),
        )


def _device_io_control(
    handle: Handle,
    code: int,
    in_buffer: ctypes.Structure | None,
    out_buffer: ctypes.Array[ctypes.c_char] | ctypes.Structure | None,
) -> tuple[bool, int, int]:
    bytes_returned = wintypes.DWORD(0)
    in_ptr = ctypes.byref(in_buffer) if in_buffer is not None else None
    in_size = ctypes.sizeof(in_buffer) if in_buffer is not None else 0
    out_ptr = ctypes.byref(out_buffer) if out_buffer is not None else None
    out_size = ctypes.sizeof(out_buffer) if out_buffer is not None else 0
    ok = kernel32.DeviceIoControl(
        handle.value,
        code,
        in_ptr,
        in_size,
        out_ptr,
        out_size,
        ctypes.byref(bytes_returned),
        None,
    )
    if ok:
        return True, int(bytes_returned.value), 0
    return False, int(bytes_returned.value), ctypes.get_last_error()


def create_usn_journal_if_needed(handle: Handle) -> None:
    data = CREATE_USN_JOURNAL_DATA(0x800000, 0x100000)
    ok, _returned, error = _device_io_control(handle, FSCTL_CREATE_USN_JOURNAL, data, None)
    if not ok:
        raise PnqiError(f"FSCTL_CREATE_USN_JOURNAL failed: Win32 error {error}: {ctypes.FormatError(error)}")


def query_usn_journal(handle: Handle) -> JournalInfo:
    data = USN_JOURNAL_DATA_V0()
    ok, _returned, error = _device_io_control(handle, FSCTL_QUERY_USN_JOURNAL, None, data)
    if not ok and error == ERROR_JOURNAL_NOT_ACTIVE:
        create_usn_journal_if_needed(handle)
        ok, _returned, error = _device_io_control(handle, FSCTL_QUERY_USN_JOURNAL, None, data)
    if not ok:
        raise PnqiError(f"FSCTL_QUERY_USN_JOURNAL failed: Win32 error {error}: {ctypes.FormatError(error)}")
    return JournalInfo(
        journal_id=int(data.UsnJournalID),
        first_usn=int(data.FirstUsn),
        next_usn=int(data.NextUsn),
        lowest_valid_usn=int(data.LowestValidUsn),
    )


def parse_usn_records(buffer: ctypes.Array[ctypes.c_char], start: int, end: int) -> list[UsnRecord]:
    records: list[UsnRecord] = []
    offset = start
    while offset + 60 <= end:
        record_length = int.from_bytes(bytes(buffer[offset : offset + 4]), "little")
        if record_length <= 0 or offset + record_length > end:
            break
        major = int.from_bytes(bytes(buffer[offset + 4 : offset + 6]), "little")
        if major == 2:
            frn = int.from_bytes(bytes(buffer[offset + 8 : offset + 16]), "little")
            parent = int.from_bytes(bytes(buffer[offset + 16 : offset + 24]), "little")
            usn = int.from_bytes(bytes(buffer[offset + 24 : offset + 32]), "little", signed=True)
            timestamp = int.from_bytes(bytes(buffer[offset + 32 : offset + 40]), "little", signed=True)
            reason = int.from_bytes(bytes(buffer[offset + 40 : offset + 44]), "little")
            attributes = int.from_bytes(bytes(buffer[offset + 52 : offset + 56]), "little")
            name_len = int.from_bytes(bytes(buffer[offset + 56 : offset + 58]), "little")
            name_off = int.from_bytes(bytes(buffer[offset + 58 : offset + 60]), "little")
            raw_name = bytes(buffer[offset + name_off : offset + name_off + name_len])
            name = raw_name.decode("utf-16le", errors="replace")
            records.append(
                UsnRecord(
                    frn=frn,
                    parent_frn=parent,
                    usn=usn,
                    reason=reason,
                    attributes=attributes,
                    name=name,
                    timestamp=timestamp,
                )
            )
        elif major == 3:
            frn = int.from_bytes(bytes(buffer[offset + 8 : offset + 24]), "little")
            parent = int.from_bytes(bytes(buffer[offset + 24 : offset + 40]), "little")
            usn = int.from_bytes(bytes(buffer[offset + 40 : offset + 48]), "little", signed=True)
            timestamp = int.from_bytes(bytes(buffer[offset + 48 : offset + 56]), "little", signed=True)
            reason = int.from_bytes(bytes(buffer[offset + 56 : offset + 60]), "little")
            attributes = int.from_bytes(bytes(buffer[offset + 68 : offset + 72]), "little")
            name_len = int.from_bytes(bytes(buffer[offset + 72 : offset + 74]), "little")
            name_off = int.from_bytes(bytes(buffer[offset + 74 : offset + 76]), "little")
            raw_name = bytes(buffer[offset + name_off : offset + name_off + name_len])
            name = raw_name.decode("utf-16le", errors="replace")
            records.append(
                UsnRecord(
                    frn=frn,
                    parent_frn=parent,
                    usn=usn,
                    reason=reason,
                    attributes=attributes,
                    name=name,
                    timestamp=timestamp,
                )
            )
        offset += record_length
    return records


def enum_usn_records(handle: Handle, high_usn: int, buffer_size: int = 8 * 1024 * 1024):
    data = MFT_ENUM_DATA_V0(0, 0, high_usn)
    buffer = ctypes.create_string_buffer(buffer_size)
    while True:
        ok, returned, error = _device_io_control(handle, FSCTL_ENUM_USN_DATA, data, buffer)
        if not ok:
            if error == ERROR_HANDLE_EOF:
                break
            raise PnqiError(f"FSCTL_ENUM_USN_DATA failed: Win32 error {error}: {ctypes.FormatError(error)}")
        if returned <= 8:
            break
        data.StartFileReferenceNumber = int.from_bytes(bytes(buffer[:8]), "little")
        for record in parse_usn_records(buffer, 8, returned):
            yield record


def read_usn_changes(
    handle: Handle,
    *,
    journal_id: int,
    start_usn: int,
    stop_usn: int,
    buffer_size: int = 8 * 1024 * 1024,
):
    data = READ_USN_JOURNAL_DATA_V0(start_usn, 0xFFFFFFFF, 0, 0, 0, journal_id)
    buffer = ctypes.create_string_buffer(buffer_size)
    while data.StartUsn < stop_usn:
        ok, returned, error = _device_io_control(handle, FSCTL_READ_USN_JOURNAL, data, buffer)
        if not ok:
            raise PnqiError(f"FSCTL_READ_USN_JOURNAL failed: Win32 error {error}: {ctypes.FormatError(error)}")
        if returned <= 8:
            break
        next_usn = int.from_bytes(bytes(buffer[:8]), "little", signed=True)
        records = parse_usn_records(buffer, 8, returned)
        if not records and next_usn == data.StartUsn:
            break
        for record in records:
            if record.usn < stop_usn:
                yield record
        data.StartUsn = max(next_usn, data.StartUsn + 1)
