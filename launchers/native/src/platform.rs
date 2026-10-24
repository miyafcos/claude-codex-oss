//! Small Windows API boundary. No registry writes, persistent handles, or global
//! changes to the console's inherited CTRL_C ignore flag.
use std::{ffi::OsString, io, os::windows::ffi::OsStringExt, ptr};

type Handle = *mut std::ffi::c_void;
#[repr(C)]
struct Guid {
    a: u32,
    b: u16,
    c: u16,
    d: [u8; 8],
}
#[repr(C)]
struct SystemTime {
    year: u16,
    month: u16,
    dow: u16,
    day: u16,
    hour: u16,
    minute: u16,
    second: u16,
    ms: u16,
}

#[link(name = "kernel32")]
extern "system" {
    fn CreateMutexW(attributes: Handle, owner: i32, name: *const u16) -> Handle;
    fn WaitForSingleObject(handle: Handle, millis: u32) -> u32;
    fn ReleaseMutex(handle: Handle) -> i32;
    fn CloseHandle(handle: Handle) -> i32;
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> Handle;
    fn SetConsoleCtrlHandler(
        handler: Option<unsafe extern "system" fn(u32) -> i32>,
        add: i32,
    ) -> i32;
    fn ExpandEnvironmentStringsW(input: *const u16, output: *mut u16, size: u32) -> u32;
    fn GetSystemTime(time: *mut SystemTime);
    fn GetCommandLineW() -> *const u16;
    fn GetConsoleCP() -> u32;
    fn GetACP() -> u32;
}
#[link(name = "shell32")]
extern "system" {
    fn SHGetKnownFolderPath(id: *const Guid, flags: u32, token: Handle, path: *mut *mut u16)
        -> i32;
}
#[link(name = "ole32")]
extern "system" {
    fn CoTaskMemFree(p: Handle);
    fn CoCreateGuid(guid: *mut Guid) -> i32;
}
#[link(name = "advapi32")]
extern "system" {
    fn RegOpenKeyExW(
        key: Handle,
        sub: *const u16,
        options: u32,
        access: u32,
        result: *mut Handle,
    ) -> i32;
    fn RegQueryValueExW(
        key: Handle,
        name: *const u16,
        reserved: *mut u32,
        kind: *mut u32,
        data: *mut u8,
        len: *mut u32,
    ) -> i32;
    fn RegCloseKey(key: Handle) -> i32;
}
fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(Some(0)).collect()
}
fn command_tail(command: &[u16]) -> OsString {
    // argv[0] uses the executable-name grammar: quotes delimit it,
    // backslashes are literal. Preserve the rest, including its quoting.
    let mut i = 0;
    let mut quoted = false;
    while i < command.len() {
        match command[i] {
            34 => quoted = !quoted,
            32 | 9 if !quoted => break,
            _ => {}
        }
        i += 1;
    }
    while i < command.len() && matches!(command[i], 32 | 9) {
        i += 1;
    }
    OsString::from_wide(&command[i..])
}
pub fn argument_tail() -> io::Result<OsString> {
    unsafe {
        let p = GetCommandLineW();
        if p.is_null() {
            return Err(io::Error::last_os_error());
        }
        let mut n = 0;
        while *p.add(n) != 0 {
            n += 1;
        }
        Ok(command_tail(std::slice::from_raw_parts(p, n)))
    }
}

pub fn known_folder(roaming: bool) -> io::Result<OsString> {
    let id = if roaming {
        Guid {
            a: 0x3eb685db,
            b: 0x65f9,
            c: 0x4cf6,
            d: [0xa0, 0x3a, 0xe3, 0xef, 0x65, 0x72, 0x9f, 0x3d],
        }
    } else {
        Guid {
            a: 0x5e6c858f,
            b: 0x0e22,
            c: 0x4760,
            d: [0x9a, 0xfe, 0xea, 0x33, 0x17, 0xb6, 0x71, 0x73],
        }
    };
    unsafe {
        let mut p = ptr::null_mut();
        if SHGetKnownFolderPath(&id, 0, ptr::null_mut(), &mut p) < 0 {
            return Err(io::Error::last_os_error());
        }
        let mut n = 0;
        while *p.add(n) != 0 {
            n += 1;
        }
        let value = OsString::from_wide(std::slice::from_raw_parts(p, n));
        CoTaskMemFree(p.cast());
        Ok(value)
    }
}

/// .NET's per-user environment reader expands REG_EXPAND_SZ against the
/// process environment. An unsupported registry type is never coerced.
pub fn user_env(name: &str) -> Result<Option<String>, &'static str> {
    unsafe {
        let mut key = ptr::null_mut();
        let status = RegOpenKeyExW(
            0x80000001u32 as i32 as isize as Handle,
            wide("Environment").as_ptr(),
            0,
            0x20019,
            &mut key,
        );
        if status == 2 {
            return Ok(None);
        }
        if status != 0 {
            return Err("user-env-unreadable");
        }
        let result = (|| {
            let mut kind = 0;
            let mut len = 0;
            let status = RegQueryValueExW(
                key,
                wide(name).as_ptr(),
                ptr::null_mut(),
                &mut kind,
                ptr::null_mut(),
                &mut len,
            );
            if status == 2 {
                return Ok(None);
            }
            if status != 0 {
                return Err("user-env-unreadable");
            }
            if kind != 1 && kind != 2 {
                return Err("user-env-type");
            }
            let mut data = vec![0u16; (len as usize / 2) + 1];
            if RegQueryValueExW(
                key,
                wide(name).as_ptr(),
                ptr::null_mut(),
                &mut kind,
                data.as_mut_ptr().cast(),
                &mut len,
            ) != 0
            {
                return Err("user-env-unreadable");
            }
            if kind != 1 && kind != 2 {
                return Err("user-env-type");
            }
            if len % 2 != 0 {
                return Err("user-env-encoding");
            }
            decode_user_value(kind, &data).map(Some)
        })();
        RegCloseKey(key);
        result
    }
}

fn decode_user_value(kind: u32, data: &[u16]) -> Result<String, &'static str> {
    if kind != 1 && kind != 2 {
        return Err("user-env-type");
    }
    let n = data.iter().position(|x| *x == 0).unwrap_or(data.len());
    let value = String::from_utf16(&data[..n]).map_err(|_| "user-env-encoding")?;
    if kind == 1 {
        return Ok(value);
    }
    // The PS host adjusts these variables before the launcher runs. Do not
    // expand a registry value against a different native-host value.
    let lower = value.to_ascii_lowercase();
    if lower.contains("%psmodulepath%")
        || lower.contains("%psexecutionpolicypreference%")
        || lower.contains("%fugu_api_key%")
    {
        return Err("user-env-host-expansion");
    }
    unsafe {
        let input = wide(&value);
        let size = ExpandEnvironmentStringsW(input.as_ptr(), ptr::null_mut(), 0);
        if size == 0 {
            return Err("user-env-unreadable");
        }
        let mut out = vec![0u16; size as usize];
        let count = ExpandEnvironmentStringsW(input.as_ptr(), out.as_mut_ptr(), size);
        if count == 0 || count > size {
            return Err("user-env-unreadable");
        }
        String::from_utf16(&out[..count as usize - 1]).map_err(|_| "user-env-encoding")
    }
}

pub struct Mutex {
    handle: Handle,
    held: bool,
}
#[derive(Debug, PartialEq)]
pub enum Acquired {
    Yes,
    Abandoned,
    Timeout,
}
impl Mutex {
    pub fn new(name: &str) -> io::Result<Self> {
        let handle = unsafe { CreateMutexW(ptr::null_mut(), 0, wide(name).as_ptr()) };
        if handle.is_null() {
            Err(io::Error::last_os_error())
        } else {
            Ok(Self {
                handle,
                held: false,
            })
        }
    }
    pub fn acquire(&mut self, millis: u32) -> io::Result<Acquired> {
        match unsafe { WaitForSingleObject(self.handle, millis) } {
            0 => {
                self.held = true;
                Ok(Acquired::Yes)
            }
            0x80 => {
                self.held = true;
                Ok(Acquired::Abandoned)
            }
            0x102 => Ok(Acquired::Timeout),
            _ => Err(io::Error::last_os_error()),
        }
    }
}
impl Drop for Mutex {
    fn drop(&mut self) {
        unsafe {
            if self.held {
                ReleaseMutex(self.handle);
            }
            CloseHandle(self.handle);
        }
    }
}
pub fn process_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    unsafe {
        let handle = OpenProcess(0x1000 | 0x100000, 0, pid as u32);
        if handle.is_null() {
            return false;
        }
        // 259 is a valid process exit code as well as STILL_ACTIVE. A signaled
        // process handle is definitive, as in Process.HasExited.
        let alive = WaitForSingleObject(handle, 0) == 0x102;
        CloseHandle(handle);
        alive
    }
}
// Console.InputEncoding as Windows PowerShell 5.1 resolves it: the console's
// input code page, or the ANSI code page (GetEncoding(0)) without a console.
pub fn console_input_is_utf8() -> bool {
    let cp = unsafe { GetConsoleCP() };
    (if cp == 0 { unsafe { GetACP() } } else { cp }) == 65001
}
unsafe extern "system" fn console_handler(event: u32) -> i32 {
    if event <= 1 {
        1
    } else {
        0
    }
}
pub struct ChildWaitGuard;
impl ChildWaitGuard {
    pub fn new() -> Self {
        unsafe {
            SetConsoleCtrlHandler(Some(console_handler), 1);
        }
        Self
    }
}
impl Drop for ChildWaitGuard {
    fn drop(&mut self) {
        unsafe {
            SetConsoleCtrlHandler(Some(console_handler), 0);
        }
    }
}
pub fn guid() -> io::Result<String> {
    let mut g = Guid {
        a: 0,
        b: 0,
        c: 0,
        d: [0; 8],
    };
    if unsafe { CoCreateGuid(&mut g) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(format!(
        "{:08x}-{:04x}-{:04x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        g.a, g.b, g.c, g.d[0], g.d[1], g.d[2], g.d[3], g.d[4], g.d[5], g.d[6], g.d[7]
    ))
}
pub fn timestamp() -> String {
    let mut t = SystemTime {
        year: 0,
        month: 0,
        dow: 0,
        day: 0,
        hour: 0,
        minute: 0,
        second: 0,
        ms: 0,
    };
    unsafe {
        GetSystemTime(&mut t);
    }
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        t.year, t.month, t.day, t.hour, t.minute, t.second, t.ms
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn original_argument_tail() {
        let input =
            wide("\"D:\\with spaces\\claude-codex-native.exe\"  -p \"hello world\" \"\" --help\n");
        assert_eq!(
            command_tail(&input[..input.len() - 1]),
            OsString::from("-p \"hello world\" \"\" --help\n")
        );
    }
    #[test]
    fn registry_string_types() {
        assert_eq!(
            decode_user_value(1, &wide("%SystemRoot%")),
            Ok("%SystemRoot%".into())
        );
        assert_eq!(
            decode_user_value(2, &wide("%SystemRoot%\\test")),
            Ok(format!("{}\\test", std::env::var("SystemRoot").unwrap()))
        );
        assert_eq!(decode_user_value(4, &wide("123")), Err("user-env-type"));
        assert_eq!(decode_user_value(1, &[0xd800, 0]), Err("user-env-encoding"));
        assert_eq!(
            decode_user_value(2, &wide("%PSModulePath%")),
            Err("user-env-host-expansion")
        );
        assert_eq!(
            decode_user_value(2, &wide("%FUGU_API_KEY%")),
            Err("user-env-host-expansion")
        );
    }
}
