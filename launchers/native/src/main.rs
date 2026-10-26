#[path = "json.rs"]
mod checked_json;
mod config;
mod platform;
mod proxy;
use config::{Env, Paths};
use serde_json::{json, Value};
use std::os::windows::process::CommandExt;
use std::{
    ffi::{OsStr, OsString},
    fs::{self, OpenOptions},
    io::{self, Write},
    path::Path,
    process::{self, Command, Stdio},
    time::Instant,
};

fn write_trace(env: &Env, path: &str, reason: &str, check: Option<&str>, start: Instant) {
    if let Some(file) = config::get(env, "CLAUDE_CODEX_NATIVE_TRACE_FILE") {
        let elapsed = start.elapsed().as_secs_f64() * 1000.0;
        let launch_ms = if path == "native" && reason == "eligible" {
            Some(elapsed)
        } else {
            None
        };
        let handoff_ms = if path == "fallback" {
            Some(elapsed)
        } else {
            None
        };
        let row = json!({"time":platform::timestamp(),"path":path,"reason":reason,"proxy_check":check,"launch_ms":launch_ms,"handoff_ms":handoff_ms});
        if let Some(parent) = Path::new(file)
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
        {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(file) {
            let mut line = row.to_string();
            line.push('\n');
            let _ = file.write_all(line.as_bytes());
        }
    }
}
struct TraceRecorder<'a> {
    env: &'a Env,
    start: Instant,
    enabled: bool,
    recorded: bool,
}
impl TraceRecorder<'_> {
    fn record(&mut self, path: &str, reason: &str, check: Option<&str>) {
        if self.enabled && !self.recorded {
            write_trace(self.env, path, reason, check, self.start);
            self.recorded = true;
        }
    }
}
impl Drop for TraceRecorder<'_> {
    fn drop(&mut self) {
        self.record("native", "launcher-error", None);
    }
}
fn use_fallback(
    mode: &str,
    trace: &mut TraceRecorder<'_>,
    ps: &Path,
    raw: &[OsString],
    tail: &OsStr,
    reason: &str,
) -> io::Result<i32> {
    if mode.eq_ignore_ascii_case("probe") {
        println!("null");
        return Ok(0);
    }
    if mode.eq_ignore_ascii_case("plan") {
        println!(
            "{}",
            json!({"path":"fallback","reason":reason,"proxy_check":null,"env_diff":{},"args":raw.iter().map(|s|s.to_string_lossy()).collect::<Vec<_>>()})
        );
        return Ok(0);
    }
    trace.record("fallback", reason, None);
    fallback(ps, tail)
}
fn fallback(ps: &Path, tail: &OsStr) -> io::Result<i32> {
    if !ps.exists() {
        eprintln!(
            "[claude-codex] PowerShell launcher missing: {}",
            ps.display()
        );
        return Ok(1);
    }
    let system = std::env::var_os("SystemRoot").unwrap_or_else(|| OsString::from("C:\\Windows"));
    let host = Path::new(&system).join(r"System32\WindowsPowerShell\v1.0\powershell.exe");
    let mut quoted_ps = OsString::from("\"");
    quoted_ps.push(ps.as_os_str());
    quoted_ps.push("\"");
    let mut child = Command::new(host)
        .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        .raw_arg(quoted_ps)
        .raw_arg(tail)
        .spawn()?;
    let _guard = platform::ChildWaitGuard::new();
    Ok(child.wait()?.code().unwrap_or(1))
}
fn run(start: Instant) -> io::Result<i32> {
    let exe = std::env::current_exe()?;
    let ps = exe.with_file_name("claude-codex.ps1");
    let raw: Vec<_> = std::env::args_os().skip(1).collect();
    let tail = platform::argument_tail()?;
    let raw_env: Vec<_> = std::env::vars_os().collect();
    let env: Env = raw_env
        .iter()
        .filter_map(|(k, v)| Some((k.to_str()?.to_ascii_uppercase(), v.to_str()?.to_string())))
        .collect();
    let mode = config::get(&env, "CLAUDE_CODEX_NATIVE_SELFTEST").unwrap_or("");
    let mut trace = TraceRecorder {
        env: &env,
        start,
        enabled: !mode.eq_ignore_ascii_case("probe") && !mode.eq_ignore_ascii_case("plan"),
        recorded: false,
    };
    if raw_env
        .iter()
        .any(|(k, v)| k.to_str().is_none() || v.to_str().is_none())
    {
        return use_fallback(mode, &mut trace, &ps, &raw, &tail, "environment-encoding");
    }
    // Delegate before paths/proxy/auth are resolved, including offline listing.
    let child_args: Vec<String> = raw.iter().map(|s| s.to_string_lossy().into_owned()).collect();
    if config::has_subagent_options(&child_args) {
        return use_fallback(mode, &mut trace, &ps, &raw, &tail, "explicit-subagent-options");
    }
    if !mode.eq_ignore_ascii_case("probe") && config::empty_ps_environment(&env) {
        return use_fallback(mode, &mut trace, &ps, &raw, &tail, "environment-empty");
    }
    let paths = match Paths::new(&exe, &env) {
        Ok(p) => p,
        Err(_) => {
            return use_fallback(
                mode,
                &mut trace,
                &ps,
                &raw,
                &tail,
                "profile-path-unavailable",
            )
        }
    };
    if mode.eq_ignore_ascii_case("probe") {
        let probe = proxy::probe(&paths, &env);
        println!("{}", probe.map(|p| p.json()).unwrap_or(Value::Null));
        return Ok(0);
    }
    let args: Option<Vec<_>> = raw.iter().map(|s| s.to_str().map(str::to_owned)).collect();
    let eligible = args
        .as_ref()
        .ok_or("argument-encoding")
        .and_then(|a| config::preflight(&paths, a, &env))
        .and_then(|eligible| {
            config::validate_command_tail(
                tail.to_str().ok_or("argument-encoding")?,
                args.as_ref().unwrap(),
            )?;
            Ok(eligible)
        });
    let eligible = match eligible {
        Ok(e) => e,
        Err(reason) => {
            return use_fallback(mode, &mut trace, &ps, &raw, &tail, reason);
        }
    };
    let probe = if proxy::enabled(&eligible.env) {
        proxy::probe(&paths, &eligible.env)
    } else {
        None
    };
    let check = proxy::fast_path(probe.as_ref(), &paths, &eligible.base, &eligible.token);
    // Build an in-memory plan before any filesystem mutations. Diagnostic
    // paths return here and do not even create the trace's parent directory.
    let launch = config::build(&paths, &eligible)?;
    if mode.eq_ignore_ascii_case("plan") {
        println!(
            "{}",
            json!({"path":"native","reason":"eligible","proxy_check":check,"env_diff":config::env_diff(&env,&launch.env),"args":launch.args,"mapping":launch.mapping,"probe":probe.map(|p|p.json())})
        );
        return Ok(0);
    }
    for dir in [&paths.config, &paths.data, &paths.state] {
        fs::create_dir_all(dir)?;
    }
    let code = proxy::ensure(
        probe.as_ref(),
        &check,
        &paths,
        &eligible.env,
        &eligible.base,
        &eligible.token,
    )?;
    if code != 0 {
        trace.record("native", "proxy-start-failed", Some(&check));
        return Ok(code);
    }
    if let Some(notice) = launch.notice {
        eprintln!("{notice}");
    }
    if let Some((file, body)) = launch.mapping {
        fs::create_dir_all(file.parent().unwrap())?;
        fs::write(file, body)?;
    }
    let mut child = Command::new(&paths.claude);
    child.raw_arg(config::legacy_arguments(&launch.args));
    for (k, v) in &launch.env {
        if env.get(k) != Some(v) {
            child.env(config::child_env_name(k), v);
        }
    }
    for k in env.keys() {
        if !launch.env.contains_key(k) {
            child.env_remove(k);
        }
    }
    if launch.has_print && config::get(&launch.env, "CLAUDE_CODEX_KEEP_STDIN").is_none() {
        child.stdin(Stdio::piped());
    }
    // .NET Framework's Process.Start writes Console.InputEncoding's preamble to a
    // redirected stdin; among code pages SetConsoleCP accepts, only 65001 has one.
    let utf8_console = platform::console_input_is_utf8();
    let mut child = child.spawn()?;
    let _guard = platform::ChildWaitGuard::new();
    // Close the empty pipe; Stdio::null is a file, unlike PowerShell's @() |.
    if let Some(mut stdin) = child.stdin.take() {
        if utf8_console {
            let _ = stdin.write_all(b"\xef\xbb\xbf");
        }
    }
    trace.record("native", "eligible", Some(&check));
    Ok(child.wait()?.code().unwrap_or(1))
}
fn main() {
    let start = Instant::now();
    let code = match run(start) {
        Ok(n) => n,
        Err(e) => {
            eprintln!("[claude-codex] Native launcher failed: {e}");
            1
        }
    };
    process::exit(code);
}
