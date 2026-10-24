//! Test-only native child, never installed. Captures the complete inherited
//! environment and actual CRT argv, plus EOF/content on its standard input.
use serde_json::json;
use std::{
    collections::BTreeMap,
    fs,
    io::{self, Read},
    time::{SystemTime, UNIX_EPOCH},
};
#[link(name = "kernel32")]
extern "system" {
    fn GetStdHandle(kind: u32) -> *mut std::ffi::c_void;
    fn GetFileType(handle: *mut std::ffi::c_void) -> u32;
}
fn main() {
    let entered = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    let env: BTreeMap<_, _> = std::env::vars().collect();
    let stdin_kind = unsafe { GetFileType(GetStdHandle((-10i32) as u32)) };
    let mut stdin = String::new();
    let read = io::stdin().read_to_string(&mut stdin);
    let file = std::env::var("ARGV_CAPTURE_FILE").unwrap_or_else(|_| {
        format!(
            "{}\\{}.json",
            std::env::var("ARGV_CAPTURE_DIR").unwrap(),
            std::process::id()
        )
    });
    let value = json!({"args":std::env::args().skip(1).collect::<Vec<_>>(),"env":env,"stdin":stdin,"stdin_closed":read.is_ok(),"stdin_kind":stdin_kind,"cwd":std::env::current_dir().unwrap(),"entered_at":entered});
    fs::write(file, value.to_string()).unwrap();
    std::process::exit(
        std::env::var("FAKE_CLAUDE_EXIT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0),
    );
}
